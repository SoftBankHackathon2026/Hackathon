//! Installed root-owned at /usr/local/libexec/migration-net-helper; no command-line arguments.
use base64::{engine::general_purpose::STANDARD, Engine};
use migration_connector::{
    network::{Policy, Request},
    Result,
};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt},
        io::AsRawFd,
    },
    path::Path,
    process::{Command, Stdio},
};
use zeroize::Zeroizing;
const POLICY: &str = "/etc/migration-connector/net-policy.json";
const STATE: &str = "/run/migration-connector-network.json";
#[derive(Serialize, Deserialize)]
struct State {
    connection_id: String,
    epoch: u64,
    public_key: String,
    key_expires_at_ms: u64,
}
fn trusted(path: &str) -> Result<Vec<u8>> {
    let m = fs::symlink_metadata(path)?;
    if !m.is_file() || m.uid() != 0 || m.mode() & 0o022 != 0 {
        return Err("untrusted root file".into());
    }
    let f = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let mut b = Vec::new();
    f.take(65537).read_to_end(&mut b)?;
    if b.len() > 65536 {
        return Err("file size".into());
    }
    Ok(b)
}
fn cmd(bin: &str, args: &[&str], input: Option<&[u8]>) -> Result<Vec<u8>> {
    let mut p = Command::new(bin)
        .args(args)
        .env_clear()
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    if let Some(b) = input {
        p.stdin.take().ok_or("stdin")?.write_all(b)?;
    }
    let o = p.wait_with_output()?;
    if !o.status.success() {
        return Err("network operation failed".into());
    }
    Ok(o.stdout)
}
fn key(v: &Option<String>) -> Result<&str> {
    let s = v.as_deref().ok_or("missing key")?;
    if STANDARD.decode(s)?.len() != 32 {
        return Err("bad key".into());
    }
    Ok(s)
}
fn state() -> Result<State> {
    Ok(serde_json::from_slice(&trusted(STATE)?)?)
}
fn nft(p: &Policy, up: bool, lifetime_ms: u64) -> Result<()> {
    let table = format!("migration_{}", p.interface);
    let family = if p.peer_address.is_ipv4() {
        "ip"
    } else {
        "ip6"
    };
    let typ = if p.peer_address.is_ipv4() {
        "ipv4_addr"
    } else {
        "ipv6_addr"
    };
    if !up {
        let _ = cmd("/usr/sbin/nft", &["delete", "table", "inet", &table], None);
        return Ok(());
    }
    let _ = cmd("/usr/sbin/nft", &["delete", "table", "inet", &table], None);
    let rules=format!("add table inet {table}\nadd set inet {table} lease {{ type {typ}; flags timeout; timeout 60s; }}\nadd element inet {table} lease {{ {} timeout {lifetime_ms}ms }}\nadd chain inet {table} incoming {{ type filter hook input priority -10; policy accept; }}\nadd chain inet {table} outgoing {{ type filter hook output priority -10; policy accept; }}\nadd rule inet {table} incoming iifname \"{}\" {family} saddr @lease udp dport {} accept\nadd rule inet {table} incoming iifname \"{}\" {family} saddr @lease tcp dport {} accept\nadd rule inet {table} incoming iifname \"{}\" {family} saddr @lease tcp sport {} ct state established accept\nadd rule inet {table} incoming iifname \"{}\" drop\nadd rule inet {table} outgoing oifname \"{}\" {family} daddr @lease udp dport {} accept\nadd rule inet {table} outgoing oifname \"{}\" {family} daddr @lease tcp dport {} accept\nadd rule inet {table} outgoing oifname \"{}\" {family} daddr @lease tcp sport {} ct state established accept\nadd rule inet {table} outgoing oifname \"{}\" drop\n",p.peer_address,p.interface,p.probe_port,p.interface,p.job_port,p.interface,p.job_port,p.interface,p.interface,p.probe_port,p.interface,p.job_port,p.interface,p.job_port,p.interface);
    let rules=format!("{rules}add chain inet {table} forwarding {{ type filter hook forward priority -10; policy accept; }}\nadd rule inet {table} forwarding iifname \"{}\" drop\nadd rule inet {table} forwarding oifname \"{}\" drop\n",p.interface,p.interface);
    cmd("/usr/sbin/nft", &["-f", "-"], Some(rules.as_bytes()))?;
    Ok(())
}
fn remove(p: &Policy) -> Result<()> {
    let table = format!("migration_{}", p.interface);
    let _ = cmd(
        "/usr/sbin/nft",
        &["flush", "set", "inet", &table, "lease"],
        None,
    );
    let deleted = cmd(
        "/usr/sbin/ip",
        &["link", "delete", "dev", &p.interface],
        None,
    );
    if deleted.is_err() {
        if !Path::new("/sys/class/net").is_dir()
            || Path::new("/sys/class/net").join(&p.interface).exists()
        {
            return Err("interface removal unconfirmed; firewall retained".into());
        }
    }
    nft(p, false, 0)?;
    let _ = fs::remove_file(STATE);
    Ok(())
}
fn execute(p: &Policy, r: &Request) -> Result<serde_json::Value> {
    p.validate()?;
    if r.connection_id.len() < 16
        || r.connection_id.len() > 64
        || !r
            .connection_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err("bad connection identifier".into());
    }
    match r.operation.as_str() {
        "check" => {
            let status = fs::read_to_string("/proc/self/status")?;
            let cap = status
                .lines()
                .find_map(|l| l.strip_prefix("CapEff:"))
                .ok_or("capabilities unavailable")?;
            let cap = u64::from_str_radix(cap.trim(), 16)?;
            if cap & (1 << 12) == 0 {
                return Err("CAP_NET_ADMIN required".into());
            }
            cmd("/usr/bin/wg", &["--version"], None)?;
            cmd("/usr/sbin/nft", &["list", "tables"], None)?;
            Ok(serde_json::json!({"ready":true,"policy":p}))
        }
        "reset" => {
            if let Ok(_) = state() {
                remove(p)?;
            } else if Path::new(STATE).exists()
                || Path::new("/sys/class/net").join(&p.interface).exists()
            {
                return Err("unowned network state".into());
            }
            Ok(serde_json::json!({"removed":true}))
        }
        "configure" => {
            let now = migration_connector::now_ms();
            if r.key_expires_at_ms <= now || r.key_expires_at_ms > now + 1_800_000 {
                return Err("key expiry rejected".into());
            }
            let private = key(&r.private_key)?;
            let peer = key(&r.public_key)?;
            let psk = key(&r.psk)?;
            if let Ok(s) = state() {
                if s.connection_id != r.connection_id || r.epoch <= s.epoch {
                    return Err("old epoch or other connection".into());
                }
                remove(p)?;
            } else if Path::new(STATE).exists() {
                return Err("invalid existing state".into());
            } else if cmd("/usr/sbin/ip", &["link", "show", "dev", &p.interface], None).is_ok() {
                return Err("unowned interface exists".into());
            }
            let endpoint = p.endpoint.to_string();
            let route = cmd(
                "/usr/sbin/ip",
                &["route", "get", &p.endpoint.ip().to_string()],
                None,
            )?;
            if String::from_utf8_lossy(&route).contains(&p.interface) {
                return Err("recursive endpoint route".into());
            }
            let prefix = if p.local_address.is_ipv4() { 32 } else { 128 };
            let local = format!("{}/{prefix}", p.local_address);
            let remote = format!("{}/{prefix}", p.peer_address);
            cmd(
                "/usr/sbin/ip",
                &["link", "add", "dev", &p.interface, "type", "wireguard"],
                None,
            )?;
            let result = (|| -> Result<()> {
                let lifetime_ms = r
                    .key_expires_at_ms
                    .saturating_sub(migration_connector::now_ms())
                    .min(60000);
                if lifetime_ms == 0 {
                    return Err("key expired".into());
                }
                nft(p, true, lifetime_ms)?;
                let conf=Zeroizing::new(format!("[Interface]\nPrivateKey = {private}\nListenPort = {}\n[Peer]\nPublicKey = {peer}\nPresharedKey = {psk}\nAllowedIPs = {remote}\nEndpoint = {endpoint}\nPersistentKeepalive = 20\n",p.listen_port));
                cmd(
                    "/usr/bin/wg",
                    &["setconf", &p.interface, "/dev/stdin"],
                    Some(conf.as_bytes()),
                )?;
                cmd(
                    "/usr/sbin/ip",
                    &["address", "add", &local, "dev", &p.interface],
                    None,
                )?;
                cmd(
                    "/usr/sbin/ip",
                    &["link", "set", "dev", &p.interface, "up"],
                    None,
                )?;
                cmd(
                    "/usr/sbin/ip",
                    &["route", "add", &remote, "dev", &p.interface],
                    None,
                )?;
                let mut f = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .custom_flags(libc::O_NOFOLLOW)
                    .open(STATE)?;
                f.write_all(&serde_json::to_vec(&State {
                    connection_id: r.connection_id.clone(),
                    epoch: r.epoch,
                    public_key: peer.into(),
                    key_expires_at_ms: r.key_expires_at_ms,
                })?)?;
                f.sync_all()?;
                Ok(())
            })();
            if result.is_err() {
                remove(p)?;
            }
            result?;
            Ok(serde_json::json!({"configured":true}))
        }
        "refresh" | "status" | "remove" => {
            if r.operation == "remove" && !Path::new(STATE).exists() {
                if Path::new("/sys/class/net").join(&p.interface).exists() {
                    return Err("unowned interface exists".into());
                }
                remove(p)?;
                return Ok(serde_json::json!({"removed":true}));
            }
            let s = state()?;
            if s.connection_id != r.connection_id || s.epoch != r.epoch {
                return Err("state mismatch".into());
            }
            if r.operation == "remove" {
                remove(p)?;
                return Ok(serde_json::json!({"removed":true}));
            }
            if s.key_expires_at_ms <= migration_connector::now_ms() {
                remove(p)?;
                return Err("key expired".into());
            }
            if r.operation == "refresh" {
                let lifetime_ms = s
                    .key_expires_at_ms
                    .saturating_sub(migration_connector::now_ms())
                    .min(60000);
                if lifetime_ms == 0 {
                    remove(p)?;
                    return Err("key expired".into());
                }
                let table = format!("migration_{}", p.interface);
                let rules=format!("flush set inet {table} lease\nadd element inet {table} lease {{ {} timeout {lifetime_ms}ms }}\n",p.peer_address);
                cmd("/usr/sbin/nft", &["-f", "-"], Some(rules.as_bytes()))?;
            }
            let out = cmd(
                "/usr/bin/wg",
                &["show", &p.interface, "latest-handshakes"],
                None,
            )?;
            let time = String::from_utf8(out)?
                .lines()
                .find_map(|line| {
                    let mut x = line.split_whitespace();
                    if x.next() == Some(s.public_key.as_str()) {
                        x.next()?.parse::<u64>().ok()
                    } else {
                        None
                    }
                })
                .unwrap_or(0);
            Ok(serde_json::json!({"handshake_at":time,"epoch":s.epoch}))
        }
        _ => Err("operation forbidden".into()),
    }
}
fn run() -> Result<()> {
    if !cfg!(target_os = "linux") || unsafe { libc::geteuid() } != 0 || std::env::args().len() != 1
    {
        return Err("Linux root helper required".into());
    }
    let p: Policy = serde_json::from_slice(&trusted(POLICY)?)?;
    let uid = std::env::var("SUDO_UID")
        .ok()
        .and_then(|s| s.parse::<u32>().ok())
        .unwrap_or(0);
    if uid != 0 && uid != p.uid {
        return Err("caller rejected".into());
    }
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open("/run/migration-connector-network.lock")?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err("busy".into());
    }
    let mut input = Zeroizing::new(Vec::new());
    std::io::stdin().take(8193).read_to_end(&mut input)?;
    if input.len() > 8192 {
        return Err("request too large".into());
    }
    let r: Request = serde_json::from_slice(&input)?;
    let result = execute(&p, &r)?;
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}
fn main() {
    if run().is_err() {
        eprintln!("NETWORK_OPERATION_REJECTED");
        std::process::exit(1)
    }
}
