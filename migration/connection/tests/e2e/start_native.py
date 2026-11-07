"""Start isolated real broker/client containers, without enrolling or approving a device."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

LABEL = "migration.connection.live-e2e=true"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--image", default="mc-live-e2e-native:local")
    args = parser.parse_args()
    root = args.private_dir.resolve()
    if root.stat().st_mode & 0o077:
        raise SystemExit("private directory must be 0700")
    metadata = json.loads((root / "public/release-metadata.json").read_text())
    version = metadata["release_version"]
    if (
        not isinstance(version, str)
        or not version
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in version
        )
    ):
        raise ValueError("invalid release version")
    bootstrap = root / "release/releases" / version / "bootstrap.sh"
    digest = hashlib.sha384(bootstrap.read_bytes()).hexdigest()
    config_dir = root / "native-config"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    for role, local, peer, endpoint, uid in (
        ("broker", "10.240.87.1", "10.240.87.2", "172.30.87.21:51820", 0),
        ("client", "10.240.87.2", "10.240.87.1", "172.30.87.20:51820", 1001),
    ):
        policy = {
            "uid": uid,
            "interface": "mclive0",
            "local_address": local,
            "peer_address": peer,
            "endpoint": endpoint,
            "listen_port": 51820,
            "job_port": 18080,
            "probe_port": 18081,
            "role": "gateway" if role == "broker" else "client",
        }
        path = config_dir / f"{role}-policy.json"
        path.write_text(json.dumps(policy))
        path.chmod(0o600)
    broker_config = {
        "listen": "0.0.0.0:8443",
        "certificate": "/etc/migration-connector/broker.crt",
        "private_key": "/etc/migration-connector/broker.key",
        "runtime_api_url": "https://control.test/api/v1/connection-runtime",
        "public_api_url": "https://control.test/api/v1/connections",
        "runtime_token_file": "/etc/migration-connector/runtime-token",
        "tenant_id": "live-e2e-team",
        "gateway_id": "live-e2e-gateway",
        "helper_path": "/usr/local/libexec/migration-net-helper",
        "release_version": version,
        "bootstrap_sha384": digest,
    }
    path = config_dir / "broker.json"
    path.write_text(json.dumps(broker_config))
    path.chmod(0o600)
    for role, ip in [("broker", "172.30.87.20"), ("client", "172.30.87.21")]:
        name = f"mc-live-e2e-{role}"
        # Refuse to replace a running container or erase an earlier attempt's evidence.
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            LABEL,
            "--network",
            "mc-live-e2e",
            "--network-alias",
            "broker.test" if role == "broker" else "client.test",
            "--ip",
            ip,
            "--env",
            "HOME=/home/connector" if role == "client" else "HOME=/root",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "AUDIT_WRITE",
            args.image,
            stdout=subprocess.DEVNULL,
        )
        docker(
            "cp",
            str(root / "ca.crt"),
            f"{name}:/usr/local/share/ca-certificates/live-e2e-control.crt",
        )
        docker(
            "cp",
            str(config_dir / f"{role}-policy.json"),
            f"{name}:/etc/migration-connector/net-policy.json",
        )
        docker(
            "exec",
            name,
            "chown",
            "0:0",
            "/etc/migration-connector/net-policy.json",
            "/usr/local/share/ca-certificates/live-e2e-control.crt",
        )
        docker(
            "exec",
            name,
            "chmod",
            "0644",
            "/etc/migration-connector/net-policy.json",
            "/usr/local/share/ca-certificates/live-e2e-control.crt",
        )
        docker("exec", name, "update-ca-certificates", stdout=subprocess.DEVNULL)
        if role == "broker":
            for source, target in [
                ("broker.crt", "broker.crt"),
                ("broker.key", "broker.key"),
                ("runtime-token", "runtime-token"),
                ("native-config/broker.json", "broker.json"),
            ]:
                docker(
                    "cp",
                    str(root / source),
                    f"{name}:/etc/migration-connector/{target}",
                )
            docker(
                "exec",
                name,
                "chown",
                "0:0",
                "/etc/migration-connector/broker.crt",
                "/etc/migration-connector/broker.key",
                "/etc/migration-connector/runtime-token",
                "/etc/migration-connector/broker.json",
            )
            docker(
                "exec",
                name,
                "chmod",
                "0600",
                "/etc/migration-connector/broker.key",
                "/etc/migration-connector/runtime-token",
            )
        else:
            docker(
                "exec",
                name,
                "install",
                "-d",
                "-o",
                "1001",
                "-g",
                "1001",
                "-m",
                "0700",
                "/home/connector/.kube",
            )
            docker(
                "cp", str(root / "kubeconfig"), f"{name}:/home/connector/.kube/config"
            )
            docker("exec", name, "chown", "1001:1001", "/home/connector/.kube/config")
            docker("exec", name, "chmod", "0400", "/home/connector/.kube/config")
    docker(
        "exec",
        "-d",
        "mc-live-e2e-broker",
        "sh",
        "-c",
        "exec /usr/local/libexec/migration-key-broker /etc/migration-connector/broker.json > /var/log/migration-broker.log 2>&1",
    )
    print(
        "Native containers started; enrollment must be initiated from the real web UI"
    )


if __name__ == "__main__":
    main()
