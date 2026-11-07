#!/usr/bin/python
"""Create/validate durable seal key and TLS materials without returning private bytes."""

import ipaddress
import os
import pwd
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ansible.module_utils.bao_common import BaoError, atomic, ensure
from ansible.module_utils.basic import AnsibleModule


def validate_certificate(cert_bytes, key_bytes, host):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    certificate = x509.load_pem_x509_certificate(cert_bytes)
    key = serialization.load_pem_private_key(key_bytes, password=None)
    fmt = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ensure(
        key.public_key().public_bytes(*fmt)
        == certificate.public_key().public_bytes(*fmt),
        "TLS_KEY_MISMATCH",
    )
    now = datetime.now(UTC)
    before = (
        certificate.not_valid_before_utc
        if hasattr(certificate, "not_valid_before_utc")
        else certificate.not_valid_before.replace(tzinfo=UTC)
    )
    after = (
        certificate.not_valid_after_utc
        if hasattr(certificate, "not_valid_after_utc")
        else certificate.not_valid_after.replace(tzinfo=UTC)
    )
    ensure(before <= now < after, "TLS_CERTIFICATE_EXPIRED")
    san = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    try:
        address = ipaddress.ip_address(host)
        ensure(
            address in san.get_values_for_type(x509.IPAddress), "TLS_ADDRESS_MISMATCH"
        )
    except ValueError:
        ensure(host in san.get_values_for_type(x509.DNSName), "TLS_ADDRESS_MISMATCH")
    return certificate


def create_certificate(key_bytes, host, days):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = (
        serialization.load_pem_private_key(key_bytes, password=None)
        if key_bytes
        else ec.generate_private_key(ec.SECP256R1())
    )
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])])
    try:
        san = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        san = x509.DNSName(host)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([san]), False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def material(config, host):
    uid = pwd.getpwnam("openbao").pw_uid
    changed = False
    keypath = Path("/etc/openbao/keys/static.key")
    ensure(not keypath.is_symlink(), "UNSAFE_KEY_PATH")
    if not keypath.exists():
        ensure(
            not any(Path("/var/lib/openbao").iterdir()),
            "STATIC_KEY_MISSING_RECOVERY_REQUIRED",
        )
        fd = os.open(
            keypath, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
        changed = True
    ensure(keypath.stat().st_size == 32, "STATIC_KEY_INVALID")
    os.chown(keypath, uid, -1)
    os.chmod(keypath, 0o400)
    certpath, privatepath = (
        Path("/etc/openbao/server.crt"),
        Path("/etc/openbao/server.key"),
    )
    ensure(
        not certpath.is_symlink() and not privatepath.is_symlink(), "UNSAFE_TLS_PATH"
    )
    existing_key = privatepath.read_bytes() if privatepath.exists() else None
    existing_cert = certpath.read_bytes() if certpath.exists() else None
    tls = config["tls"]
    if tls["mode"] == "provided":
        cert = config["credentials"]["tls_certificate"].encode()
        key = config["credentials"]["tls_private_key"].encode()
        validate_certificate(cert, key, host)
        ensure(
            not existing_key or existing_key == key,
            "TLS_CHANGE_REQUIRES_EXPLICIT_ACTION",
        )
        ensure(
            not existing_cert or existing_cert == cert,
            "TLS_CHANGE_REQUIRES_EXPLICIT_ACTION",
        )
    elif existing_cert:
        ensure(existing_key, "TLS_KEY_MISSING")
        cert, key = existing_cert, existing_key
        validate_certificate(cert, key, host)
    else:
        cert, key = create_certificate(existing_key, host, tls["validity_days"])
    if not existing_key:
        atomic(privatepath, key, 0o400)
        changed = True
    if not existing_cert:
        atomic(certpath, cert, 0o444)
        changed = True
    os.chown(privatepath, uid, -1)
    os.chmod(privatepath, 0o400)
    os.chmod(certpath, 0o444)
    return changed


def main():
    m = AnsibleModule(
        argument_spec={
            "config": {"type": "dict", "required": True},
            "credentials": {"type": "dict", "default": {}, "no_log": True},
            "host": {"type": "str", "required": True},
        },
        supports_check_mode=False,
    )
    try:
        m.params["config"]["credentials"] = m.params["credentials"]
        changed = material(m.params["config"], m.params["host"])
        m.exit_json(changed=changed, report={"status": "materials_ready"})
    except BaoError as exc:
        m.fail_json(msg="Material preparation blocked", reason_code=str(exc))
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        m.fail_json(
            msg="Material preparation failed", reason_code="MATERIAL_PREPARATION_FAILED"
        )


if __name__ == "__main__":
    main()
