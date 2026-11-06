//! Signed release installation. Trust anchors are embedded at build time.
use crate::{crypto, protocol, Result};
use openssl::{
    pkey::{Id, PKey},
    sign::Verifier,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
        io::AsRawFd,
    },
    path::{Path, PathBuf},
    time::Duration,
};

const MAX_MANIFEST: u64 = 65536;
const MAX_ARTIFACT: u64 = 256 * 1024 * 1024;
const MAX_TOTAL: u64 = 512 * 1024 * 1024;
const SAFE_INTEGER: u64 = 9_007_199_254_740_991;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub name: String,
    pub sha384: String,
    pub size: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub version: u8,
    pub sequence: u64,
    pub expires_at_ms: u64,
    pub artifacts: Vec<Artifact>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct SignedManifest {
    payload: Manifest,
    mldsa_signature: String,
    ed25519_signature: String,
}

struct Trust {
    mldsa: String,
    ed25519: String,
    minimum_sequence: u64,
}
impl Trust {
    fn embedded() -> Result<Self> {
        Ok(Self {
            mldsa: option_env!("MIGRATION_RELEASE_MLDSA_PUBLIC_KEY")
                .ok_or("release ML-DSA trust anchor not configured")?
                .into(),
            ed25519: option_env!("MIGRATION_RELEASE_ED25519_PUBLIC_KEY")
                .ok_or("release Ed25519 trust anchor not configured")?
                .into(),
            minimum_sequence: option_env!("MIGRATION_MIN_RELEASE_SEQUENCE")
                .ok_or("release minimum sequence not configured")?
                .parse()?,
        })
    }
}

fn safe_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name != "."
        && name != ".."
        && name.as_bytes()[0].is_ascii_alphanumeric()
        && name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}

fn check_manifest(bytes: &[u8], trust: &Trust, now: u64) -> Result<Manifest> {
    if bytes.len() > MAX_MANIFEST as usize {
        return Err("release manifest too large".into());
    }
    let signed: SignedManifest = serde_json::from_slice(bytes)?;
    let manifest = &signed.payload;
    if manifest.version != 1
        || manifest.sequence < trust.minimum_sequence
        || manifest.sequence > SAFE_INTEGER
        || manifest.expires_at_ms <= now
        || manifest.expires_at_ms > SAFE_INTEGER
    {
        return Err("release version, sequence or expiry rejected".into());
    }
    if manifest.artifacts.is_empty() || manifest.artifacts.len() > 32 {
        return Err("release artifact count rejected".into());
    }
    let mut seen = HashSet::new();
    let mut total = 0;
    for artifact in &manifest.artifacts {
        if !safe_name(&artifact.name)
            || !seen.insert(artifact.name.as_str())
            || artifact.name == "manifest.json"
            || artifact.size == 0
            || artifact.size > MAX_ARTIFACT
            || artifact.sha384.len() != 96
            || !artifact
                .sha384
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        {
            return Err("release artifact specification rejected".into());
        }
        total += artifact.size;
    }
    if total > MAX_TOTAL
        || [
            "migration-connector",
            "connector.json",
            "root-ca.pem",
            "inventory.pyz",
        ]
        .iter()
        .any(|name| !seen.contains(name))
    {
        return Err("release bundle incomplete or too large".into());
    }
    let message = protocol::signed_bytes("migration/release/v1", manifest)?;
    let mldsa_public = crypto::public(&trust.mldsa)?;
    crypto::verify(&mldsa_public, &message, &signed.mldsa_signature)?;
    let public =
        PKey::public_key_from_raw_bytes(&crypto::decode(&trust.ed25519, 32)?, Id::ED25519)?;
    let signature = crypto::decode(&signed.ed25519_signature, 64)?;
    if !Verifier::new_without_digest(&public)?.verify_oneshot(&signature, &message)? {
        return Err("release Ed25519 signature rejected".into());
    }
    Ok(signed.payload)
}

fn private_directory(path: &Path) -> Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir()
        || meta.file_type().is_symlink()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0
    {
        return Err("release directory must be owned and private".into());
    }
    Ok(())
}

fn read_owned(path: &Path, limit: u64) -> Result<Vec<u8>> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o022 != 0
        || meta.len() > limit
    {
        return Err("release file rejected".into());
    }
    let mut bytes = Vec::new();
    (&mut file).take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err("release file too large".into());
    }
    Ok(bytes)
}

