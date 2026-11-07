//! Test-only fixed public fixtures. Never use these keys for a deployment.
use migration_connector::{crypto, protocol, Result};
use serde_json::{json, Value};
use std::io::Read;
fn main() -> Result<()> {
    let mut b = String::new();
    std::io::stdin().take(65537).read_to_string(&mut b)?;
    if b.len() > 65536 {
        return Err("input too large".into());
    }
    let v: Value = serde_json::from_str(&b)?;
    let seed = v["seed"].as_u64().unwrap_or(7);
    if seed != 7 && seed != 8 {
        return Err("test seed must be 7 or 8".into());
    }
    let key = crypto::seed_key(&[seed as u8; 32])?;
    let purpose = v["purpose"].as_str().ok_or("purpose")?;
    if !purpose.starts_with("migration/") {
        return Err("test purpose".into());
    }
    println!(
        "{}",
        json!({"public_key":crypto::b64(&key.raw_public_key()?),"signature":crypto::sign(&key,&protocol::signed_bytes(purpose,&v["payload"])?)?})
    );
    Ok(())
}
