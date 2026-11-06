use base64::{engine::general_purpose::STANDARD, Engine};
use migration_connector::{
    api, crypto,
    network::{self, Policy, Request},
    protocol::{self, Binding},
    transport, Result,
};
use openssl::pkey::{PKey, Private};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
        io::AsRawFd,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};
use zeroize::Zeroizing;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    public_api_url: String,
    broker_host: String,
    broker_port: u16,
    root_ca: String,
    job_public_key: String,
    collector_archive: String,
    helper_path: String,
}
#[derive(Serialize, Deserialize)]
struct Local {
    request_id: String,
    candidate_id: String,
    device_id: Option<String>,
    epoch: u64,
    connection_id: Option<String>,
    wireguard_private: String,
    wireguard_public: String,
}
impl Drop for Local {
    fn drop(&mut self) {
        use zeroize::Zeroize;
        self.wireguard_private.zeroize();
    }
}
fn private_file(p: &Path, b: &[u8]) -> Result<()> {
    let tmp = p.with_extension(format!("{}.tmp", crypto::b64(&crypto::random::<8>()?)));
    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&tmp)?;
    f.write_all(b)?;
    f.sync_all()?;
    fs::rename(tmp, p)?;
    Ok(())
}
fn save(dir: &Path, s: &Local) -> Result<()> {
    let b = Zeroizing::new(serde_json::to_vec(s)?);
    private_file(&dir.join("state.json"), &b)
}
fn safe_read(p: &Path) -> Result<Vec<u8>> {
    let m = fs::symlink_metadata(p)?;
    if !m.is_file() || m.uid() != unsafe { libc::getuid() } || m.mode() & 0o077 != 0 {
        return Err("unsafe state file".into());
    }
    let mut b = Vec::new();
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(p)?
        .take(65537)
        .read_to_end(&mut b)?;
    if b.len() > 65536 {
        return Err("state too large".into());
    }
    Ok(b)
}
fn asset(base: &Path, name: &str) -> Result<PathBuf> {
    if name.is_empty() || name.contains('/') || name.contains('\\') || name == "." || name == ".." {
        return Err("invalid asset path".into());
    }
    let p = base.join(name);
    if !fs::symlink_metadata(&p)?.is_file() {
        return Err("invalid asset".into());
    }
    Ok(p)
}
fn net_req(op: &str, s: &Local) -> Request {
    Request {
        operation: op.into(),
        connection_id: s
            .connection_id
            .clone()
            .unwrap_or_else(|| "preflight0000000000".into()),
        epoch: s.epoch,
        private_key: None,
        public_key: None,
        psk: None,
        key_expires_at_ms: 0,
    }
}
fn collect_raw(
    c: &Config,
    base: &Path,
    s: &Local,
    op: &str,
    target: Option<&str>,
    valid: &std::sync::atomic::AtomicBool,
) -> Result<Value> {
    if !matches!(op, "discover" | "collect") {
        return Err("operation denied".into());
    }
    let mut cmd = Command::new("/usr/bin/python3");
    cmd.arg(asset(base, &c.collector_archive)?).arg(op).args([
        "--request-id",
        &s.request_id,
        "--device-id",
        s.device_id.as_deref().ok_or("device missing")?,
        "--epoch",
        &s.epoch.to_string(),
    ]);
    if let Some(t) = target {
        cmd.args(["--target-id", t]);
    }
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let stdout = child.stdout.take().ok_or("collector output")?;
    let reader = std::thread::spawn(move || {
        let mut b = Vec::new();
        let r = stdout.take(2_097_153).read_to_end(&mut b);
        (r, b)
    });
    let deadline = std::time::Instant::now() + Duration::from_secs(90);
    loop {
        if let Some(status) = child.try_wait()? {
            let (result, b) = reader.join().map_err(|_| "collector reader")?;
            result?;
            if !status.success() || b.len() > 2_097_152 {
                return Err("environment collection failed".into());
            }
            return Ok(serde_json::from_slice(&b)?);
        }
        if std::time::Instant::now() > deadline || !valid.load(std::sync::atomic::Ordering::Acquire)
        {
            child.kill()?;
            child.wait()?;
            return Err("environment collection timed out".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}
fn collect(
    c: &Config,
    base: &Path,
    s: &Local,
    op: &str,
    target: Option<&str>,
    key: &PKey<Private>,
    api_base: &str,
) -> Result<Value> {
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Condvar, Mutex,
    };
    let valid = AtomicBool::new(true);
    let stop = Mutex::new(false);
    let wake = Condvar::new();
    std::thread::scope(|scope| {
        scope.spawn(|| loop {
            let done = stop.lock().unwrap();
            let (done, _) = wake
                .wait_timeout_while(done, Duration::from_secs(10), |v| !*v)
                .unwrap();
            if *done {
                break;
            }
            drop(done);
            let result = (|| -> Result<()> {
                let v = api::device(
                    api_base,
                    &s.request_id,
                    &s.candidate_id,
                    key,
                    "poll",
                    json!({}),
                )?;
                if v["snapshot"]["device"] != "APPROVED"
                    || v["snapshot"]["tunnel"] != "READY"
                    || v["snapshot"]["epoch"].as_u64() != Some(s.epoch)
                {
                    return Err("lease rejected".into());
                }
                network::helper(Path::new(&c.helper_path), &net_req("refresh", s))?;
                Ok(())
            })();
            if result.is_err() {
                valid.store(false, Ordering::Release);
                break;
            }
        });
        let result = collect_raw(c, base, s, op, target, &valid);
        *stop.lock().unwrap() = true;
        wake.notify_all();
        if !valid.load(Ordering::Acquire) {
            return Err("collection authorization lost".into());
        }
        result
    })
}
fn establish(
    c: &Config,
    base: &Path,
    dir: &Path,
    s: &mut Local,
    key: &PKey<Private>,
    policy: &Policy,
) -> Result<Binding> {
    let cert = crypto::certificate(key)?;
    let client = transport::client(&asset(base, &c.root_ca)?, key, &cert)?;
    let stream = TcpStream::connect((c.broker_host.as_str(), c.broker_port))?;
    stream.set_read_timeout(Some(Duration::from_secs(30)))?;
    stream.set_write_timeout(Some(Duration::from_secs(30)))?;
    let mut tls = client.connect(&c.broker_host, stream)?;
    transport::enforce(tls.ssl())?;
    transport::peer_public(&tls)?;
    if s.epoch > 0 {
        let (private, public) = crypto::wg_key()?;
        s.wireguard_private = private.to_string();
        s.wireguard_public = public;
        save(dir, s)?;
    }
    protocol::send(
        &mut tls,
        &json!({"type":"BIND","request_id":s.request_id,"client_wireguard_public_key":s.wireguard_public}),
    )?;
    let prepared: Value = protocol::recv(&mut tls)?;
    if prepared["type"] != "PREPARE" {
        return Err("broker prepare missing".into());
    }
    let binding: Binding = serde_json::from_value(prepared["binding"].clone())?;
    binding.validate(
        s.device_id.as_deref().ok_or("device missing")?,
        &s.wireguard_public,
        s.epoch,
    )?;
    if binding.client_tunnel_address != policy.local_address.to_string()
        || binding.gateway_tunnel_address != policy.peer_address.to_string()
        || binding.allowed_services_digest
            != format!(
                "sha384:{}",
                crypto::digest(&protocol::canonical(
                    &json!({"job_port":policy.job_port,"probe_port":policy.probe_port})
                )?)
            )
    {
        return Err("network policy mismatch".into());
    }
    let psk = transport::exporter(&tls, &binding)?;
    protocol::send(&mut tls, &json!({"type":"PREPARE","epoch":binding.epoch}))?;
    let commit: Value = protocol::recv(&mut tls)?;
    if commit["type"] != "COMMIT" || commit["epoch"] != binding.epoch {
        return Err("commit mismatch".into());
    }
    s.epoch = binding.epoch;
    s.connection_id = Some(binding.connection_id.clone());
    save(dir, s)?;
    let started = migration_connector::now_ms() / 1000;
    let req = Request {
        operation: "configure".into(),
        connection_id: binding.connection_id.clone(),
        epoch: binding.epoch,
        private_key: Some(s.wireguard_private.clone()),
        public_key: Some(
            STANDARD.encode(crypto::decode(&binding.gateway_wireguard_public_key, 32)?),
        ),
        psk: Some(STANDARD.encode(&psk[..])),
        key_expires_at_ms: binding.expires_at_ms,
    };
    network::helper(Path::new(&c.helper_path), &req)?;
    drop(req);
    drop(psk);
    protocol::send(&mut tls, &json!({"type":"COMMIT","epoch":binding.epoch}))?;
    network::probe(policy, prepared["nonce"].as_str().ok_or("probe nonce")?)?;
    let status = network::helper(Path::new(&c.helper_path), &net_req("status", s))?;
    if status["handshake_at"].as_u64().unwrap_or(0) < started {
        return Err("new WireGuard handshake missing".into());
    }
    protocol::send(
        &mut tls,
        &json!({"type":"STATUS","epoch":binding.epoch,"checks":2}),
    )?;
    let ready: Value = protocol::recv(&mut tls)?;
    if ready["type"] != "STATUS" || ready["ready"] != true {
        return Err("broker not ready".into());
    }
    Ok(binding)
}
fn cleanup(c: &Config, dir: &Path, s: &Local, key: &PKey<Private>) -> Result<()> {
    if s.connection_id.is_some() {
        network::helper(Path::new(&c.helper_path), &net_req("remove", s))?;
    }
    for name in ["device.pem", "state.json"] {
        let path = dir.join(name);
        if path.exists() {
            fs::remove_file(path)?;
        }
    }
    api::device(
        &c.public_api_url,
        &s.request_id,
        &s.candidate_id,
        key,
        "cleanup",
        json!({"local":true}),
    )?;
    Ok(())
}
fn run(config_path: &Path, resume: Option<&str>) -> Result<()> {
    if config_path.file_name().and_then(|n| n.to_str()) != Some("connector.json") {
        return Err("signed connector.json required".into());
    }
    if !cfg!(target_os = "linux") {
        return Err("Ubuntu Linux required".into());
    }
    let base = config_path.parent().ok_or("bundle path")?;
    migration_connector::release::verify_bundle(base)?;
    let c: Config = serde_json::from_slice(&fs::read(config_path)?)?;
    api::https(&c.public_api_url)?;
    if c.root_ca != "root-ca.pem"
        || c.collector_archive != "inventory.pyz"
        || c.helper_path != "/usr/local/libexec/migration-net-helper"
    {
        return Err("helper path rejected".into());
    }
    crypto::public(&c.job_public_key)?;
    let mut enrollment = Zeroizing::new(String::new());
    let (request, seed) = if let Some(request) = resume {
        api::identifier(request)?;
        (request.to_string(), Zeroizing::new(Vec::new()))
    } else {
        std::io::stdin()
            .take(1025)
            .read_to_string(&mut enrollment)?;
        let (request, seed) = enrollment
            .trim()
            .split_once('.')
            .ok_or("invalid enrollment input")?;
        if request.len() != 22 {
            return Err("invalid enrollment identifier".into());
        }
        api::identifier(request)?;
        (
            request.to_string(),
            Zeroizing::new(crypto::decode(seed, 32)?),
        )
    };
    let home = std::env::var_os("HOME").ok_or("home required")?;
    let parent = PathBuf::from(home).join(".local/state/migration-connector");
    fs::create_dir_all(&parent)?;
    let m = fs::symlink_metadata(&parent)?;
    if !m.is_dir() || m.uid() != unsafe { libc::getuid() } {
        return Err("unsafe state directory".into());
    }
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o700))?;
    let dir = parent.join(&request);
    fs::create_dir_all(&dir)?;
    let m = fs::symlink_metadata(&dir)?;
    if !m.is_dir() || m.uid() != unsafe { libc::getuid() } {
        return Err("unsafe request directory".into());
    }
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(dir.join("lock"))?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err("connector already running".into());
    }
    let (mut state, key) = if dir.join("state.json").exists() {
        (
            serde_json::from_slice::<Local>(&safe_read(&dir.join("state.json"))?)?,
            PKey::private_key_from_pem(&Zeroizing::new(safe_read(&dir.join("device.pem"))?))?,
        )
    } else {
        if resume.is_some() {
            return Err("device state missing; new enrollment required".into());
        }
        let key = crypto::device_key()?;
        let (private, public) = crypto::wg_key()?;
        let state = Local {
            request_id: request.clone(),
            candidate_id: crypto::b64(&crypto::random::<16>()?),
            device_id: None,
            epoch: 0,
            connection_id: None,
            wireguard_private: private.to_string(),
            wireguard_public: public,
        };
        private_file(
            &dir.join("device.pem"),
            &Zeroizing::new(key.private_key_to_pem_pkcs8()?),
        )?;
        save(&dir, &state)?;
        (state, key)
    };
    if state.request_id != request {
        return Err("state request mismatch".into());
    }
    let policy: Policy = serde_json::from_value(
        network::helper(Path::new(&c.helper_path), &net_req("check", &state))?["policy"].clone(),
    )?;
    policy.validate()?;
    if state.connection_id.is_some() {
        let _ = network::helper(Path::new(&c.helper_path), &net_req("remove", &state));
    }
    if state.device_id.is_none() {
        let response = (|| -> Result<Value> {
            let challenge = api::post(
                &format!("{}/{request}/challenge", c.public_api_url),
                &json!({"candidateId":state.candidate_id}),
                None,
            )?;
            let payload = json!({"version":1,"request_id":request,"candidate_id":state.candidate_id,"challenge_id":challenge["challenge_id"],"challenge":challenge["challenge"],"device_auth_public_key":crypto::b64(&key.raw_public_key()?),"wireguard_public_key":state.wireguard_public,"audience":"migration-connector-enrollment","expires_at_ms":challenge["expires_at_ms"]});
            let enroll_key = crypto::seed_key(&seed)?;
            let body = json!({"payload":payload,"enrollment_signature":crypto::sign(&enroll_key,&protocol::signed_bytes("migration/enrollment/v1",&payload)?)?,"device_signature":crypto::sign(&key,&protocol::signed_bytes("migration/device-possession/v1",&payload)?)?,"metadata":{"hostname":"user-terminal","os":"linux","arch":std::env::consts::ARCH}});
            api::post(
                &format!("{}/{request}/prove", c.public_api_url),
                &body,
                None,
            )
        })();
        let response = match response {
            Ok(r) => r,
            Err(_) => api::device(
                &c.public_api_url,
                &request,
                &state.candidate_id,
                &key,
                "status",
                json!({}),
            )?,
        };
        state.device_id = Some(
            response["device_id"]
                .as_str()
                .ok_or("device response")?
                .into(),
        );
        save(&dir, &state)?;
        if let Some(code) = response["confirmation_code"].as_str() {
            println!("웹에서 단말 확인 코드를 입력하세요: {code}");
        }
    }
    drop(seed);
    drop(enrollment);
    loop {
        let status = api::device(
            &c.public_api_url,
            &request,
            &state.candidate_id,
            &key,
            "status",
            json!({}),
        )?;
        match status["snapshot"]["device"].as_str() {
            Some("APPROVED") => break,
            Some("PENDING_APPROVAL") => {
                if let Some(code) = status["confirmation_code"].as_str() {
                    println!("웹에서 단말 확인 코드를 입력하세요: {code}");
                }
                std::thread::sleep(Duration::from_secs(2))
            }
            _ => {
                if state.device_id.is_some() {
                    cleanup(&c, &dir, &state, &key)?;
                }
                return Err("device approval rejected or expired".into());
            }
        }
    }
    loop {
        let binding = match establish(&c, base, &dir, &mut state, &key, &policy) {
            Ok(b) => b,
            Err(_) => {
                let _ = network::helper(Path::new(&c.helper_path), &net_req("remove", &state));
                eprintln!("TUNNEL_RECOVERY_REQUIRED");
                std::thread::sleep(Duration::from_secs(5));
                let st = api::device(
                    &c.public_api_url,
                    &request,
                    &state.candidate_id,
                    &key,
                    "status",
                    json!({}),
                )?;
                if st["snapshot"]["device"] != "APPROVED" {
                    cleanup(&c, &dir, &state, &key)?;
                    return Ok(());
                }
                continue;
            }
        };
        println!("보안 연결을 확인했습니다. 웹에서 조회할 Kubernetes 문맥을 선택하세요.");
        let tunnel = format!(
            "http://{}",
            SocketAddr::new(policy.peer_address, policy.job_port)
        );
        let api_base = format!("{tunnel}/api/v1/connections");
        let found = collect(&c, base, &state, "discover", None, &key, &api_base)?;
        api::device(
            &api_base,
            &request,
            &state.candidate_id,
            &key,
            "discovered",
            json!({"environments":found["discovered"]}),
        )?;
        let mut last_lease = 0;
        let mut completed = std::collections::HashSet::new();
        loop {
            let now = migration_connector::now_ms();
            if now >= binding.expires_at_ms.saturating_sub(300000) {
                break;
            }
            if now - last_lease >= 20000 {
                let status = api::device(
                    &c.public_api_url,
                    &request,
                    &state.candidate_id,
                    &key,
                    "status",
                    json!({}),
                )?;
                if status["snapshot"]["device"] != "APPROVED" {
                    cleanup(&c, &dir, &state, &key)?;
                    return Ok(());
                }
                if status["snapshot"]["tunnel"] != "READY"
                    || status["snapshot"]["epoch"].as_u64() != Some(state.epoch)
                {
                    break;
                }
                network::helper(Path::new(&c.helper_path), &net_req("refresh", &state))?;
                last_lease = now;
            }
            let response = match api::device(
                &api_base,
                &request,
                &state.candidate_id,
                &key,
                "poll",
                json!({}),
            ) {
                Ok(v) => v,
                Err(_) => break,
            };
            if !response["task"].is_null() {
                let task = &response["task"];
                let op = protocol::verify_job(
                    task,
                    &c.job_public_key,
                    state.device_id.as_deref().ok_or("device missing")?,
                    state.epoch,
                )?;
                let job = task["payload"]["job_id"].as_str().ok_or("job id")?;
                if !completed.contains(job) {
                    let result = collect(
                        &c,
                        base,
                        &state,
                        op,
                        task["payload"]["target_id"].as_str(),
                        &key,
                        &api_base,
                    )?;
                    protocol::verify_job(
                        task,
                        &c.job_public_key,
                        state.device_id.as_deref().ok_or("device missing")?,
                        state.epoch,
                    )?;
                    api::device(
                        &api_base,
                        &request,
                        &state.candidate_id,
                        &key,
                        "inventory",
                        json!({"job_id":job,"report":result["report"]}),
                    )?;
                    completed.insert(job.to_string());
                }
            }
            std::thread::sleep(Duration::from_secs(2));
        }
        let _ = network::helper(Path::new(&c.helper_path), &net_req("remove", &state));
    }
}
fn main() {
    let args: Vec<_> = std::env::args().collect();
    let result = match args.get(1).map(String::as_str) {
        Some("run") if args.len() == 3 => run(Path::new(&args[2]), None),
        Some("resume") if args.len() == 4 => run(Path::new(&args[2]), Some(&args[3])),
        Some("crypto-vector") if args.len() == 2 => vector(),
        Some("verify-release") if args.len() == 3 => {
            migration_connector::release::verify_bundle(Path::new(&args[2])).map(|_| ())
        }
        Some("install-release") if args.len() == 4 => {
            migration_connector::release::install_release(&args[2], Path::new(&args[3])).map(|_| ())
        }
        _ => Err("usage: migration-connector run CONFIG".into()),
    };
    if result.is_err() {
        eprintln!("CONNECTOR_FAILED: inspect the web connection state; no secret details logged");
        std::process::exit(1)
    }
}
fn vector() -> Result<()> {
    let key = crypto::seed_key(&[7u8; 32])?;
    let data = b"migration/interop/v1\n{\"message\":\"test\"}";
    println!(
        "{}",
        json!({"seed":crypto::b64(&[7u8;32]),"public_key":crypto::b64(&key.raw_public_key()?),"message":String::from_utf8_lossy(data),"signature":crypto::sign(&key,data)?})
    );
    Ok(())
}