fn create_private(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

fn sequence_path() -> Result<PathBuf> {
    let home = std::env::var_os("HOME").ok_or("home directory unavailable")?;
    let path = PathBuf::from(home).join(".local/state/migration-connector");
    private_directory(&path)?;
    Ok(path)
}

fn record_sequence(manifest: &Manifest) -> Result<()> {
    let state = sequence_path()?;
    record_sequence_at(&state, manifest)
}

fn record_sequence_at(state: &Path, manifest: &Manifest) -> Result<()> {
    private_directory(state)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(state.join("release-sequence.lock"))?;
    let meta = lock.metadata()?;
    if !meta.is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0
    {
        return Err("release sequence lock rejected".into());
    }
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err("another release update is in progress".into());
    }
    let floor_path = state.join("release-sequence");
    let floor = match read_owned(&floor_path, 32) {
        Ok(bytes) => std::str::from_utf8(&bytes)?.trim().parse::<u64>()?,
        Err(error)
            if error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
        {
            0
        }
        Err(error) => return Err(error),
    };
    if manifest.sequence < floor {
        return Err("release rollback rejected".into());
    }
    let temporary = state.join(format!(
        ".release-sequence-{}",
        crypto::b64(&crypto::random::<16>()?)
    ));
    create_private(&temporary, manifest.sequence.to_string().as_bytes())?;
    if let Err(error) = fs::rename(&temporary, &floor_path) {
        let _ = fs::remove_file(&temporary);
        return Err(error.into());
    }
    File::open(&state)?.sync_all()?;
    Ok(())
}

pub fn verify_bundle(bundle: &Path) -> Result<Manifest> {
    private_directory(bundle)?;
    let manifest = check_manifest(
        &read_owned(&bundle.join("manifest.json"), MAX_MANIFEST)?,
        &Trust::embedded()?,
        crate::now_ms(),
    )?;
    for artifact in &manifest.artifacts {
        let bytes = read_owned(&bundle.join(&artifact.name), artifact.size)?;
        if bytes.len() as u64 != artifact.size || crypto::digest(&bytes) != artifact.sha384 {
            return Err("release artifact integrity rejected".into());
        }
    }
    record_sequence(&manifest)?;
    Ok(manifest)
}

fn base_url(base: &str) -> Result<String> {
    let tail = base
        .strip_prefix("https://")
        .ok_or("release URL must use HTTPS")?;
    let authority = tail.split('/').next().unwrap_or("");
    if authority.is_empty()
        || base.bytes().any(|b| b <= 32 || b >= 127)
        || base.contains(['@', '?', '#', '\\'])
        || tail.split('/').any(|part| part == ".." || part == ".")
    {
        return Err("release URL rejected".into());
    }
    Ok(base.trim_end_matches('/').to_owned())
}

fn download(agent: &ureq::Agent, url: &str, limit: u64) -> Result<Vec<u8>> {
    let response = agent
        .get(url)
        .call()
        .map_err(|_| "release download failed")?;
    if response.status() != 200 {
        return Err("release download status rejected".into());
    }
    let mut bytes = Vec::new();
    response
        .into_reader()
        .take(limit + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err("release download too large".into());
    }
    Ok(bytes)
}

pub fn install_release(base: &str, bundle: &Path) -> Result<Manifest> {
    private_directory(bundle)?;
    if fs::read_dir(bundle)?.next().is_some() {
        return Err("release target directory must be empty".into());
    }
    let base = base_url(base)?;
    let agent = ureq::AgentBuilder::new()
        .tls_connector(std::sync::Arc::new(native_tls::TlsConnector::new()?))
        .redirects(0)
        .timeout(Duration::from_secs(20))
        .build();
    let manifest_bytes = download(&agent, &format!("{base}/manifest.json"), MAX_MANIFEST)?;
    let manifest = check_manifest(&manifest_bytes, &Trust::embedded()?, crate::now_ms())?;
    for artifact in &manifest.artifacts {
        let bytes = download(&agent, &format!("{base}/{}", artifact.name), artifact.size)?;
        if bytes.len() as u64 != artifact.size || crypto::digest(&bytes) != artifact.sha384 {
            return Err("release download integrity rejected".into());
        }
        create_private(&bundle.join(&artifact.name), &bytes)?;
    }
    create_private(&bundle.join("manifest.json"), &manifest_bytes)?;
    verify_bundle(bundle)
}

#[cfg(test)]
mod tests {
    use super::*;
    use openssl::sign::Signer;

