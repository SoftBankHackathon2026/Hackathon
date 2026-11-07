use crate::{
    crypto,
    protocol::{Binding, ALPN},
    Result,
};
use foreign_types::ForeignTypeRef;
use openssl::{
    pkey::{PKeyRef, Private},
    ssl::{
        AlpnError, SslAcceptor, SslConnector, SslContextBuilder, SslFiletype, SslMethod,
        SslOptions, SslSessionCacheMode, SslStream, SslVerifyMode, SslVersion,
    },
    x509::X509Ref,
};
use std::{ffi::CStr, net::TcpStream, path::Path};
use zeroize::Zeroizing;
unsafe extern "C" {
    fn SSL_get0_group_name(s: *mut openssl_sys::SSL) -> *const libc::c_char;
}
fn profile(c: &mut SslContextBuilder) -> Result<()> {
    if openssl::version::number() < 0x30500000 {
        return Err("OpenSSL 3.5+ required".into());
    }
    c.set_min_proto_version(Some(SslVersion::TLS1_3))?;
    c.set_max_proto_version(Some(SslVersion::TLS1_3))?;
    c.set_groups_list("X25519MLKEM768")?;
    c.set_sigalgs_list("mldsa65")?;
    c.set_options(SslOptions::NO_TICKET);
    c.set_session_cache_mode(SslSessionCacheMode::OFF);
    c.set_max_early_data(0)?;
    Ok(())
}
pub fn client(root: &Path, key: &PKeyRef<Private>, cert: &X509Ref) -> Result<SslConnector> {
    let mut c = SslConnector::builder(SslMethod::tls())?;
    profile(&mut c)?;
    c.set_ca_file(root)?;
    c.set_certificate(cert)?;
    c.set_private_key(key)?;
    c.check_private_key()?;
    c.set_verify(SslVerifyMode::PEER);
    let mut p = vec![ALPN.len() as u8];
    p.extend(ALPN);
    c.set_alpn_protos(&p)?;
    Ok(c.build())
}
pub fn server(cert: &Path, key: &Path) -> Result<SslAcceptor> {
    let mut c = SslAcceptor::mozilla_intermediate_v5(SslMethod::tls())?;
    profile(&mut c)?;
    c.set_certificate_chain_file(cert)?;
    c.set_private_key_file(key, SslFiletype::PEM)?;
    c.check_private_key()?;
    c.set_num_tickets(0)?;
    c.set_alpn_select_callback(|_, client| {
        openssl::ssl::select_next_proto(b"\x16migration-key-broker/1", client)
            .ok_or(AlpnError::ALERT_FATAL)
    });
    c.set_verify_callback(
        SslVerifyMode::PEER | SslVerifyMode::FAIL_IF_NO_PEER_CERT,
        |ok, ctx| {
            if ok {
                return true;
            }
            if ctx.error_depth() != 0 || ctx.error().as_raw() != 18 {
                return false;
            }
            ctx.current_cert()
                .and_then(|c| c.public_key().ok().map(|k| c.verify(&k).unwrap_or(false)))
                .unwrap_or(false)
        },
    );
    Ok(c.build())
}
pub fn enforce(s: &openssl::ssl::SslRef) -> Result<()> {
    let group = unsafe {
        let ptr = SSL_get0_group_name(s.as_ptr());
        if ptr.is_null() {
            return Err("missing group".into());
        }
        CStr::from_ptr(ptr).to_str()?
    };
    if group != "X25519MLKEM768"
        || s.version_str() != "TLSv1.3"
        || s.session_reused()
        || s.selected_alpn_protocol() != Some(ALPN)
    {
        return Err("TLS profile rejected".into());
    }
    Ok(())
}
pub fn exporter(s: &SslStream<TcpStream>, binding: &Binding) -> Result<Zeroizing<[u8; 32]>> {
    enforce(s.ssl())?;
    let context = openssl::sha::sha384(&crate::protocol::canonical(binding)?);
    let mut out = Zeroizing::new([0u8; 32]);
    s.ssl().export_keying_material(
        &mut out[..],
        "EXPORTER-migration-wireguard-psk-v1",
        Some(&context),
    )?;
    Ok(out)
}
#[derive(Debug)]
pub enum PeerCertificateError {
    Missing,
    Validity,
    Lifetime,
    Key,
    Inspection,
}

impl PeerCertificateError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Missing => "peer-cert-missing",
            Self::Validity => "peer-cert-validity",
            Self::Lifetime => "peer-cert-lifetime",
            Self::Key => "peer-cert-key",
            Self::Inspection => "peer-cert-inspection",
        }
    }
}

impl std::fmt::Display for PeerCertificateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.code())
    }
}

impl std::error::Error for PeerCertificateError {}

pub fn peer_public(s: &SslStream<TcpStream>) -> std::result::Result<String, PeerCertificateError> {
    use PeerCertificateError::{Inspection, Key, Lifetime, Missing, Validity};
    let cert = s.ssl().peer_certificate().ok_or(Missing)?;
    let now = openssl::asn1::Asn1Time::from_unix((crate::now_ms() / 1000) as i64)
        .map_err(|_| Inspection)?;
    if cert.not_before() > now || cert.not_after() < now {
        return Err(Validity);
    }
    let duration = cert
        .not_before()
        .diff(cert.not_after())
        .map_err(|_| Inspection)?;
    if i64::from(duration.days) * 86400 + i64::from(duration.secs) > 86400 {
        return Err(Lifetime);
    }
    let key = cert.public_key().map_err(|_| Key)?;
    let raw = key.raw_public_key().map_err(|_| Key)?;
    if raw.len() != 1952 {
        return Err(Key);
    }
    Ok(crypto::b64(&raw))
}
