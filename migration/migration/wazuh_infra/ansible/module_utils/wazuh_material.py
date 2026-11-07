"""Initial PKI, private service identities and owned Wazuh configuration."""

import configparser
import datetime
import grp
import ipaddress
import os
import pwd
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path

from ansible.module_utils.wazuh_common import (
    STATE,
    atomic,
    check,
    no_symlinks,
    read,
    save,
)


def owned(path, content, user="root", mode=0o600, group=None):
    p = no_symlinks(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    changed = atomic(p, content, mode)
    account = pwd.getpwnam(user)
    os.chown(p, account.pw_uid, grp.getgrnam(group).gr_gid if group else account.pw_gid)
    return changed


def certificate(name, ca_key=None, ca_cert=None, addresses=()):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Oneport"),
            x509.NameAttribute(NameOID.COMMON_NAME, name),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject if ca_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(
                ca=ca_cert is None, path_length=0 if ca_cert is None else None
            ),
            critical=True,
        )
    )
    if addresses:
        names = []
        for address in addresses:
            try:
                names.append(x509.IPAddress(ipaddress.ip_address(address)))
            except ValueError:
                names.append(x509.DNSName(address))
        builder = builder.add_extension(
            x509.SubjectAlternativeName(names), critical=False
        )
    cert = builder.sign(ca_key or key, hashes.SHA256())
    return (
        key,
        cert,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        cert.public_bytes(serialization.Encoding.PEM),
    )


def pki(c, tls_material):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    root = STATE / "pki"
    root.mkdir(mode=0o700, exist_ok=True)
    if not (root / "ca.key").exists():
        check(not (root / "ca.pem").exists(), "INCOMPLETE_PKI_REQUIRES_RECOVERY")
        _, _, key, cert = certificate("oneport-wazuh-ca")
        owned(root / "ca.key", key)
        owned(root / "ca.pem", cert)
    ca_key = serialization.load_pem_private_key(
        (root / "ca.key").read_bytes(), password=None
    )
    ca = x509.load_pem_x509_certificate((root / "ca.pem").read_bytes())
    for name, addresses in (
        ("indexer", ("127.0.0.1", "localhost")),
        ("oneport-admin", ()),
        ("api", ("127.0.0.1", "localhost")),
        ("dashboard", (c["target"]["bind_address"],)),
    ):
        if not (root / (name + ".key")).exists():
            check(
                not (root / (name + ".pem")).exists(),
                "INCOMPLETE_PKI_REQUIRES_RECOVERY",
            )
            _, _, key, cert = certificate(name, ca_key, ca, addresses)
            owned(root / (name + ".key"), key)
            owned(root / (name + ".pem"), cert)
    for directory, user, name in (
        ("/etc/wazuh-indexer/certs", "wazuh-indexer", "indexer"),
        ("/etc/filebeat/certs", "root", "indexer"),
        ("/etc/wazuh-dashboard/certs", "wazuh-dashboard", "dashboard"),
    ):
        target = no_symlinks(directory)
        target.mkdir(mode=0o755, parents=True, exist_ok=True)
        owned(target / "root-ca.pem", (root / "ca.pem").read_bytes(), mode=0o644)
        if name == "dashboard" and c["tls"]["mode"] == "provided":
            cert = x509.load_pem_x509_certificate(
                tls_material["certificate_ref"].encode()
            )
            key = serialization.load_pem_private_key(
                tls_material["private_key_ref"].encode(), password=None
            )
            check(
                cert.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                == key.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                ),
                "TLS_KEY_MISMATCH",
            )
            check(
                (
                    cert.not_valid_after_utc
                    if hasattr(cert, "not_valid_after_utc")
                    else cert.not_valid_after.replace(tzinfo=datetime.UTC)
                )
                > datetime.datetime.now(datetime.UTC),
                "TLS_CERTIFICATE_EXPIRED",
            )
            check(
                (
                    cert.not_valid_before_utc
                    if hasattr(cert, "not_valid_before_utc")
                    else cert.not_valid_before.replace(tzinfo=datetime.UTC)
                )
                <= datetime.datetime.now(datetime.UTC),
                "TLS_CERTIFICATE_NOT_YET_VALID",
            )
            check(
                ipaddress.ip_address(c["target"]["bind_address"])
                in cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value.get_values_for_type(x509.IPAddress),
                "TLS_ADDRESS_MISMATCH",
            )
            owned(target / "dashboard.pem", tls_material["certificate_ref"], user)
            owned(target / "dashboard-key.pem", tls_material["private_key_ref"], user)
            owned(STATE / "dashboard-ca.pem", tls_material["ca_ref"], mode=0o644)
        else:
            owned(
                target / (name + ".pem"),
                (root / (name + ".pem")).read_bytes(),
                user,
                0o644,
            )
            owned(
                target / (name + "-key.pem"),
                (root / (name + ".key")).read_bytes(),
                user,
            )
            if name == "dashboard":
                owned(
                    STATE / "dashboard-ca.pem",
                    (root / "ca.pem").read_bytes(),
                    mode=0o644,
                )
    return root