    fn fixture() -> (SignedManifest, Trust) {
        let ml = crypto::seed_key(&[8; 32]).unwrap();
        let ed = PKey::generate_ed25519().unwrap();
        let payload = Manifest {
            version: 1,
            sequence: 7,
            expires_at_ms: 2000,
            artifacts: [
                "migration-connector",
                "connector.json",
                "root-ca.pem",
                "inventory.pyz",
            ]
            .iter()
            .map(|name| Artifact {
                name: (*name).into(),
                sha384: crypto::digest(b"{}"),
                size: 2,
            })
            .collect(),
        };
        let trust = Trust {
            mldsa: crypto::b64(&ml.raw_public_key().unwrap()),
            ed25519: crypto::b64(&ed.raw_public_key().unwrap()),
            minimum_sequence: 7,
        };
        let bytes = protocol::signed_bytes("migration/release/v1", &payload).unwrap();
        let signed = SignedManifest {
            payload,
            mldsa_signature: crypto::sign(&ml, &bytes).unwrap(),
            ed25519_signature: crypto::b64(
                &Signer::new_without_digest(&ed)
                    .unwrap()
                    .sign_oneshot_to_vec(&bytes)
                    .unwrap(),
            ),
        };
        (signed, trust)
    }
    #[test]
    fn both_signatures_required_and_expiry_checked() {
        let (mut signed, trust) = fixture();
        assert_eq!(
            check_manifest(&serde_json::to_vec(&signed).unwrap(), &trust, 1000)
                .unwrap()
                .sequence,
            7
        );
        assert!(check_manifest(&serde_json::to_vec(&signed).unwrap(), &trust, 2000).is_err());
        signed.ed25519_signature = crypto::b64(&[0; 64]);
        assert!(check_manifest(&serde_json::to_vec(&signed).unwrap(), &trust, 1000).is_err());
        let (mut signed, trust) = fixture();
        signed.mldsa_signature = crypto::b64(&[0; 3309]);
        assert!(check_manifest(&serde_json::to_vec(&signed).unwrap(), &trust, 1000).is_err());
    }
    #[test]
    fn tampering_paths_and_downgrade_rejected() {
        let (mut signed, trust) = fixture();
        signed.payload.sequence = 6;
        assert!(check_manifest(&serde_json::to_vec(&signed).unwrap(), &trust, 1000).is_err());
        signed.payload.sequence = 7;
        signed.payload.artifacts[0].name = "../connector.json".into();
        assert!(check_manifest(&serde_json::to_vec(&signed).unwrap(), &trust, 1000).is_err());
        assert!(!safe_name("a/b"));
        assert!(!safe_name(".."));
        assert!(base_url("http://example.com").is_err());
        assert!(base_url("https://user@example.com").is_err());
        assert!(base_url("https://example.com/../x").is_err());
    }
    #[test]
    fn duplicate_fields_rejected() {
        let (signed, trust) = fixture();
        let bytes = serde_json::to_string(&signed)
            .unwrap()
            .replace("\"version\":1", "\"version\":1,\"version\":1");
        assert!(check_manifest(bytes.as_bytes(), &trust, 1000).is_err());
    }
    #[test]
    fn symlinks_and_writable_files_rejected() {
        let directory = std::env::temp_dir().join(format!(
            "migration-release-test-{}",
            crypto::b64(&crypto::random::<16>().unwrap())
        ));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        create_private(&directory.join("safe"), b"hello").unwrap();
        std::os::unix::fs::symlink(directory.join("safe"), directory.join("linked")).unwrap();
        assert!(read_owned(&directory.join("linked"), 16).is_err());
        fs::set_permissions(directory.join("safe"), fs::Permissions::from_mode(0o666)).unwrap();
        assert!(read_owned(&directory.join("safe"), 16).is_err());
        fs::remove_dir_all(directory).unwrap();
    }
    #[test]
    fn previous_release_sequence_is_persistent() {
        let directory = std::env::temp_dir().join(format!(
            "migration-sequence-test-{}",
            crypto::b64(&crypto::random::<16>().unwrap())
        ));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let (signed, _) = fixture();
        let mut manifest = signed.payload;
        record_sequence_at(&directory, &manifest).unwrap();
        manifest.sequence = 6;
        assert!(record_sequence_at(&directory, &manifest).is_err());
        manifest.sequence = 8;
        record_sequence_at(&directory, &manifest).unwrap();
        assert_eq!(
            fs::read_to_string(directory.join("release-sequence")).unwrap(),
            "8"
        );
        fs::remove_dir_all(directory).unwrap();
    }
}
