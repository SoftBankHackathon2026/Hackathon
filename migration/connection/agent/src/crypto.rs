//! All cryptographic operations terminate in OpenSSL, not shell commands.
use crate::Result;
use base64::{
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
    Engine,
};
use openssl::{
    asn1::Asn1Time,
    bn::BigNum,
    hash::MessageDigest,
    pkey::{KeyType, PKey, PKeyRef, Private, Public},
    sign::{Signer, Verifier},
    x509::{X509NameBuilder, X509},
};
use zeroize::Zeroizing;
pub fn b64(v: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(v)
}
pub fn decode(s: &str, len: usize) -> Result<Vec<u8>> {
    let v = URL_SAFE_NO_PAD.decode(s)?;
    if v.len() != len || b64(&v) != s {
        return Err("invalid key encoding".into());
    }
    Ok(v)
}
pub fn random<const N: usize>() -> Result<[u8; N]> {
    let mut b = [0; N];
    openssl::rand::rand_bytes(&mut b)?;
    Ok(b)
}
pub fn seed_key(seed: &[u8]) -> Result<PKey<Private>> {
    if seed.len() != 32 {
        return Err("invalid seed".into());
    }
    Ok(PKey::private_key_from_seed(
        None,
        KeyType::ML_DSA_65,
        None,
        seed,
    )?)
}
pub fn device_key() -> Result<PKey<Private>> {
    let seed = Zeroizing::new(random::<32>()?);
    seed_key(&seed[..])
}
pub fn public(s: &str) -> Result<PKey<Public>> {
    let raw = decode(s, 1952)?;
    let mut der = vec![
        0x30, 0x82, 0x07, 0xb2, 0x30, 0x0b, 0x06, 0x09, 0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04,
        0x03, 0x12, 0x03, 0x82, 0x07, 0xa1, 0,
    ];
    der.extend(raw);
    Ok(PKey::public_key_from_der(&der)?)
}
pub fn sign(key: &PKeyRef<Private>, data: &[u8]) -> Result<String> {
    let mut s = Signer::new_without_digest(key)?;
    Ok(b64(&s.sign_oneshot_to_vec(data)?))
}
pub fn verify(key: &PKeyRef<Public>, data: &[u8], sig: &str) -> Result<()> {
    let signature = decode(sig, 3309)?;
    let mut v = Verifier::new_without_digest(key)?;
    if !v.verify_oneshot(&signature, data)? {
        return Err("signature rejected".into());
    }
    Ok(())
}
pub fn digest(data: &[u8]) -> String {
    openssl::sha::sha384(data)
        .iter()
        .map(|v| format!("{v:02x}"))
        .collect()
}
pub fn wg_key() -> Result<(Zeroizing<String>, String)> {
    let key = PKey::generate_x25519()?;
    Ok((
        Zeroizing::new(STANDARD.encode(key.raw_private_key()?)),
        b64(&key.raw_public_key()?),
    ))
}
pub fn certificate(key: &PKeyRef<Private>) -> Result<X509> {
    let mut n = X509NameBuilder::new()?;
    n.append_entry_by_text("CN", "migration-device")?;
    let n = n.build();
    let mut b = X509::builder()?;
    b.set_version(2)?;
    let serial = BigNum::from_slice(&random::<16>()?)?.to_asn1_integer()?;
    b.set_serial_number(&serial)?;
    b.set_subject_name(&n)?;
    b.set_issuer_name(&n)?;
    b.set_pubkey(key)?;
    let before = Asn1Time::from_unix((crate::now_ms() / 1000) as i64 - 30)?;
    let after = Asn1Time::from_unix((crate::now_ms() / 1000) as i64 + 86400 - 30)?;
    b.set_not_before(&before)?;
    b.set_not_after(&after)?;
    b.sign(key, MessageDigest::null())?;
    Ok(b.build())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn noble_fixture_verifies() {
        let v: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/noble-mldsa65.json")).unwrap();
        let k = seed_key(&[7; 32]).unwrap();
        assert_eq!(
            b64(&k.raw_public_key().unwrap()),
            v["public_key"].as_str().unwrap()
        );
        verify(
            &public(v["public_key"].as_str().unwrap()).unwrap(),
            v["message"].as_str().unwrap().as_bytes(),
            v["signature"].as_str().unwrap(),
        )
        .unwrap();
    }
    #[test]
    fn seed_roundtrip_and_tamper() {
        let k = seed_key(&[7; 32]).unwrap();
        let pk = b64(&k.raw_public_key().unwrap());
        assert_eq!(
            pk,
            b64(&seed_key(&[7; 32]).unwrap().raw_public_key().unwrap())
        );
        let sig = sign(&k, b"scope\nhello").unwrap();
        verify(&public(&pk).unwrap(), b"scope\nhello", &sig).unwrap();
        assert!(verify(&public(&pk).unwrap(), b"other\nhello", &sig).is_err());
    }
}