def credentials(caller):
    path = STATE / "credentials.json"
    if path.exists():
        value = read(path)
        check(
            value["operator"] == caller["operator_password_ref"]
            and value["reader"] == caller["reader_password_ref"],
            "PASSWORD_CHANGE_REQUIRES_EXPLICIT_ROTATION",
        )
        return value
    value = {
        k: "Wi!9aA" + secrets.token_urlsafe(32)
        for k in ("admin", "kibana", "ingest", "api", "api_admin")
    }
    value.update(
        operator=caller["operator_password_ref"], reader=caller["reader_password_ref"]
    )
    save(path, value)
    return value


def yaml_file(path, value, user="root", mode=0o600, group=None):
    import yaml

    return owned(path, yaml.safe_dump(value, sort_keys=False), user, mode, group)


def security_users(value):
    import bcrypt
    import yaml

    p = Path("/etc/wazuh-indexer/opensearch-security/internal_users.yml")
    if (STATE / "users-prepared.json").exists():
        return
    users = {"_meta": {"type": "internalusers", "config_version": 2}}
    for username, key, roles in (
        ("admin", "admin", ["admin"]),
        ("kibanaserver", "kibana", []),
        ("oneport_ingest", "ingest", ["oneport_ingest"]),
        ("oneport_operator", "operator", ["oneport_operator"]),
        ("oneport_reader", "reader", ["oneport_reader"]),
    ):
        users[username] = {
            "hash": bcrypt.hashpw(value[key].encode(), bcrypt.gensalt()).decode(),
            "reserved": username in ("admin", "kibanaserver"),
            "backend_roles": roles,
        }
    yaml_file(p, users, "wazuh-indexer")
    # Preserve vendor role definitions while adding only our roles/mappings.
    rp = p.parent / "roles.yml"
    roles = yaml.safe_load(rp.read_text())
    roles["oneport_ingest"] = {
        "cluster_permissions": [
            "cluster_monitor",
            "cluster_composite_ops",
            "indices:admin/template/*",
        ],
        "index_permissions": [
            {
                "index_patterns": [
                    "wazuh-alerts-*",
                    "wazuh-archives-*",
                    "wazuh-ingest-failures",
                ],
                "allowed_actions": [
                    "create_index",
                    "write",
                    "indices:admin/mapping/put",
                ],
            }
        ],
    }
    roles["oneport_reader"] = {
        "cluster_permissions": ["cluster_composite_ops_ro"],
        "index_permissions": [
            {
                "index_patterns": [
                    "wazuh-alerts-*",
                    "wazuh-archives-*",
                    "wazuh-ingest-failures",
                ],
                "allowed_actions": [
                    "read",
                    "indices:admin/mappings/get",
                    "indices:admin/aliases/get",
                    "indices:data/read/field_caps",
                    "indices:monitor/settings/get",
                    "indices:monitor/stats",
                ],
            },
            {
                "index_patterns": [".kibana*", ".opensearch_dashboards*"],
                "allowed_actions": ["read"],
            },
        ],
    }
    yaml_file(rp, roles, "wazuh-indexer")
    mp = p.parent / "roles_mapping.yml"
    mappings = yaml.safe_load(mp.read_text())
    mappings.update(
        oneport_reader={"backend_roles": ["oneport_reader"]},
        oneport_ingest={"backend_roles": ["oneport_ingest"]},
    )
    for role, backend in (
        ("all_access", "oneport_operator"),
        ("kibana_user", "oneport_operator"),
    ):
        mapping = mappings.setdefault(role, {})
        existing = mapping.setdefault("backend_roles", [])
        if backend not in existing:
            existing.append(backend)
    yaml_file(mp, mappings, "wazuh-indexer")
    save(STATE / "users-prepared.json", {"complete": True})


