#!/usr/bin/python
"""Controller-side certificate, private-key, hostname, chain and expiry checks."""

import ipaddress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.keycloak_common import command
from cryptography import x509
from cryptography.hazmat.primitives import serialization


def name_matches(cert, hostname):
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    try:
        return ipaddress.ip_address(hostname) in san.get_values_for_type(x509.IPAddress)
    except ValueError:
        host = hostname.rstrip(".").lower().encode("idna").decode()
        for dns in san.get_values_for_type(x509.DNSName):
            dns = dns.rstrip(".").lower()
            if dns == host or (
                dns.startswith("*.")
                and dns.count("*") == 1
                and host.count(".") == dns.count(".")
                and host.endswith(dns[1:])
            ):
                return True
    return False


def main():
    module = AnsibleModule(
        argument_spec={
            "certificate_file": {"type": "path", "required": True},
            "private_key_file": {"type": "path", "required": True, "no_log": True},
            "ca_file": {"type": "path", "required": True},
            "base_url": {"type": "str", "required": True},
        },
        supports_check_mode=True,
    )
    p = module.params
    cert, key, ca = p["certificate_file"], p["private_key_file"], p["ca_file"]
    host = urlsplit(p["base_url"]).hostname
    try:
        leaf = x509.load_pem_x509_certificate(Path(cert).read_bytes())
        private = serialization.load_pem_private_key(
            Path(key).read_bytes(), password=None
        )
        encoding, fmt = (
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        now = datetime.now(UTC)
        outcomes = {
            "certificate_name": name_matches(leaf, host),
            "certificate_expiry": leaf.not_valid_before_utc
            <= now
            < leaf.not_valid_after_utc,
            "certificate_key_match": leaf.public_key().public_bytes(encoding, fmt)
            == private.public_key().public_bytes(encoding, fmt),
        }
    except (OSError, ValueError, TypeError):
        outcomes = {
            name: False
            for name in (
                "certificate_name",
                "certificate_expiry",
                "certificate_key_match",
            )
        }
    verified = command(
        [
            "openssl",
            "verify",
            "-purpose",
            "sslserver",
            "-CAfile",
            ca,
            "-untrusted",
            cert,
            cert,
        ]
    )
    outcomes["certificate_chain"] = bool(verified and verified.returncode == 0)
    checks = [
        {"name": name, "status": "pass" if ok else "fail"}
        for name, ok in outcomes.items()
    ]
    module.exit_json(changed=False, report={"checks": checks})


if __name__ == "__main__":
    main()
