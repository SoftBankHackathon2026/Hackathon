from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from migration.image_migration.config import Config
from migration.image_migration.plan import plan
from migration.image_migration.registry import (
    OCI_IMAGE,
    OCI_INDEX,
    Registry,
    Skopeo,
    digest,
)
from migration.image_migration.runner import Harbor, apply, journal, verify

pytestmark = pytest.mark.skipif(
    os.environ.get("ONEPORT_IMAGE_TEST_REGISTRY") != "1",
    reason="requires skopeo, docker-registry and openssl",
)


class LocalConfig(Config):
    @property
    def source_host(self):
        return self.profile


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def registry(path, cert, key):
    path.mkdir()
    host = f"127.0.0.1:{free_port()}"
    conf = path / "registry.json"
    conf.write_text(
        json.dumps(
            {
                "version": "0.1",
                "storage": {"filesystem": {"rootdirectory": str(path / "storage")}},
                "http": {
                    "addr": host,
                    "tls": {"certificate": str(cert), "key": str(key)},
                },
            }
        )
    )
    with (path / "registry.log").open("wb") as log:
        process = subprocess.Popen(
            ["docker-registry", "serve", str(conf)], stdout=log, stderr=log
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    break
                try:
                    with urlopen(
                        f"https://{host}/v2/",
                        context=ssl.create_default_context(cafile=cert),
                        timeout=1,
                    ):
                        break
                except (URLError, TimeoutError):
                    time.sleep(0.05)
            else:
                pytest.fail("registry did not become reachable")
            assert process.poll() is None, (path / "registry.log").read_text()
            yield host
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def oci_layout(root: Path):
    blobs = root / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')

    def write(content, media):
        data = content if isinstance(content, bytes) else json.dumps(content).encode()
        checksum = digest(data)
        (blobs / checksum.split(":")[1]).write_bytes(data)
        return {"mediaType": media, "digest": checksum, "size": len(data)}

    payload = b"registry migration integration test\n"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("payload.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    layer = write(
        gzip.compress(archive.getvalue(), mtime=0),
        "application/vnd.oci.image.layer.v1.tar+gzip",
    )
    manifests = []
    for architecture in ("amd64", "arm64"):
        settings = write(
            {
                "architecture": architecture,
                "os": "linux",
                "config": {},
                "rootfs": {"type": "layers", "diff_ids": [digest(archive.getvalue())]},
            },
            "application/vnd.oci.image.config.v1+json",
        )
        entry = write(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE,
                "config": settings,
                "layers": [layer],
            },
            OCI_IMAGE,
        )
        manifests.append(
            {**entry, "platform": {"os": "linux", "architecture": architecture}}
        )
    manifest_list = write(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": manifests,
        },
        OCI_INDEX,
    )
    (root / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        **manifest_list,
                        "annotations": {"org.opencontainers.image.ref.name": "seed"},
                    }
                ],
            }
        )
    )
    return manifest_list, manifests, layer


def test_real_skopeo_transfer_and_verified_blob_corruption_detection(
    tmp_path, monkeypatch
):
    for command in ("skopeo", "docker-registry", "openssl"):
        assert shutil.which(command), (
            f"missing required integration executable: {command}"
        )
    ca = tmp_path / "ca.crt"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(ca),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    version = re.search(
        r"\d+\.\d+\.\d+", subprocess.check_output(["skopeo", "--version"], text=True)
    )[0]
    layout = tmp_path / "oci"
    root, children, layer = oci_layout(layout)

    with (
        registry(tmp_path / "source", ca, key) as src,
        registry(tmp_path / "target", ca, key) as dst,
    ):
        src_repo = "team/demo"
        subprocess.run(
            [
                "skopeo",
                "copy",
                "--all",
                "--preserve-digests",
                "--dest-tls-verify=true",
                f"oci:{layout}:seed",
                f"docker://{src}/{src_repo}:release",
            ],
            check=True,
            capture_output=True,
        )
        config = LocalConfig(
            "integration",
            "123456789012",
            "ap-northeast-2",
            src,
            f"https://{dst}",
            "ghost",
            "registry-01",
            "migrated",
            {},
            tmp_path / "robots.json",
            ca,
            True,
            version,
            60,
            tmp_path / "state",
        )
        config.credentials_ref.write_text(
            json.dumps(
                {
                    "binding": {
                        "organization_id": config.organization,
                        "installation_id": config.installation,
                        "endpoint": config.endpoint,
                    },
                    "accounts": {
                        f"migrated/{role}": {
                            "project": "migrated",
                            "role": role,
                            "username": f"robot$migrated+{role}",
                            "secret": "fixture-only",
                            "pending": False,
                            "expires_at": time.time() + 120,
                        }
                        for role in ("writer", "reader")
                    },
                }
            )
        )
        config.credentials_ref.chmod(0o600)
        inventory = {
            "repositories": [
                {
                    "repository": src_repo,
                    "images": [
                        {
                            "digest": entry["digest"],
                            "tags": ["release"] if entry is root else [],
                            "media_type": entry["mediaType"],
                            "artifact_type": None,
                            "state": "ACTIVE",
                            "size": entry["size"],
                        }
                        for entry in [root, *children]
                    ],
                }
            ]
        }
        ecr = SimpleNamespace(
            inventory=lambda: inventory,
            authorization=lambda **_kw: base64.b64encode(b"AWS:fixture-only").decode(),
        )
        harbor = Harbor(config)
        source = Registry(src, ecr.authorization)
        catalog = plan(config, inventory, source, harbor.registry)
        copier = Skopeo(config)
        with journal(config, root["digest"]) as connection:
            result = apply(config, catalog, ecr, source, harbor, copier, connection)
            assert result["status"] == "copied", result
            result = apply(config, catalog, ecr, source, harbor, copier, connection)
            assert result["items"][0]["status"] == "already_present", result
            result = verify(config, catalog, ecr, harbor, connection)
            assert result["status"] == "verified", result
            assert {item["source_digest"] for item in result["verified_mappings"]} == {
                entry["digest"] for entry in [root, *children]
            }
            sha = layer["digest"].split(":")[1]
            data = (
                tmp_path
                / "target"
                / "storage"
                / "docker"
                / "registry"
                / "v2"
                / "blobs"
                / "sha256"
                / sha[:2]
                / sha
                / "data"
            )
            data.write_bytes(b"x" * layer["size"])
            result = verify(config, catalog, ecr, harbor, connection)
            assert result["status"] == "partial", result
            assert result["verified_mappings"] == []
            assert all(
                item["reason"] == "BLOB_DIGEST_MISMATCH" for item in result["items"]
            )
