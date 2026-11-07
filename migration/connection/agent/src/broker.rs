//! One broker per tenant and isolated network namespace. No shared customer routing.
use base64::{engine::general_purpose::STANDARD, Engine};
use migration_connector::{
    api, crypto,
    network::{self, Policy, Request},
    protocol::{self, Binding},
    transport, Result,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use zeroize::Zeroizing;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    listen: SocketAddr,
    certificate: PathBuf,
    private_key: PathBuf,
    runtime_api_url: String,
    public_api_url: String,
    runtime_token_file: PathBuf,
    tenant_id: String,
    gateway_id: String,
    helper_path: PathBuf,
    release_version: String,
    bootstrap_sha384: String,
}
#[derive(Clone)]
struct Session {
    request_id: String,
    binding: Binding,
    last_activity_ms: u64,
}
struct Broker {
    config: Config,
    token: Zeroizing<String>,
    policy: Policy,
    current: Mutex<Option<Session>>,
    negotiating: AtomicBool,
    transition: Mutex<()>,
}
fn root_file(path: &Path) -> Result<Vec<u8>> {
    let mut f = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let m = f.metadata()?;
    if !m.is_file() || m.uid() != 0 || m.mode() & 0o022 != 0 {
        return Err("root owned configuration required".into());
    }
    let mut data = Vec::new();
    (&mut f).take(1_048_577).read_to_end(&mut data)?;
    if data.len() > 1_048_576 {
        return Err("configuration too large".into());
    }
    Ok(data)
}
fn req(op: &str, s: &Session) -> Request {
    Request {
        operation: op.into(),
        connection_id: s.binding.connection_id.clone(),
        epoch: s.binding.epoch,
        private_key: None,
        public_key: None,
        psk: None,
        key_expires_at_ms: 0,
    }
}
impl Broker {
    fn authorize(&self, id: &str) -> Result<Value> {
        api::get(
            &format!("{}/{id}/authorize", self.config.runtime_api_url),
            &self.token,
        )
    }
    fn status(&self, s: &Session, state: &str) -> Result<Value> {
        let now = migration_connector::now_ms();
        api::post(
            &format!("{}/{}/tunnel", self.config.runtime_api_url, s.request_id),
            &json!({"state":state,"epoch":s.binding.epoch,"connection_id":s.binding.connection_id,"checks":2,"key_expires_at_ms":s.binding.expires_at_ms,"lease_expires_at_ms":(now+55000).min(s.binding.expires_at_ms)}),
            Some(&self.token),
        )
    }
    fn remove(&self, s: &Session) -> Result<()> {
        if network::helper(&self.config.helper_path, &req("remove", s)).is_err() {
            let _ = self.status(s, "DEGRADED");
            return Err("gateway removal unconfirmed".into());
        }
        if self.status(s, "DEGRADED").is_err() {
            self.status(s, "REVOKED")?;
        }
        Ok(())
    }
    fn remove_replaced(&self, previous: &network::Replacement) -> Result<()> {
        let request = previous.removal().ok_or("replacement binding missing")?;
        let removed = network::helper(&self.config.helper_path, &request);
        api::post(
            &format!(
                "{}/{}/tunnel",
                self.config.runtime_api_url, previous.request_id
            ),
            &json!({
                "state": if removed.is_ok() { "REVOKED" } else { "REVOKE_BLOCKED" },
                "epoch": request.epoch,
                "connection_id": request.connection_id
            }),
            Some(&self.token),
        )?;
        removed?;
        *self.current.lock().map_err(|_| "state lock")? = None;
        Ok(())
    }

