import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path

CONNECTION = Path(__file__).resolve().parents[3] / "migration/connection"
TUNNEL = {"interface": "mc0", "listen_port": 51820, "job_port": 18080, "probe_port": 18081}
GATEWAY_ADDRESS, CLIENT_ADDRESS = "10.240.0.1", "10.240.0.2"


def module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


TRUST = module(CONNECTION / "tests/e2e/prepare_trust.py")


def private_root(path: Path) -> Path:
    meta = path.lstat()
    if (
        not stat.S_ISDIR(meta.st_mode)
        or meta.st_uid != os.getuid()
        or stat.S_IMODE(meta.st_mode) != 0o700
    ):
        raise SystemExit(f"{path} must be an existing current-user 0700 directory")
    return path


def compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def renew_broker_cert(root: Path, domain: str, openssl: str) -> None:
    with tempfile.TemporaryDirectory(dir=root) as directory:
        work = Path(directory)
        csr, extensions, certificate = work / "broker.csr", work / "broker.ext", work / "broker.crt"
        TRUST.write(
            csr,
            TRUST.invoke(
                openssl, "req", "-new", "-key", str(root / "broker.key"),
                "-subj", f"/CN={domain}",
            ),
        )
        TRUST.write(
            extensions,
            (
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                f"extendedKeyUsage=serverAuth\nsubjectAltName=DNS:{domain}\n"
            ).encode(),
        )
        TRUST.write(
            certificate,
            TRUST.invoke(
                openssl, "x509", "-req", "-in", str(csr), "-CA", str(root / "broker-ca.crt"),
                "-CAkey", str(root / "broker-ca.key"), "-set_serial", str(secrets.randbits(120) + 1),
                # Both peers enforce a maximum certificate lifetime of 24 hours.
                "-days", "1", "-extfile", str(extensions),
            ),
        )
        TRUST.invoke(
            openssl, "verify", "-CAfile", str(root / "broker-ca.crt"),
            "-purpose", "sslserver", "-verify_hostname", domain, str(certificate),
        )
        certificate.replace(root / "broker.crt")


def trust(root: Path, domain: str, openssl: str) -> None:
    os.umask(0o077)
    for name in ("broker-ca.key", "signing", "job-signing-key.pem", "runtime-token"):
        if (root / name).exists():
            raise SystemExit(f"refusing to replace existing {name}")
    (root / "signing").mkdir(mode=0o700)
    (root / "public").mkdir(mode=0o700, exist_ok=True)

    def key(path: Path, algorithm: str) -> None:
        TRUST.write(path, TRUST.invoke(openssl, "genpkey", "-algorithm", algorithm))

    key(root / "broker-ca.key", "ML-DSA-65")
    TRUST.write(
        root / "broker-ca.crt",
        TRUST.invoke(
            openssl, "req", "-new", "-x509", "-key", str(root / "broker-ca.key"),
            "-subj", "/CN=Oneport connection broker CA", "-days", "3650",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        ),
    )
    key(root / "broker.key", "ML-DSA-65")
    renew_broker_cert(root, domain, openssl)
    key(root / "signing/release-mldsa.pem", "ML-DSA-65")
    key(root / "signing/release-ed25519.pem", "ED25519")
    key(root / "job-signing-key.pem", "ML-DSA-65")
    TRUST.write(root / "runtime-token", (secrets.token_urlsafe(48) + "\n").encode())
    public = {
        "MIGRATION_RELEASE_MLDSA_PUBLIC_KEY": TRUST.public_key(openssl, root / "signing/release-mldsa.pem", 1952),
        "MIGRATION_RELEASE_ED25519_PUBLIC_KEY": TRUST.public_key(openssl, root / "signing/release-ed25519.pem", 32),
        "MIGRATION_MIN_RELEASE_SEQUENCE": "1",
    }
    TRUST.write(root / "public/release-build.env", "".join(f"{k}={v}\n" for k, v in public.items()).encode())
    TRUST.write(
        root / "public/job-public-key",
        (TRUST.public_key(openssl, root / "job-signing-key.pem", 1952) + "\n").encode(),
    )
    print("trust prepared; no secret was printed")


