use crate::{crypto, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{Read, Write};
pub const ALPN: &[u8] = b"migration-key-broker/1";
pub const MAX_FRAME: usize = 65536;
pub fn canonical<T: Serialize>(v: &T) -> Result<Vec<u8>> {
    Ok(serde_jcs::to_vec(v)?)
}
pub fn signed_bytes<T: Serialize>(domain: &str, v: &T) -> Result<Vec<u8>> {
    let mut b = format!("{domain}\n").into_bytes();
    b.extend(canonical(v)?);
    Ok(b)
}
pub fn send<T: Serialize>(io: &mut impl Write, v: &T) -> Result<()> {
    let b = canonical(v)?;
    if b.len() > MAX_FRAME {
        return Err("frame too large".into());
    }
    io.write_all(&(b.len() as u32).to_be_bytes())?;
    io.write_all(&b)?;
    io.flush()?;
    Ok(())
}
pub fn recv<T: serde::de::DeserializeOwned>(io: &mut impl Read) -> Result<T> {
    let mut len = [0; 4];
    io.read_exact(&mut len)?;
    let n = u32::from_be_bytes(len) as usize;
    if n == 0 || n > MAX_FRAME {
        return Err("frame size rejected".into());
    }
    let mut b = vec![0; n];
    io.read_exact(&mut b)?;
    Ok(serde_json::from_slice(&b)?)
}
#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub version: u8,
    pub tenant_id: String,
    pub device_id: String,
    pub gateway_id: String,
    pub connection_id: String,
    pub epoch: u64,
    pub client_wireguard_public_key: String,
    pub gateway_wireguard_public_key: String,
    pub client_tunnel_address: String,
    pub gateway_tunnel_address: String,
    pub allowed_services_digest: String,
    pub expires_at_ms: u64,
}
impl Binding {
    pub fn validate(&self, device: &str, public: &str, last_epoch: u64) -> Result<()> {
        if self.version != 1
            || self.device_id != device
            || self.client_wireguard_public_key != public
            || self.epoch <= last_epoch
            || self.expires_at_ms <= crate::now_ms()
            || self.expires_at_ms > crate::now_ms() + 1800000
        {
            return Err("binding rejected".into());
        }
        self.client_tunnel_address.parse::<std::net::IpAddr>()?;
        self.gateway_tunnel_address.parse::<std::net::IpAddr>()?;
        if self.client_tunnel_address == self.gateway_tunnel_address {
            return Err("addresses collide".into());
        }
        Ok(())
    }
}
pub fn verify_job(
    job: &Value,
    public: &str,
    request: &str,
    device: &str,
    epoch: u64,
) -> Result<&'static str> {
    let p = &job["payload"];
    if p["request_id"] != request
        || p["device_id"] != device
        || p["epoch"].as_u64() != Some(epoch)
        || p["expires_at_ms"].as_u64().unwrap_or(0) <= crate::now_ms()
    {
        return Err("job binding expired or mismatched".into());
    }
    let key = crypto::public(public)?;
    crypto::verify(
        &key,
        &signed_bytes("migration/job/v1", p)?,
        job["signature"].as_str().ok_or("missing signature")?,
    )?;
    match p["operation"].as_str() {
        Some("environment.discover") => Ok("discover"),
        Some("environment.inspect") => Ok("collect"),
        Some("environment.observe") => Ok("observe"),
        _ => Err("operation forbidden".into()),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn oversized_frame() {
        let b = (65537u32).to_be_bytes();
        assert!(recv::<Value>(&mut &b[..]).is_err());
    }
    #[test]
    fn canonical_matches_contract() {
        assert_eq!(
            canonical(&serde_json::json!({"z":1,"a":"한"})).unwrap(),
            "{\"a\":\"한\",\"z\":1}".as_bytes()
        );
    }
}