def configure_manager(c, enabled=False):
    p = Path("/var/ossec/etc/ossec.conf")
    text = p.read_text()
    root = ET.fromstring("<root>" + text + "</root>")
    main = root.find("ossec_config")
    check(main is not None, "MANAGER_CONFIGURATION_INVALID")
    global_ = main.find("global")
    if global_ is None:
        global_ = ET.SubElement(main, "global")
    node = global_.find("logall_json")
    if node is None:
        node = ET.SubElement(global_, "logall_json")
    node.text = "yes"
    for block in root.findall("ossec_config"):
        for item in list(block):
            if (
                item.tag in ("remote", "active-response")
                or item.tag == "wodle"
                and item.get("name") == "aws-s3"
            ):
                block.remove(item)
            elif item.tag == "auth":
                flag = item.find("disabled")
                if flag is None:
                    flag = ET.SubElement(item, "disabled")
                flag.text = "yes"
            elif item.tag in ("vulnerability-detection", "indexer"):
                flag = item.find("enabled")
                if flag is not None:
                    flag.text = "no"
    aws = ET.SubElement(main, "wodle", {"name": "aws-s3"})
    for k, v in (
        ("disabled", "no" if enabled and c["sources"] else "yes"),
        ("interval", "10m"),
        ("run_on_start", "yes"),
        ("skip_on_error", "no"),
    ):
        ET.SubElement(aws, k).text = v
    for s in c["sources"]:
        if s["type"] == "cloudwatchlogs":
            service = ET.SubElement(aws, "service", {"type": "cloudwatchlogs"})
            for k, v in (
                ("aws_profile", "wi_" + s["id"]),
                ("aws_log_groups", s["log_group"]),
                ("regions", s["region"]),
                ("only_logs_after", s["start_date"]),
            ):
                ET.SubElement(service, k).text = v
            continue
        bucket = ET.SubElement(aws, "bucket", {"type": s["type"]})
        for k, v in (
            ("name", s["bucket"]),
            ("path", s["prefix"]),
            ("aws_account_id", s["account_id"]),
            ("regions", s["region"]),
            ("aws_profile", "wi_" + s["id"]),
            ("only_logs_after", s["start_date"]),
            ("remove_from_bucket", "no"),
        ):
            if v:
                ET.SubElement(bucket, k).text = v
    return owned(
        p,
        "\n".join(ET.tostring(x, encoding="unicode") for x in root),
        "root",
        0o640,
        group="wazuh",
    )


def write_aws_credentials(c, aws_material):
    import io

    profile = configparser.ConfigParser(interpolation=None)
    for s in c["sources"]:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(aws_material["aws"][s["id"]])
        profile["wi_" + s["id"]] = {
            k: v
            for k, v in parser[s["profile"]].items()
            if k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")
        }
    buf = io.StringIO()
    profile.write(buf)
    return owned("/var/ossec/.aws/credentials", buf.getvalue(), "root")


