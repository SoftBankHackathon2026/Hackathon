use migration_connector::{crypto, protocol::Binding, transport};
use openssl::{
    asn1::Asn1Time,
    bn::BigNum,
    hash::MessageDigest,
    pkey::{PKey, Private},
    ssl::{SslConnector, SslMethod, SslVersion},
    x509::{
        extension::{BasicConstraints, ExtendedKeyUsage, KeyUsage, SubjectAlternativeName},
        X509NameBuilder, X509,
    },
};
use std::{
    fs,
    net::{TcpListener, TcpStream},
    thread,
    time::Duration,
};
fn cert(key: &PKey<Private>, ca: Option<(&X509, &PKey<Private>)>, server: bool) -> X509 {
    let mut n = X509NameBuilder::new().unwrap();
    n.append_entry_by_text("CN", if server { "localhost" } else { "test-root" })
        .unwrap();
    let name = n.build();
    let mut b = X509::builder().unwrap();
    b.set_version(2).unwrap();
    let serial = BigNum::from_slice(&crypto::random::<16>().unwrap())
        .unwrap()
        .to_asn1_integer()
        .unwrap();
    b.set_serial_number(&serial).unwrap();
    b.set_subject_name(&name).unwrap();
    b.set_issuer_name(ca.map(|c| c.0.subject_name()).unwrap_or(&name))
        .unwrap();
    b.set_pubkey(key).unwrap();
    let before = Asn1Time::from_unix((migration_connector::now_ms() / 1000) as i64 - 30).unwrap();
    let after = Asn1Time::from_unix((migration_connector::now_ms() / 1000) as i64 + 3600).unwrap();
    b.set_not_before(&before).unwrap();
    b.set_not_after(&after).unwrap();
    if ca.is_none() {
        b.append_extension(BasicConstraints::new().critical().ca().build().unwrap())
            .unwrap();
        b.append_extension(KeyUsage::new().key_cert_sign().crl_sign().build().unwrap())
            .unwrap();
    } else {
        b.append_extension(ExtendedKeyUsage::new().server_auth().build().unwrap())
            .unwrap();
        let san = SubjectAlternativeName::new()
            .dns("localhost")
            .build(&b.x509v3_context(ca.map(|x| x.0.as_ref()), None))
            .unwrap();
        b.append_extension(san).unwrap();
    }
    b.sign(ca.map(|c| c.1).unwrap_or(key), MessageDigest::null())
        .unwrap();
    b.build()
}
fn fixture() -> (std::path::PathBuf, PKey<Private>, X509) {
    let dir = std::env::temp_dir().join(format!(
        "migration-tls-test-{}",
        crypto::b64(&crypto::random::<12>().unwrap())
    ));
    fs::create_dir(&dir).unwrap();
    let root_key = crypto::device_key().unwrap();
    let root = cert(&root_key, None, false);
    let server_key = crypto::device_key().unwrap();
    let server = cert(&server_key, Some((&root, &root_key)), true);
    fs::write(dir.join("root.pem"), root.to_pem().unwrap()).unwrap();
    fs::write(dir.join("server.pem"), server.to_pem().unwrap()).unwrap();
    fs::write(
        dir.join("key.pem"),
        server_key.private_key_to_pem_pkcs8().unwrap(),
    )
    .unwrap();
    let client_key = crypto::device_key().unwrap();
    let client_cert = crypto::certificate(&client_key).unwrap();
    (dir, client_key, client_cert)
}
fn binding() -> Binding {
    Binding {
        version: 1,
        tenant_id: "test-tenant".into(),
        device_id: "test-device".into(),
        gateway_id: "test-gateway".into(),
        connection_id: "test-connection".into(),
        epoch: 1,
        client_wireguard_public_key: "test-client".into(),
        gateway_wireguard_public_key: "test-gateway".into(),
        client_tunnel_address: "10.200.0.2".into(),
        gateway_tunnel_address: "10.200.0.1".into(),
        allowed_services_digest: "test-digest".into(),
        expires_at_ms: 1700000000000,
    }
}
#[test]
fn mutual_mldsa_hybrid_tls_exporter_matches() {
    let (dir, key, client_cert) = fixture();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let acceptor = transport::server(&dir.join("server.pem"), &dir.join("key.pem")).unwrap();
    let expected = crypto::b64(&key.raw_public_key().unwrap());
    let server = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let tls = acceptor.accept(stream).unwrap();
        transport::enforce(tls.ssl()).unwrap();
        assert_eq!(transport::peer_public(&tls).unwrap(), expected);
        *transport::exporter(&tls, &binding()).unwrap()
    });
    let connector = transport::client(&dir.join("root.pem"), &key, &client_cert).unwrap();
    let tcp = TcpStream::connect(addr).unwrap();
    tcp.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let tls = connector.connect("localhost", tcp).unwrap();
    let psk = transport::exporter(&tls, &binding()).unwrap();
    assert_eq!(&psk[..], &server.join().unwrap()[..]);
    let mut other = binding();
    other.epoch = 2;
    assert_ne!(&psk[..], &transport::exporter(&tls, &other).unwrap()[..]);
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn classical_only_client_is_rejected() {
    let (dir, key, client_cert) = fixture();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let acceptor = transport::server(&dir.join("server.pem"), &dir.join("key.pem")).unwrap();
    let server = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        acceptor.accept(stream).is_err()
    });
    let mut connector = SslConnector::builder(SslMethod::tls()).unwrap();
    connector.set_ca_file(dir.join("root.pem")).unwrap();
    connector.set_private_key(&key).unwrap();
    connector.set_certificate(&client_cert).unwrap();
    connector
        .set_min_proto_version(Some(SslVersion::TLS1_3))
        .unwrap();
    connector.set_groups_list("X25519").unwrap();
    connector.set_sigalgs_list("mldsa65").unwrap();
    let tcp = TcpStream::connect(addr).unwrap();
    tcp.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    assert!(connector.build().connect("localhost", tcp).is_err());
    assert!(server.join().unwrap());
    fs::remove_dir_all(dir).unwrap();
}
