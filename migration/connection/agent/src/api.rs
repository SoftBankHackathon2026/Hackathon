use crate::{crypto, protocol, Result};
use openssl::pkey::{PKeyRef, Private};
use serde_json::{json, Value};
use std::{io::Read, time::Duration};
pub fn http() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .redirects(0)
        .timeout(Duration::from_secs(15))
        .build()
}
pub fn parse(response: ureq::Response) -> Result<Value> {
    let mut b = Vec::new();
    response.into_reader().take(2_097_153).read_to_end(&mut b)?;
    if b.len() > 2_097_152 {
        return Err("response too large".into());
    }
    Ok(serde_json::from_slice(&b)?)
}
pub fn post(url: &str, body: &Value, token: Option<&str>) -> Result<Value> {
    let request = http().post(url);
    let request = if let Some(t) = token {
        request.set("Authorization", &format!("Bearer {t}"))
    } else {
        request
    };
    parse(request.send_json(body.clone())?)
}
pub fn get(url: &str, token: &str) -> Result<Value> {
    parse(
        http()
            .get(url)
            .set("Authorization", &format!("Bearer {token}"))
            .call()?,
    )
}
pub fn device(
    base: &str,
    request: &str,
    candidate: &str,
    key: &PKeyRef<Private>,
    action: &str,
    data: Value,
) -> Result<Value> {
    let challenge = post(
        &format!("{base}/{request}/device-challenge"),
        &json!({"candidateId":candidate}),
        None,
    )?;
    let p = json!({"version":1,"request_id":request,"candidate_id":candidate,"challenge_id":challenge["challenge_id"],"challenge":challenge["challenge"],"action":action,"data":data,"expires_at_ms":challenge["expires_at_ms"]});
    let signature = crypto::sign(
        key,
        &protocol::signed_bytes("migration/device-request/v1", &p)?,
    )?;
    post(
        &format!("{base}/{request}/device"),
        &json!({"payload":p,"signature":signature}),
        None,
    )
}
pub fn identifier(s: &str) -> Result<()> {
    if s.len() < 16
        || s.len() > 80
        || !s
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err("invalid identifier".into());
    }
    Ok(())
}
pub fn https(url: &str) -> Result<()> {
    let u = ureq::AgentBuilder::new();
    drop(u);
    if !url.starts_with("https://")
        || url.contains('@')
        || url.contains('#')
        || url.contains('?')
        || url.ends_with('/')
        || url.chars().any(|c| c.is_control() || c.is_whitespace())
    {
        return Err("HTTPS URL required".into());
    }
    Ok(())
}
