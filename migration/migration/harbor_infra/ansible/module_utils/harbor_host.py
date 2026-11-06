"""Ubuntu installation and registry-scoped trust, invoked only by Ansible."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from ansible.module_utils.harbor_core import (
    HarborError,
    atomic,
    check,
    digest,
    locked,
    private_dir,
    read,
    save,
)


def command(argv, cwd=None, accepted=(0,), timeout=120):
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={
                **{
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith(("DOCKER_", "COMPOSE_"))
                },
                "LC_ALL": "C",
                "DEBIAN_FRONTEND": "noninteractive",
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        raise HarborError("HOST_COMMAND_FAILED_OR_TIMED_OUT") from None
    check(p.returncode in accepted, "HOST_COMMAND_FAILED")
    check(
        len(p.stdout) < 8 * 1024 * 1024 and len(p.stderr) < 8 * 1024 * 1024,
        "HOST_OUTPUT_TOO_LARGE",
    )
    return p.stdout.decode(errors="replace"), p.returncode


def installed(package):
    out, code = command(["dpkg-query", "-W", "-f=${Status}", package], accepted=(0, 1))
    return code == 0 and out == "install ok installed"


def mount(path):
    out, _ = command(
        ["findmnt", "--json", "--target", str(path), "--output", "TARGET,SOURCE,FSTYPE"]
    )
    values = json.loads(out)["filesystems"]
    check(len(values) == 1, "MOUNT_IDENTITY_UNKNOWN")
    return values[0]


def paths(c):
    ident = c["target"]["installation_id"]
    return Path("/opt/oneport-harbor") / ident, Path("/var/lib/harbor-infra") / ident


def identity(c, node):
    check(platform.system() == "Linux", "LINUX_REQUIRED")
    check(
        Path("/etc/machine-id").read_text().strip() == node["machine_id"]
        and bool(node["machine_id"]),
        "HOST_IDENTITY_MISMATCH",
    )
    check(
        node["uid"] == next(n["uid"] for n in c["nodes"] if n["name"] == node["name"]),
        "NODE_IDENTITY_MISMATCH",
    )


def discover_host(node):
    candidates = []
    if shutil.which("docker"):
        out, code = command(
            ["docker", "ps", "-a", "--format", "{{json .}}"], accepted=(0, 1)
        )
        if code:
            return {
                "status": "partially_ready",
                "candidates": [],
                "reason_codes": ["DOCKER_DISCOVERY_UNAVAILABLE"],
            }
        groups = {}
        for line in out.splitlines():
            v = json.loads(line)
            if "goharbor/" not in v.get("Image", ""):
                continue
            # docker ps metadata only: do not inspect Config.Env or compose YAML.
            label = v.get("Labels", "")
            project = next(
                (
                    x.split("=", 1)[1]
                    for x in label.split(",")
                    if x.startswith("com.docker.compose.project=")
                ),
                v.get("Names"),
            )
            item = groups.setdefault(
                project,
                {
                    "kind": "compose",
                    "node_name": node["name"],
                    "name": project,
                    "components": [],
                    "product_confirmed": False,
                },
            )
            item["components"].append(
                {
                    "name": v.get("Names"),
                    "image": v.get("Image"),
                    "state": v.get("State"),
                    "ports": v.get("Ports"),
                }
            )
        candidates.extend(groups.values())
    return {"status": "discovered", "candidates": candidates, "reason_codes": []}


def check_docker_space(root):
    p = Path(root)
    check(p.is_absolute(), "DOCKER_ROOT_INVALID")
    while not p.exists():
        p = p.parent
    check(shutil.disk_usage(p).free >= 10 * 1000**3, "INSUFFICIENT_DOCKER_DISK")


def preflight(c, node, releases):
    identity(c, node)
    os_release = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    check(
        os_release.get("ID", "").strip('"') == "ubuntu"
        and os_release.get("VERSION_ID", "").strip('"') == "24.04"
        and platform.machine() == "x86_64",
        "UNSUPPORTED_INSTALL_PLATFORM",
    )
    check(os.geteuid() == 0, "ROOT_REQUIRED")
    check(
        shutil.which("openssl") and shutil.which("systemctl"),
        "HOST_DEPENDENCY_REQUIRED",
    )
    root, state = paths(c)
    previous = read(state / "installation.json")
    if previous:
        check(
            previous["machine_id"] == node["machine_id"]
            and previous["uid"] == node["uid"]
            and previous["installation_digest"] == c["installation_digest"],
            "MANAGED_INSTALLATION_CONFLICT",
        )
    else:
        check(not root.exists(), "INSTALL_PATH_CONFLICT")
    data = Path(c["install"]["data_path"])
    for part in (data, *data.parents):
        check(not part.is_symlink(), "DATA_PATH_SYMLINK")
    if not previous:
        check(
            not data.exists() or data.is_dir() and not any(data.iterdir()),
            "DATA_PATH_NOT_EMPTY",
        )
    ancestor = data
    while not ancestor.exists():
        ancestor = ancestor.parent
    check(
        shutil.disk_usage(ancestor).free >= c["install"]["capacity_gb"] * 1000**3,
        "INSUFFICIENT_DISK",
    )
    expected_mount = mount(ancestor)
    check(expected_mount["target"] == c["install"]["mount_path"], "MOUNT_PATH_MISMATCH")
    memory = dict(
        (line.split(":")[0], int(line.split()[1]))
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.split()[1].isdigit()
    )
    check(
        memory["MemAvailable"] >= 4_000_000 and (os.cpu_count() or 0) >= 2,
        "INSUFFICIENT_RESOURCES",
    )
    check(
        not Path("/data/secretkey").exists()
        and not Path("/data/defaultalias").exists(),
        "LEGACY_HARBOR_DATA_CONFLICT",
    )
    if not previous:
        for port in ((urlsplit(c["endpoint"]["url"]).port or 443), 1514):
            with socket.socket() as s:
                try:
                    s.bind(("0.0.0.0", port))
                except OSError:
                    raise HarborError("PORT_IN_USE") from None
    planned = []
    if shutil.which("docker"):
        docker_root, _ = command(["docker", "info", "--format", "{{.DockerRootDir}}"])
        check_docker_space(docker_root.strip())
    else:
        check_docker_space("/var/lib/docker")
    if not shutil.which("docker"):
        check(c["install"]["install_runtime"], "DOCKER_REQUIRED")
        check(
            not any(
                installed(p)
                for p in (
                    "containerd",
                    "runc",
                    "docker.io",
                    "podman-docker",
                    "docker-ce",
                    "containerd.io",
                )
            ),
            "RUNTIME_PACKAGE_CONFLICT",
        )
        check(
            not shutil.which("containerd")
            and not Path("/etc/containerd/config.toml").exists(),
            "NATIVE_CONTAINERD_REQUIRES_REVIEW",
        )
        check(
            not Path("/etc/docker/daemon.json").exists(),
            "EXISTING_DOCKER_CONFIGURATION",
        )
        # Do not silently bypass an existing host firewall during Docker installation.
        for service in ("ufw", "firewalld", "nftables"):
            _, active = command(
                ["systemctl", "is-active", "--quiet", service], accepted=(0, 3, 4)
            )
            check(active != 0, "DOCKER_FIREWALL_REVIEW_REQUIRED")
        planned = list(releases["docker_packages"])
    else:
        server, _ = command(["docker", "version", "--format", "{{.Server.Version}}"])
        check(int(server.strip().split(".")[0]) > 20, "DOCKER_VERSION_UNSUPPORTED")
        version, _ = command(["docker", "compose", "version", "--short"])
        check(
            version.strip().lstrip("v").split(".")[0] in ("2", "5"),
            "COMPOSE_VERSION_UNSUPPORTED",
        )
    return {
        "status": "preflight_passed",
        "planned_packages": planned,
        "mount": expected_mount,
        "reason_codes": [],
    }


def download(url, sha, destination):
    check(url.startswith("https://"), "DOWNLOAD_HTTPS_REQUIRED")
    if destination.exists():
        check(
            hashlib.sha256(destination.read_bytes()).hexdigest() == sha,
            "DOWNLOAD_CHECKSUM_MISMATCH",
        )
        return
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    check(
        len(data) <= 32 * 1024 * 1024 and hashlib.sha256(data).hexdigest() == sha,
        "DOWNLOAD_CHECKSUM_MISMATCH",
    )
    atomic(destination, data)


def safe_extract(archive, destination):
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        check(len(members) <= 128, "ARCHIVE_LIMIT")
        for entry in members:
            check(
                not entry.issym()
                and not entry.islnk()
                and (entry.isfile() or entry.isdir()),
                "ARCHIVE_ENTRY_REJECTED",
            )
            path = Path(entry.name)
            check(
                not path.is_absolute()
                and ".." not in path.parts
                and path.parts[0] == "harbor"
                and entry.size <= 8 * 1024 * 1024,
                "ARCHIVE_PATH_REJECTED",
            )
        tar.extractall(destination, members=members, filter="data")


def prepare_certificates(c, secrets_value, certdir):
    certdir.mkdir(mode=0o700, exist_ok=True)
    certificate, key, ca = (
        certdir / "server.crt",
        certdir / "server.key",
        certdir / "ca.crt",
    )
    if c["tls"]["mode"] == "provided":
        for p, value in [
            (certificate, secrets_value["tls_certificate"]),
            (key, secrets_value["tls_private_key"]),
            (ca, secrets_value["tls_ca"]),
        ]:
            if p.exists():
                check(
                    p.read_text() == value,
                    "CERTIFICATE_CHANGE_REQUIRES_SEPARATE_OPERATION",
                )
            else:
                atomic(p, value)
    elif not certificate.exists():
        check(
            not key.exists() and not ca.exists(),
            "PARTIAL_CERTIFICATE_REQUIRES_RECOVERY",
        )
        hostname = urlsplit(c["endpoint"]["url"]).hostname
        import ipaddress

        try:
            ipaddress.ip_address(hostname)
            san = "IP:" + hostname
        except ValueError:
            san = "DNS:" + hostname
        command(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-nodes",
                "-days",
                "3650",
                "-subj",
                "/CN=Oneport Harbor CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-keyout",
                str(certdir / "ca.key"),
                "-out",
                str(ca),
            ]
        )
        command(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:3072",
                "-nodes",
                "-subj",
                "/CN=" + hostname,
                "-keyout",
                str(key),
                "-out",
                str(certdir / "server.csr"),
            ]
        )
        atomic(
            certdir / "extensions",
            "subjectAltName="
            + san
            + "\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n",
        )
        command(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(certdir / "server.csr"),
                "-CA",
                str(ca),
                "-CAkey",
                str(certdir / "ca.key"),
                "-CAcreateserial",
                "-days",
                "365",
                "-extfile",
                str(certdir / "extensions"),
                "-out",
                str(certificate),
            ]
        )
    for p in certdir.iterdir():
        p.chmod(0o600)
    command(
        ["openssl", "x509", "-in", str(certificate), "-checkend", "86400", "-noout"]
    )
    command(["openssl", "verify", "-CAfile", str(ca), str(certificate)])
    return ca.read_text()


def install(c, node, secrets_value, releases, harbor_yaml):
    root, state_path = paths(c)
    private_dir(state_path)
    with locked(state_path):
        report = preflight(c, node, releases)
        marker = {
            "machine_id": node["machine_id"],
            "uid": node["uid"],
            "plan_digest": c["plan_digest"],
            "installation_digest": c["installation_digest"],
            "mount": report["mount"],
        }
        previous = read(state_path / "installation.json")
        if previous:
            check(previous["mount"] == report["mount"], "MOUNT_IDENTITY_CHANGED")
        if previous and previous.get("complete"):
            for filename, expected_hash in previous["managed_files"].items():
                p = Path(filename)
                check(
                    p.is_file()
                    and not p.is_symlink()
                    and hashlib.sha256(p.read_bytes()).hexdigest() == expected_hash,
                    "MANAGED_FILE_CHANGED",
                )
            check(
                (root / "harbor.yml").read_text() == harbor_yaml,
                "HARBOR_SETTINGS_CHANGED",
            )
            ca = prepare_certificates(c, secrets_value, root / "certificates")
            command(
                [
                    "systemctl",
                    "is-active",
                    "--quiet",
                    "oneport-harbor-" + c["target"]["installation_id"],
                ]
            )
            return {
                "status": "installed",
                "changed": False,
                "trusted_ca_pem": ca,
                "trusted_ca_sha256": hashlib.sha256(ca.encode()).hexdigest(),
                "service": "oneport-harbor-" + c["target"]["installation_id"],
                "reason_codes": [],
            }
        save(state_path / "installation.json", marker)
        if report["planned_packages"]:
            # No package removals or distribution upgrades are permitted.
            key = state_path / "docker.asc"
            download(
                "https://download.docker.com/linux/ubuntu/gpg",
                releases["docker_key_sha256"],
                key,
            )
            Path("/etc/apt/keyrings").mkdir(exist_ok=True, mode=0o755)
            check(
                not Path("/etc/apt/keyrings/oneport-docker.asc").is_symlink(),
                "APT_PATH_CONFLICT",
            )
            atomic("/etc/apt/keyrings/oneport-docker.asc", key.read_bytes(), 0o644)
            repo = "/etc/apt/sources.list.d/oneport-docker.sources"
            content = "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/oneport-docker.asc\n"
            if Path(repo).exists():
                check(Path(repo).read_text() == content, "APT_SOURCE_CONFLICT")
            atomic(repo, content, 0o644)
            command(["apt-get", "update"], timeout=300)
            packages = [
                key + "=" + value for key, value in releases["docker_packages"].items()
            ]
            simulation, _ = command(
                [
                    "apt-get",
                    "--simulate",
                    "install",
                    "--no-install-recommends",
                    *packages,
                ]
            )
            check(
                not any(
                    line.startswith(("Remv ", "Inst containerd ", "Inst runc "))
                    for line in simulation.splitlines()
                ),
                "RUNTIME_PACKAGE_CONFLICT",
            )
            command(
                [
                    "apt-get",
                    "install",
                    "-y",
                    "--no-remove",
                    "--no-install-recommends",
                    *packages,
                ],
                timeout=900,
            )
        command(["docker", "info", "--format", "{{.ServerVersion}}"])
        archive = state_path / "installer.tgz"
        download(releases["installer_url"], releases["installer_sha256"], archive)
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=root.parent) as tmp:
                safe_extract(archive, tmp)
                os.rename(Path(tmp) / "harbor", root)
            root.chmod(0o700)
        data = Path(c["install"]["data_path"])
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        ca = prepare_certificates(c, secrets_value, root / "certificates")
        config_path = root / "harbor.yml"
        if config_path.exists():
            check(config_path.read_text() == harbor_yaml, "HARBOR_SETTINGS_CHANGED")
        atomic(config_path, harbor_yaml)
        compose = root / "managed-compose.json"
        if not compose.exists():
            command(["bash", "./prepare"], cwd=root, timeout=600)
            if secrets_value.get("oidc_ca"):
                trustdir = root / "common/config/shared/trust-certificates"
                trustdir.mkdir(parents=True, exist_ok=True)
                atomic(
                    trustdir / "oneport-keycloak.crt", secrets_value["oidc_ca"], 0o644
                )
            output, _ = command(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yml",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=root,
            )
            model = json.loads(output)
            model.pop("name", None)
            for section in ("networks", "volumes"):
                for name, value in model.get(section, {}).items():
                    check(
                        not value.get("external"), "EXTERNAL_COMPOSE_RESOURCE_REJECTED"
                    )
                    value["name"] = (
                        "oneport-harbor-" + c["target"]["installation_id"] + "_" + name
                    )
            for service in model["services"].values():
                service.pop("container_name", None)
                service["restart"] = "on-failure"
                if service.get("logging", {}).get("driver", "json-file") == "json-file":
                    service["logging"] = {
                        "driver": "json-file",
                        "options": {"max-size": "10m", "max-file": "3"},
                    }
            atomic(compose, json.dumps(model))
        ident = c["target"]["installation_id"]
        unit = "oneport-harbor-" + ident
        docker = shutil.which("docker")
        compose_cmd = f"{docker} compose -p oneport-harbor-{ident} -f {compose}"
        guard = root / "mount-check.py"
        atomic(
            guard,
            "import json,subprocess\nexpected="
            + repr(marker["mount"])
            + '\nactual=json.loads(subprocess.check_output(["findmnt","--json","--target",'
            + repr(str(data))
            + ',"--output","TARGET,SOURCE,FSTYPE"]))["filesystems"][0]\nassert expected==actual, "Harbor data mount changed"\n',
        )
        service = f"[Unit]\nDescription=Oneport Harbor {ident}\nRequires=docker.service\nAfter=docker.service network-online.target\nRequiresMountsFor={data}\n[Service]\nType=oneshot\nRemainAfterExit=yes\nWorkingDirectory={root}\nExecStartPre=/usr/bin/python3 {guard}\nExecStart={compose_cmd} up -d\nExecStop={compose_cmd} stop\nTimeoutStartSec=600\n[Install]\nWantedBy=multi-user.target\n"
        service_path = Path("/etc/systemd/system") / (unit + ".service")
        if service_path.exists():
            check(service_path.read_text() == service, "SERVICE_SETTINGS_CONFLICT")
        atomic(service_path, service, 0o644)
        command(["systemctl", "daemon-reload"])
        command(["systemctl", "enable", "--now", unit], timeout=660)
        command(["systemctl", "is-active", "--quiet", unit])
        marker["complete"] = True
        marker["managed_files"] = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                config_path,
                compose,
                service_path,
                guard,
                *(root / "certificates").iterdir(),
            ]
            if p.is_file()
        }
        save(state_path / "installation.json", marker)
        return {
            "status": "installed",
            "changed": True,
            "trusted_ca_pem": ca,
            "trusted_ca_sha256": hashlib.sha256(ca.encode()).hexdigest(),
            "service": unit,
            "reason_codes": [],
        }


def trust(c, node, ca, apply=False):
    identity(c, node)
    check(
        os.geteuid() == 0
        and "-----BEGIN CERTIFICATE-----" in ca
        and "PRIVATE KEY" not in ca,
        "PUBLIC_CA_REQUIRED",
    )
    authority = urlsplit(c["endpoint"]["url"]).netloc
    key = digest(
        {"endpoint": c["endpoint"]["url"], "organization": c["organization_id"]}
    )[:20]
    state_path = Path("/var/lib/harbor-infra-trust") / key
    k3s = Path("/etc/rancher/k3s").is_dir() and bool(shutil.which("k3s"))
    runtime = node.get("container_runtime_version", "")
    engine = (
        ("k3s" if k3s else "containerd")
        if runtime.startswith("containerd://")
        else "docker"
        if runtime.startswith("docker://")
        else None
    )
    check(engine is not None, "UNSUPPORTED_RUNTIME")
    config_file = None
    if engine == "k3s":
        config_file = Path("/etc/rancher/k3s/registries.yaml")
        try:
            import yaml
        except ImportError:
            if not apply:
                return {
                    "status": "partially_ready",
                    "node_name": node["name"],
                    "runtime": "k3s",
                    "planned_packages": ["python3-yaml"],
                    "reason_codes": ["PYTHON3_YAML_REQUIRED"],
                }
            os_release = Path("/etc/os-release").read_text()
            check(
                "ID=ubuntu" in os_release and 'VERSION_ID="24.04"' in os_release,
                "HELPER_INSTALL_UNSUPPORTED_OS",
            )
            command(["apt-get", "update"], timeout=300)
            command(
                [
                    "apt-get",
                    "install",
                    "-y",
                    "--no-remove",
                    "--no-install-recommends",
                    "python3-yaml",
                ],
                timeout=300,
            )
            import importlib

            importlib.invalidate_caches()
            yaml = importlib.import_module("yaml")
        content = (
            yaml.safe_load(config_file.read_text()) or {}
            if config_file.exists()
            else {}
        )
        check(isinstance(content, dict), "REGISTRY_CONFIG_INVALID")
    elif engine == "containerd":
        import tomllib

        content = tomllib.loads(Path("/etc/containerd/config.toml").read_text())
        plugins = content.get("plugins", {})
        registry = plugins.get(
            "io.containerd.cri.v1.images", plugins.get("io.containerd.grpc.v1.cri", {})
        ).get("registry", {})
        check(
            registry.get("config_path") == "/etc/containerd/certs.d",
            "CONTAINERD_CONFIG_PATH_REQUIRES_REVIEW",
        )
        config_file = Path("/etc/containerd/certs.d") / authority / "hosts.toml"
    else:
        config_file = Path("/etc/docker/certs.d") / authority / "ca.crt"
    for p in (config_file, *config_file.parents):
        check(not p.is_symlink(), "TRUST_PATH_SYMLINK")
    observed_digest = (
        hashlib.sha256(config_file.read_bytes()).hexdigest()
        if config_file.exists()
        else None
    )
    expected = {
        "endpoint": c["endpoint"]["url"],
        "uid": node["uid"],
        "machine_id": node["machine_id"],
        "ca_digest": hashlib.sha256(ca.encode()).hexdigest(),
        "engine": engine,
    }
    old = read(state_path / "managed.json")
    if old:
        check(
            all(old.get(k) == v for k, v in expected.items()), "TRUST_SETTINGS_CHANGED"
        )
    if engine != "k3s" and config_file.exists() and not old:
        if engine == "docker" and config_file.read_text() == ca:
            pass
        else:
            raise HarborError("EXISTING_TRUST_SETTINGS_CONFLICT")
    if engine == "k3s":
        entry = content.get("configs", {}).get(authority, {})
        check(not entry or old is not None, "EXISTING_TRUST_SETTINGS_CONFLICT")
        check(
            not entry.get("tls", {}).get("insecure_skip_verify"),
            "INSECURE_EXISTING_TRUST",
        )
    if not apply:
        return {
            "status": "preflight_passed",
            "runtime": engine,
            "node_name": node["name"],
            "restart_required": engine == "k3s" and not (old or {}).get("activated"),
            "reason_codes": [],
        }
    private_dir(state_path)
    with locked(state_path):
        current_digest = (
            hashlib.sha256(config_file.read_bytes()).hexdigest()
            if config_file.exists()
            else None
        )
        check(
            observed_digest == current_digest
            and read(state_path / "managed.json") == old,
            "TRUST_CONCURRENT_UPDATE",
        )
        if old:
            check(
                old.get("config_digest") == current_digest,
                "TRUST_SETTINGS_MODIFIED_EXTERNALLY",
            )
        if not (state_path / "original.saved").exists():
            if config_file.exists():
                atomic(state_path / "original", config_file.read_bytes())
            atomic(
                state_path / "original.saved",
                "present" if config_file.exists() else "absent",
            )
        ca_path = state_path / "ca.crt"
        atomic(ca_path, ca, 0o644)
        # Certificate is public, but this directory remains root accessible; runtimes run as root.
        config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if engine == "k3s":
            content.setdefault("configs", {}).setdefault(authority, {}).setdefault(
                "tls", {}
            )["ca_file"] = str(ca_path)
            desired = yaml.safe_dump(content, sort_keys=False)
        elif engine == "containerd":
            desired = (
                "server = "
                + json.dumps(c["endpoint"]["url"])
                + "\n[host."
                + json.dumps(c["endpoint"]["url"])
                + ']\n  capabilities = ["pull", "resolve"]\n  ca = '
                + json.dumps(str(ca_path))
                + "\n"
            )
        else:
            desired = ca
        changed = not config_file.exists() or config_file.read_text() != desired
        atomic(config_file, desired, 0o600 if engine == "k3s" else 0o644)
        activated = (
            engine != "k3s" or not changed and bool((old or {}).get("activated"))
        )
        save(
            state_path / "managed.json",
            {
                **expected,
                "activated": activated,
                "config_digest": hashlib.sha256(desired.encode()).hexdigest(),
            },
        )
        if not activated and node["name"] in c["trust"]["restart_approved"]:
            _, server = command(
                ["systemctl", "is-active", "--quiet", "k3s"], accepted=(0, 3, 4)
            )
            service = "k3s" if server == 0 else "k3s-agent"
            command(["systemctl", "is-active", "--quiet", service])
            command(["systemctl", "restart", service], timeout=180)
            activated = True
            save(
                state_path / "managed.json",
                {
                    **expected,
                    "activated": True,
                    "config_digest": hashlib.sha256(desired.encode()).hexdigest(),
                },
            )
        return {
            "status": "trust_configured" if activated else "partially_ready",
            "changed": changed,
            "runtime": engine,
            "node_name": node["name"],
            "trust_activated": activated,
            "runtime_pull_verified": False,
            "restart_required": not activated,
            "reason_codes": [] if activated else ["RUNTIME_RESTART_APPROVAL_REQUIRED"],
        }


def verify_host(c, node):
    identity(c, node)
    root, state_path = paths(c)
    with locked(state_path):
        state = read(state_path / "installation.json")
        check(
            state
            and state["machine_id"] == node["machine_id"]
            and state["uid"] == node["uid"],
            "MANAGED_INSTALLATION_REQUIRED",
        )
        check(
            mount(c["install"]["data_path"]) == state["mount"], "MOUNT_IDENTITY_CHANGED"
        )
        unit = "oneport-harbor-" + c["target"]["installation_id"]
        if c["verification"]["restart_harbor"]:
            command(["systemctl", "restart", unit], timeout=660)
        command(["systemctl", "is-active", "--quiet", unit])
        return {
            "status": "installed",
            "service_active": True,
            "restart_verified": c["verification"]["restart_harbor"],
            "reason_codes": [],
        }