def write_configuration(c, value, aws_material, enabled=False):
    from ansible.module_utils.wazuh_ingestion import harden_pipelines

    pki(c, aws_material.get("tls", {}))
    security_users(value)
    harden_pipelines()
    index = {
        "network.host": "127.0.0.1",
        "http.port": 9200,
        "node.name": "indexer",
        "cluster.name": "oneport-wazuh",
        "discovery.type": "single-node",
        "path.data": c["storage"]["index_path"],
        "path.logs": "/var/log/wazuh-indexer",
        "plugins.security.ssl.http.enabled": True,
        "plugins.security.ssl.http.pemcert_filepath": "/etc/wazuh-indexer/certs/indexer.pem",
        "plugins.security.ssl.http.pemkey_filepath": "/etc/wazuh-indexer/certs/indexer-key.pem",
        "plugins.security.ssl.http.pemtrustedcas_filepath": "/etc/wazuh-indexer/certs/root-ca.pem",
        "plugins.security.ssl.transport.pemcert_filepath": "/etc/wazuh-indexer/certs/indexer.pem",
        "plugins.security.ssl.transport.pemkey_filepath": "/etc/wazuh-indexer/certs/indexer-key.pem",
        "plugins.security.ssl.transport.pemtrustedcas_filepath": "/etc/wazuh-indexer/certs/root-ca.pem",
        "plugins.security.ssl.transport.enforce_hostname_verification": True,
        "plugins.security.nodes_dn": ["CN=indexer,O=Oneport"],
        "plugins.security.authcz.admin_dn": ["CN=oneport-admin,O=Oneport"],
        "plugins.security.restapi.roles_enabled": [
            "all_access",
            "security_rest_api_access",
        ],
        "compatibility.override_main_response_version": True,
        "cluster.routing.allocation.disk.threshold_enabled": True,
    }
    yaml_file("/etc/wazuh-indexer/opensearch.yml", index, "wazuh-indexer", 0o640)
    jvm = Path("/etc/wazuh-indexer/jvm.options")
    lines = [
        line
        for line in jvm.read_text().splitlines()
        if not line.startswith(("-Xms", "-Xmx"))
    ]
    atomic(
        jvm,
        "\n".join(
            lines + [f"-Xms{c['target']['heap_mb']}m", f"-Xmx{c['target']['heap_mb']}m"]
        )
        + "\n",
        0o640,
    )
    configure_manager(c, enabled)
    yaml_file(
        "/var/ossec/api/configuration/api.yaml",
        {
            "host": ["127.0.0.1"],
            "port": 55000,
            "https": {
                "enabled": True,
                "key": "server.key",
                "cert": "server.crt",
                "use_ca": False,
            },
        },
        "root",
        0o640,
        group="wazuh",
    )
    owned(
        "/var/ossec/api/configuration/ssl/server.key",
        (STATE / "pki/api.key").read_bytes(),
        "root",
        0o640,
        group="wazuh",
    )
    owned(
        "/var/ossec/api/configuration/ssl/server.crt",
        (STATE / "pki/api.pem").read_bytes(),
        "root",
        0o644,
    )
    write_aws_credentials(c, aws_material)
    yaml_file(
        "/etc/filebeat/filebeat.yml",
        {
            "output.elasticsearch": {
                "hosts": ["https://127.0.0.1:9200"],
                "username": "oneport_ingest",
                "password": value["ingest"],
                "ssl.certificate_authorities": ["/etc/filebeat/certs/root-ca.pem"],
            },
            "filebeat.modules": [
                {
                    "module": "wazuh",
                    "alerts": {"enabled": True},
                    "archives": {"enabled": True},
                }
            ],
            "setup.template.json.enabled": True,
            "setup.template.json.path": "/etc/filebeat/wazuh-template.json",
            "setup.template.json.name": "wazuh",
            "setup.ilm.enabled": False,
            "logging.to_files": True,
            "logging.files.keepfiles": 7,
            "seccomp": {
                "default_action": "allow",
                "syscalls": [{"action": "allow", "names": ["rseq"]}],
            },
        },
    )
    yaml_file(
        "/etc/wazuh-dashboard/opensearch_dashboards.yml",
        {
            "server.host": c["target"]["bind_address"],
            "server.port": c["target"]["dashboard_port"],
            "opensearch.hosts": ["https://127.0.0.1:9200"],
            "opensearch.username": "kibanaserver",
            "opensearch.password": value["kibana"],
            "opensearch.ssl.verificationMode": "full",
            "opensearch.ssl.certificateAuthorities": [
                "/etc/wazuh-dashboard/certs/root-ca.pem"
            ],
            "opensearch.requestHeadersAllowlist": ["securitytenant", "Authorization"],
            "opensearch_security.multitenancy.enabled": False,
            "opensearch_security.readonly_mode.roles": ["oneport_reader"],
            "server.ssl.enabled": True,
            "server.ssl.key": "/etc/wazuh-dashboard/certs/dashboard-key.pem",
            "server.ssl.certificate": "/etc/wazuh-dashboard/certs/dashboard.pem",
            "opensearch_security.cookie.secure": True,
            "uiSettings.overrides.defaultRoute": "/app/discover",
        },
        "wazuh-dashboard",
    )
    yaml_file(
        "/usr/share/wazuh-dashboard/data/wazuh/config/wazuh.yml",
        {
            "hosts": [
                {
                    "default": {
                        "url": "https://127.0.0.1",
                        "port": 55000,
                        "username": "wazuh-wui",
                        "password": value["api"],
                        "run_as": True,
                    }
                }
            ]
        },
        "wazuh-dashboard",
    )
    save(STATE / "runtime.json", {"config": c, "enabled": enabled})