def release(root: Path, domain: str, version: str, sequence: int, days: int, endpoint: str, binaries: dict, openssl: str) -> None:
    os.umask(0o022)
    out = root / "release" / version
    out.mkdir(parents=True, exist_ok=False)
    digests = {}
    policy = {
        "uid": 0,
        "role": "client",
        "local_address": CLIENT_ADDRESS,
        "peer_address": GATEWAY_ADDRESS,
        "endpoint": endpoint,
        **TUNNEL,
    }
    for arch, directory in binaries.items():
        bundle = out / f"linux-{arch}"
        bundle.mkdir()
        for name in ("migration-connector", "migration-net-helper"):
            shutil.copyfile(directory / name, bundle / name)
        shutil.copyfile(root / "broker-ca.crt", bundle / "root-ca.pem")
        (bundle / "connector.json").write_text(
            compact(
                {
                    "public_api_url": f"https://{domain}/api/v1/connections",
                    "broker_host": domain,
                    "broker_port": 8443,
                    "root_ca": "root-ca.pem",
                    "job_public_key": (root / "public/job-public-key").read_text().strip(),
                    "collector_archive": "inventory.pyz",
                    "helper_path": "/usr/local/libexec/migration-net-helper",
                }
            )
        )
        (bundle / "net-policy.json").write_text(compact(policy))
        module(CONNECTION / "releases/build_inventory.py").build(bundle / "inventory.pyz")
        manifest = module(CONNECTION / "releases/sign_manifest.py").sign(
            bundle,
            sequence,
            int(time.time() * 1000) + days * 86_400_000,
            root / "signing/release-mldsa.pem",
            root / "signing/release-ed25519.pem",
            openssl,
        )
        (bundle / "manifest.json").write_text(compact(manifest))
        digests[arch] = hashlib.sha384((bundle / "migration-connector").read_bytes()).hexdigest()
    bootstrap = module(CONNECTION / "releases/render_bootstrap.py").render(
        f"https://{domain}/releases/{version}", digests["amd64"], digests["arm64"]
    )
    (out / "bootstrap.sh").write_text(bootstrap)
    for path in [out, *out.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    metadata = {
        "release_version": version,
        "release_sequence": sequence,
        "bootstrap_url": f"https://{domain}/releases/{version}/bootstrap.sh",
        "bootstrap_sha384": hashlib.sha384(bootstrap.encode()).hexdigest(),
    }
    (root / "public/release-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


def config(root: Path, domain: str, tenant: str, gateway: str) -> None:
    os.umask(0o077)
    metadata = json.loads((root / "public/release-metadata.json").read_text())
    server, broker = root / "server", root / "broker"
    server.mkdir(mode=0o700, exist_ok=True)
    broker.mkdir(mode=0o700, exist_ok=True)
    shutil.copyfile(root / "job-signing-key.pem", server / "job-signing-key.pem")
    shutil.copyfile(root / "runtime-token", server / "runtime-token")
    shutil.copyfile(root / "runtime-token", broker / "runtime-token")
    shutil.copyfile(root / "broker.key", broker / "gateway-key.pem")
    shutil.copyfile(root / "broker.crt", broker / "gateway-chain.pem")
    (root / "server.env").write_text(
        "".join(
            f"{k}={v}\n"
            for k, v in {
                "ONEPORT_CONNECTION_ENABLED": "true",
                "ONEPORT_CONNECTION_REDIS_URL": "redis://redis:6379/0",
                "ONEPORT_CONNECTION_OPENSSL": "/usr/bin/openssl",
                "ONEPORT_CONNECTION_JOB_SIGNING_KEY": "/etc/oneport/connection/job-signing-key.pem",
                "ONEPORT_CONNECTION_RUNTIME_TOKEN_FILE": "/etc/oneport/connection/runtime-token",
                "ONEPORT_CONNECTION_BOOTSTRAP_URL": metadata["bootstrap_url"],
                "ONEPORT_CONNECTION_RELEASE_VERSION": metadata["release_version"],
                "ONEPORT_CONNECTION_BOOTSTRAP_SHA384": metadata["bootstrap_sha384"],
                "ONEPORT_CONNECTION_RUNTIME_TENANT_ID": tenant,
                "ONEPORT_CONNECTION_RUNTIME_GATEWAY_ID": gateway,
            }.items()
        )
    )
    (broker / "broker.json").write_text(
        compact(
            {
                "listen": "0.0.0.0:8443",
                "certificate": "/etc/migration-connector/gateway-chain.pem",
                "private_key": "/etc/migration-connector/gateway-key.pem",
                "runtime_api_url": f"https://{domain}/api/v1/connection-runtime",
                "public_api_url": f"https://{domain}/api/v1/connections",
                "runtime_token_file": "/etc/migration-connector/runtime-token",
                "tenant_id": tenant,
                "gateway_id": gateway,
                "helper_path": "/usr/local/libexec/migration-net-helper",
                "release_version": metadata["release_version"],
                "bootstrap_sha384": metadata["bootstrap_sha384"],
            }
        )
    )
    (broker / "net-policy.json").write_text(
        compact(
            {
                "uid": 0,
                "role": "gateway",
                "local_address": GATEWAY_ADDRESS,
                "peer_address": CLIENT_ADDRESS,
                "endpoint": "192.0.2.1:51820",
                **TUNNEL,
            }
        )
    )
    print("server.env, server/, broker/ prepared; no secret was printed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--openssl", default="openssl")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("trust")
    sub.add_parser("renew-broker-cert")
    rel = sub.add_parser("release")
    rel.add_argument("--version", required=True)
    rel.add_argument("--sequence", type=int, required=True)
    rel.add_argument("--days", type=int, default=30)
    rel.add_argument("--endpoint", required=True)
    rel.add_argument("--amd64", type=Path, required=True)
    rel.add_argument("--arm64", type=Path, required=True)
    cfg = sub.add_parser("config")
    cfg.add_argument("--tenant", required=True)
    cfg.add_argument("--gateway", required=True)
    args = parser.parse_args()
    root = private_root(args.dir)
    if args.command == "trust":
        trust(root, args.domain, args.openssl)
    elif args.command == "renew-broker-cert":
        renew_broker_cert(root, args.domain, args.openssl)
        print("broker.crt renewed; deploy it as broker/gateway-chain.pem")
    elif args.command == "release":
        release(
            root, args.domain, args.version, args.sequence, args.days, args.endpoint,
            {"amd64": args.amd64, "arm64": args.arm64}, args.openssl,
        )
    else:
        config(root, args.domain, args.tenant, args.gateway)


if __name__ == "__main__":
    main()