    fn ready_health(&self) -> Result<()> {
        api::post(
            &format!("{}/health", self.config.runtime_api_url),
            &json!({"tenant_id":self.config.tenant_id,"gateway_id":self.config.gateway_id,"release_version":self.config.release_version,"protocol_version":1,"crypto_profile":"MLDSA65-X25519MLKEM768-WG","bootstrap_sha384":self.config.bootstrap_sha384}),
            Some(&self.token),
        )?;
        Ok(())
    }
    fn auth_failure(&self, source: std::net::IpAddr) {
        let _ = api::post(
            &format!("{}/auth-failure", self.config.runtime_api_url),
            &json!({"source_ip":source.to_string()}),
            Some(&self.token),
        );
    }
    fn establish(&self, mut tls: openssl::ssl::SslStream<TcpStream>) -> Result<()> {
        transport::enforce(tls.ssl())?;
        let source = tls.get_ref().peer_addr()?.ip();
        let public = match transport::peer_public(&tls) {
            Ok(p) => p,
            Err(e) => {
                self.auth_failure(source);
                return Err(e);
            }
        };
        let bind: Bind = protocol::recv(&mut tls)?;
        if bind.kind != "BIND" {
            return Err("BIND required".into());
        }
        api::identifier(&bind.request_id)?;
        crypto::decode(&bind.client_wireguard_public_key, 32)?;
        let auth = self.authorize(&bind.request_id)?;
        if auth["tenant_id"] != self.config.tenant_id || auth["device_auth_public_key"] != public {
            self.auth_failure(source);
            return Err("device authorization rejected".into());
        }
        eprintln!("TUNNEL_TLS_VERIFIED protocol=TLSv1.3 group=X25519MLKEM768 peer_auth=MLDSA65 approved_device=true");
        let epoch = auth["epoch"].as_u64().ok_or("epoch missing")? + 1;
        if epoch == 1 && auth["wireguard_public_key"] != bind.client_wireguard_public_key {
            self.auth_failure(source);
            return Err("first peer mismatch".into());
        }
        let replacement: Option<network::Replacement> =
            serde_json::from_value(auth["replacement"].clone())?;
        let existing = self.current.lock().map_err(|_| "state lock")?.clone();
        if let Some(s) = &existing {
            if s.request_id != bind.request_id
                && replacement
                    .as_ref()
                    .is_none_or(|old| old.request_id != s.request_id)
            {
                return Err("gateway already assigned".into());
            }
        }
        if let Some(previous) = &replacement {
            self.remove_replaced(previous)?;
        }
        let (private, wg_public) = crypto::wg_key()?;
        let binding = Binding {
            version: 1,
            tenant_id: self.config.tenant_id.clone(),
            device_id: auth["device_id"].as_str().ok_or("device id")?.into(),
            gateway_id: self.config.gateway_id.clone(),
            connection_id: crypto::b64(&crypto::random::<16>()?),
            epoch,
            client_wireguard_public_key: bind.client_wireguard_public_key,
            gateway_wireguard_public_key: wg_public,
            client_tunnel_address: self.policy.peer_address.to_string(),
            gateway_tunnel_address: self.policy.local_address.to_string(),
            allowed_services_digest: format!(
                "sha384:{}",
                crypto::digest(&protocol::canonical(
                    &json!({"job_port":self.policy.job_port,"probe_port":self.policy.probe_port})
                )?)
            ),
            expires_at_ms: migration_connector::now_ms() + 1800000,
        };
        let session = Session {
            request_id: bind.request_id,
            binding,
            last_activity_ms: migration_connector::now_ms(),
        };
        let nonce = crypto::b64(&crypto::random::<32>()?);
        let psk = transport::exporter(&tls, &session.binding)?;
        eprintln!("TUNNEL_PSK_DERIVED source=TLS-exporter context=SHA384 bytes=32");
        protocol::send(
            &mut tls,
            &json!({"type":"PREPARE","binding":session.binding,"nonce":nonce}),
        )?;
        let prepared: EpochMessage = protocol::recv(&mut tls)?;
        if prepared.kind != "PREPARE" || prepared.epoch != epoch {
            return Err("PREPARE rejected".into());
        }
        let fresh = self.authorize(&session.request_id)?;
        if fresh["device_auth_public_key"] != public {
            return Err("authorization changed".into());
        }
        // COMMIT closes the prior job path before changing either peer. Never roll back.
        if let Some(old) = self.current.lock().map_err(|_| "state lock")?.take() {
            self.remove(&old)?;
        }
        self.status(&session, "REKEYING")?;
        protocol::send(&mut tls, &json!({"type":"COMMIT","epoch":epoch}))?;
        let started = migration_connector::now_ms() / 1000;
        let request = Request {
            operation: "configure".into(),
            connection_id: session.binding.connection_id.clone(),
            epoch,
            private_key: Some(private.to_string()),
            public_key: Some(STANDARD.encode(crypto::decode(
                &session.binding.client_wireguard_public_key,
                32,
            )?)),
            psk: Some(STANDARD.encode(&psk[..])),
            key_expires_at_ms: session.binding.expires_at_ms,
        };
        let result = (|| -> Result<()> {
            network::helper(&self.config.helper_path, &request)?;
            let commit: EpochMessage = protocol::recv(&mut tls)?;
            if commit.kind != "COMMIT" || commit.epoch != epoch {
                return Err("COMMIT rejected".into());
            }
            network::probe(&self.policy, &nonce)?;
            let status = network::helper(&self.config.helper_path, &req("status", &session))?;
            if status["handshake_at"].as_u64().unwrap_or(0) < started {
                return Err("new WireGuard handshake missing".into());
            }
            let confirmed: ProbeMessage = protocol::recv(&mut tls)?;
            if confirmed.kind != "STATUS" || confirmed.epoch != epoch || confirmed.checks != 2 {
                return Err("probe confirmation rejected".into());
            }
            self.authorize(&session.request_id)?;
            self.status(&session, "READY")?;
            eprintln!(
                "TUNNEL_READY epoch={epoch} bidirectional_checks=2 wireguard_handshake=fresh"
            );
            *self.current.lock().map_err(|_| "state lock")? = Some(session.clone());
            protocol::send(
                &mut tls,
                &json!({"type":"STATUS","ready":true,"epoch":epoch}),
            )?;
            Ok(())
        })();
        if result.is_err() {
            let _ = self.remove(&session);
        }
        result
    }
    fn maintenance(&self) {
        loop {
            if self.ready_health().is_err() {
                eprintln!("BROKER_HEALTH_LEASE_UNAVAILABLE");
            }
            let _transition = self.transition.lock().expect("transition mutex");
            if !self.negotiating.load(Ordering::Acquire) {
                let s = self.current.lock().ok().and_then(|g| g.clone());
                if let Some(s) = s {
                    let valid = migration_connector::now_ms() < s.binding.expires_at_ms
                        && migration_connector::now_ms().saturating_sub(s.last_activity_ms) < 45000
                        && self.authorize(&s.request_id).is_ok();
                    let valid = valid && self.status(&s, "READY").is_ok();
                    if !valid
                        || network::helper(&self.config.helper_path, &req("refresh", &s)).is_err()
                    {
                        if self.remove(&s).is_ok() {
                            if let Ok(mut g) = self.current.lock() {
                                *g = None;
                            }
                        }
                    }
                }
            }
            drop(_transition);
            std::thread::sleep(Duration::from_secs(20));
        }
    }
    fn relay(&self, mut stream: TcpStream) -> Result<()> {
        stream.set_read_timeout(Some(Duration::from_secs(5)))?;
        stream.set_write_timeout(Some(Duration::from_secs(5)))?;
        if stream.peer_addr()?.ip() != self.policy.peer_address {
            return Err("tunnel source mismatch".into());
        }
        let session = self
            .current
            .lock()
            .map_err(|_| "state lock")?
            .clone()
            .ok_or("tunnel not ready")?;
        if migration_connector::now_ms() >= session.binding.expires_at_ms {
            return Err("key expired".into());
        }
        let auth = self.authorize(&session.request_id)?;
        if auth["epoch"].as_u64() != Some(session.binding.epoch) || auth["state"] != "READY" {
            return Err("tunnel state changed".into());
        }
        let (path, body) = http_request(&mut stream)?;
        let prefix = format!("/api/v1/connections/{}/", session.request_id);
        let suffix = path.strip_prefix(&prefix).ok_or("request scope")?;
        let url = match suffix {
            "device-challenge" => format!(
                "{}/{}/device-challenge",
                self.config.public_api_url, session.request_id
            ),
            "device" => format!(
                "{}/{}/device",
                self.config.runtime_api_url, session.request_id
            ),
            _ => return Err("relay operation denied".into()),
        };
        let response = api::post(
            &url,
            &body,
            if suffix == "device" {
                Some(&self.token)
            } else {
                None
            },
        )?;
        if let Ok(mut current) = self.current.lock() {
            if let Some(active) = current.as_mut() {
                if active.binding.epoch == session.binding.epoch {
                    active.last_activity_ms = migration_connector::now_ms();
                }
            }
        }
        let data = serde_json::to_vec(&response)?;
        write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",data.len())?;
        stream.write_all(&data)?;
        Ok(())
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Bind {
    #[serde(rename = "type")]
    kind: String,
    request_id: String,
    client_wireguard_public_key: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EpochMessage {
    #[serde(rename = "type")]
    kind: String,
    epoch: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProbeMessage {
    #[serde(rename = "type")]
    kind: String,
    epoch: u64,
    checks: u8,
}
fn http_request(s: &mut impl Read) -> Result<(String, Value)> {
    let mut header = Vec::new();
    let mut b = [0u8; 1];
    while !header.ends_with(b"\r\n\r\n") {
        if header.len() >= 8192 {
            return Err("header too large".into());
        }
        s.read_exact(&mut b)?;
        header.push(b[0]);
    }
    let h = std::str::from_utf8(&header)?;
    let mut lines = h.split("\r\n");
    let mut first = lines.next().ok_or("request line")?.split(' ');
    if first.next() != Some("POST") {
        return Err("POST required".into());
    }
    let path = first.next().ok_or("path")?.to_string();
    if first.next() != Some("HTTP/1.1") || first.next().is_some() {
        return Err("protocol rejected".into());
    }
    let mut length = None;
    for line in lines.filter(|s| !s.is_empty()) {
        let (k, v) = line.split_once(':').ok_or("header")?;
        if k.eq_ignore_ascii_case("transfer-encoding") {
            return Err("chunked rejected".into());
        }
        if k.eq_ignore_ascii_case("content-length") {
            if length.is_some() {
                return Err("duplicate length".into());
            }
            length = Some(v.trim().parse::<usize>()?);
        }
    }
    let n = length.ok_or("length required")?;
    if n > 2_097_152 {
        return Err("body too large".into());
    }
    let mut b = vec![0; n];
    s.read_exact(&mut b)?;
    Ok((path, serde_json::from_slice(&b)?))
}
#[cfg(target_os = "linux")]
fn freebind(addr: SocketAddr) -> Result<TcpListener> {
    use std::os::fd::FromRawFd;
    unsafe {
        let family = if addr.is_ipv4() {
            libc::AF_INET
        } else {
            libc::AF_INET6
        };
        let fd = libc::socket(family, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0);
        if fd < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let on: libc::c_int = 1;
        let level = if addr.is_ipv4() {
            libc::IPPROTO_IP
        } else {
            libc::IPPROTO_IPV6
        };
        let option = if addr.is_ipv4() {
            libc::IP_FREEBIND
        } else {
            libc::IPV6_FREEBIND
        };
        if libc::setsockopt(
            fd,
            level,
            option,
            &on as *const _ as *const _,
            std::mem::size_of_val(&on) as _,
        ) != 0
        {
            libc::close(fd);
            return Err("freebind failed".into());
        }
        let result = match addr {
            SocketAddr::V4(a) => {
                let a = libc::sockaddr_in {
                    sin_family: libc::AF_INET as _,
                    sin_port: a.port().to_be(),
                    sin_addr: libc::in_addr {
                        s_addr: u32::from_ne_bytes(a.ip().octets()),
                    },
                    sin_zero: [0; 8],
                };
                libc::bind(
                    fd,
                    &a as *const _ as *const _,
                    std::mem::size_of_val(&a) as _,
                )
            }
            SocketAddr::V6(a) => {
                let a = libc::sockaddr_in6 {
                    sin6_family: libc::AF_INET6 as _,
                    sin6_port: a.port().to_be(),
                    sin6_flowinfo: 0,
                    sin6_addr: libc::in6_addr {
                        s6_addr: a.ip().octets(),
                    },
                    sin6_scope_id: a.scope_id(),
                };
                libc::bind(
                    fd,
                    &a as *const _ as *const _,
                    std::mem::size_of_val(&a) as _,
                )
            }
        };
        if result != 0 || libc::listen(fd, 32) != 0 {
            libc::close(fd);
            return Err("listener failed".into());
        }
        Ok(TcpListener::from_raw_fd(fd))
    }
}
#[cfg(not(target_os = "linux"))]
fn freebind(addr: SocketAddr) -> Result<TcpListener> {
    Ok(TcpListener::bind(addr)?)
}
fn run(path: &Path) -> Result<()> {
    if !cfg!(target_os = "linux") {
        return Err("Linux isolated namespace required".into());
    }
    let config: Config = serde_json::from_slice(&root_file(path)?)?;
    api::https(&config.runtime_api_url)?;
    api::https(&config.public_api_url)?;
    if config.helper_path != Path::new("/usr/local/libexec/migration-net-helper") {
        return Err("helper path".into());
    }
    if fs::symlink_metadata(&config.runtime_token_file)?.mode() & 0o077 != 0
        || fs::symlink_metadata(&config.private_key)?.mode() & 0o077 != 0
    {
        return Err("runtime token must be 0600".into());
    }
    let token = Zeroizing::new(
        String::from_utf8(root_file(&config.runtime_token_file)?)?
            .trim()
            .to_string(),
    );
    let policy: Policy = serde_json::from_value(
        network::helper(
            &config.helper_path,
            &Request {
                operation: "check".into(),
                connection_id: "preflight0000000000".into(),
                epoch: 0,
                private_key: None,
                public_key: None,
                psk: None,
                key_expires_at_ms: 0,
            },
        )?["policy"]
            .clone(),
    )?;
    if policy.role != "gateway" {
        return Err("gateway policy required".into());
    }
    root_file(&config.private_key)?;
    root_file(&config.certificate)?;
    network::helper(
        &config.helper_path,
        &Request {
            operation: "reset".into(),
            connection_id: "preflight0000000000".into(),
            epoch: 0,
            private_key: None,
            public_key: None,
            psk: None,
            key_expires_at_ms: 0,
        },
    )?;
    let acceptor = transport::server(&config.certificate, &config.private_key)?;
    let listener = TcpListener::bind(config.listen)?;
    let job = freebind(SocketAddr::new(policy.local_address, policy.job_port))?;
    let broker = Arc::new(Broker {
        config,
        token,
        policy,
        current: Mutex::new(None),
        negotiating: AtomicBool::new(false),
        transition: Mutex::new(()),
    });
    let b = broker.clone();
    std::thread::spawn(move || b.maintenance());
    let b = broker.clone();
    std::thread::spawn(move || {
        for stream in job.incoming().flatten() {
            if b.relay(stream).is_err() {
                eprintln!("TUNNEL_REQUEST_REJECTED");
            }
        }
    });
    for stream in listener.incoming() {
        let stream = stream?;
        stream.set_read_timeout(Some(Duration::from_secs(30)))?;
        stream.set_write_timeout(Some(Duration::from_secs(30)))?;
        let source = stream.peer_addr()?.ip();
        if api::post(
            &format!("{}/auth-check", broker.config.runtime_api_url),
            &json!({"source_ip":source.to_string()}),
            Some(&broker.token),
        )
        .is_err()
        {
            continue;
        }
        let _transition = broker.transition.lock().map_err(|_| "transition mutex")?;
        broker.negotiating.store(true, Ordering::Release);
        let result = match acceptor.accept(stream) {
            Ok(tls) => broker.establish(tls),
            Err(e) => {
                if let openssl::ssl::HandshakeError::Failure(ref failed) = e {
                    if failed
                        .error()
                        .ssl_error()
                        .map(|stack| {
                            stack.errors().iter().any(|e| {
                                matches!(
                                    e.reason(),
                                    Some(
                                        "certificate verify failed"
                                            | "bad signature"
                                            | "peer did not return a certificate"
                                            | "tlsv1 alert unknown ca"
                                            | "tlsv1 alert bad certificate"
                                    )
                                )
                            })
                        })
                        .unwrap_or(false)
                    {
                        broker.auth_failure(source);
                    }
                }
                Err("TLS peer rejected".into())
            }
        };
        broker.negotiating.store(false, Ordering::Release);
        if result.is_err() {
            eprintln!("BROKER_CONNECTION_REJECTED");
        }
    }
    Ok(())
}
fn main() {
    let a: Vec<_> = std::env::args().collect();
    if a.len() != 2 || run(Path::new(&a[1])).is_err() {
        eprintln!("BROKER_FAILED");
        std::process::exit(1)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn duplicate_length_rejected() {
        let mut b = &b"POST /a HTTP/1.1\r\nContent-Length: 2\r\ncontent-length: 2\r\n\r\n{}"[..];
        assert!(http_request(&mut b).is_err());
    }
}
