//! Narrow root helper protocol. Policy is provisioned by an administrator, never by a remote job.
use crate::Result;
use serde::{Deserialize, Serialize};
use std::{
    io::Write,
    net::{IpAddr, SocketAddr, UdpSocket},
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use zeroize::Zeroizing;
#[derive(Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub uid: u32,
    pub interface: String,
    pub local_address: IpAddr,
    pub peer_address: IpAddr,
    pub endpoint: SocketAddr,
    pub listen_port: u16,
    pub job_port: u16,
    pub probe_port: u16,
    pub role: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub operation: String,
    pub connection_id: String,
    pub epoch: u64,
    pub private_key: Option<String>,
    pub public_key: Option<String>,
    pub psk: Option<String>,
    #[serde(default)]
    pub key_expires_at_ms: u64,
}
impl Drop for Request {
    fn drop(&mut self) {
        use zeroize::Zeroize;
        self.private_key.zeroize();
        self.psk.zeroize();
    }
}
impl Policy {
    pub fn validate(&self) -> Result<()> {
        if self.local_address.is_unspecified()
            || self.local_address.is_loopback()
            || self.local_address.is_multicast()
            || self.peer_address.is_unspecified()
            || self.peer_address.is_loopback()
            || self.peer_address.is_multicast()
            || self.endpoint.ip().is_unspecified()
            || self.endpoint.ip().is_loopback()
            || self.endpoint.ip().is_multicast()
            || self.job_port == self.probe_port
            || !self.interface.starts_with("mc")
            || self.interface.len() > 15
            || !self.interface.bytes().all(|c| c.is_ascii_alphanumeric())
            || self.local_address == self.peer_address
            || self.local_address.is_ipv4() != self.peer_address.is_ipv4()
            || self.endpoint.ip() == self.peer_address
            || self.endpoint.ip() == self.local_address
            || self.job_port < 1024
            || self.probe_port < 1024
            || self.listen_port < 1024
            || !matches!(self.role.as_str(), "client" | "gateway")
        {
            return Err("invalid network policy".into());
        }
        Ok(())
    }
}
pub fn helper(path: &Path, req: &Request) -> Result<serde_json::Value> {
    let mut cmd = if unsafe { libc::geteuid() } == 0 {
        Command::new(path)
    } else {
        let mut c = Command::new("/usr/bin/sudo");
        c.arg("-n").arg(path);
        c
    };
    let mut child = cmd
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let data = Zeroizing::new(serde_json::to_vec(req)?);
    child
        .stdin
        .take()
        .ok_or("stdin unavailable")?
        .write_all(&data)?;
    let out = child.wait_with_output()?;
    if !out.status.success() {
        return Err("network helper rejected operation".into());
    }
    Ok(serde_json::from_slice(&out.stdout)?)
}
pub fn probe(policy: &Policy, nonce: &str) -> Result<()> {
    let socket = UdpSocket::bind(SocketAddr::new(policy.local_address, policy.probe_port))?;
    socket.set_read_timeout(Some(Duration::from_millis(500)))?;
    let peer = SocketAddr::new(policy.peer_address, policy.probe_port);
    let deadline = Instant::now() + Duration::from_secs(20);
    let mine = format!("migration-probe/1:{nonce}:{}", policy.role);
    let expected = format!(
        "migration-probe/1:{nonce}:{}",
        if policy.role == "client" {
            "gateway"
        } else {
            "client"
        }
    );
    let mut seen = false;
    let mut ack = false;
    let mut b = [0u8; 256];
    while Instant::now() < deadline {
        socket.send_to(mine.as_bytes(), peer)?;
        if seen {
            socket.send_to(format!("ack:{expected}").as_bytes(), peer)?;
        }
        if let Ok((n, addr)) = socket.recv_from(&mut b) {
            if addr != peer {
                continue;
            }
            if &b[..n] == expected.as_bytes() {
                seen = true;
            }
            if &b[..n] == format!("ack:{mine}").as_bytes() {
                ack = true;
            }
            if seen && ack {
                socket.send_to(format!("ack:{expected}").as_bytes(), peer)?;
                return Ok(());
            }
        }
    }
    Err("bidirectional tunnel probe failed".into())
}
