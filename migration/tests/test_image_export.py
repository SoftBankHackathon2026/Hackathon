from __future__ import annotations

import base64
import json
import sqlite3
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from migration.image_export import __main__ as cli
from migration.image_export import source as harbor_source
from migration.image_export.config import Config, load_config
from migration.image_export.plan import plan
from migration.image_export.runner import apply
from migration.image_export.source import Harbor
from migration.image_export.target import Target
from migration.image_migration.config import MigrationError
from migration.image_migration.registry import OCI_IMAGE, OCI_INDEX, digest
from migration.image_migration.runner import journal, verify

ACCOUNT = "0" * 12
SECRET_ENV = "ONEPORT_TEST_HARBOR_SECRET"
SECRET = "fixture-secret"
ROBOT = "robot$team+export"
CONFIG_MEDIA = "application/vnd.oci.image.config.v1+json"
LAYER_MEDIA = "application/vnd.oci.image.layer.v1.tar+gzip"


@pytest.fixture
def export_config(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    return Config(
        "export-001",
        "https://harbor.invalid",
        ("team",),
        ROBOT,
        "env",
        SECRET_ENV,
        None,
        ACCOUNT,
        "ap-northeast-2",
        None,
        "onprem",
        True,
        True,
        "AES256",
        None,
        True,
        "1.18.0",
        30,
        tmp_path / "state",
    )


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def response(code, body=b"", headers=None):
    stream = BytesIO(body)
    stream.code = code
    stream.headers = Message()
    for name, value in (headers or {}).items():
        stream.headers[name] = value
    return stream


class FakeHarbor:
    def __init__(self, host):
        self.host = host
        self.repositories = {}
        self.manifests = {}
        self.blobs = {}
        self.requests = []
        self.failure = None
        self.total_offset = 0

    def blob(self, data, media):
        identity = digest(data)
        self.blobs[identity] = data
        return {"mediaType": media, "digest": identity, "size": len(data)}

    def add(self, repo, doc):
        raw = json.dumps(doc, sort_keys=True).encode()
        identity = digest(raw)
        self.manifests[(repo, identity)] = (identity, doc)
        return {"mediaType": doc["mediaType"], "digest": identity, "size": len(raw)}

    def artifact(self, repo, descriptor, tags=(), accessories=()):
        self.repositories.setdefault(repo, []).append(
            {
                "record": {
                    "digest": descriptor["digest"],
                    "manifest_media_type": descriptor["mediaType"],
                    "media_type": CONFIG_MEDIA,
                    "size": descriptor["size"],
                    "accessories": [
                        {
                            "digest": item["digest"],
                            "subject_artifact_digest": descriptor["digest"],
                            "type": "signature.cosign",
                            "size": item["size"],
                        }
                        for item in accessories
                    ],
                },
                "tags": list(tags),
            }
        )
        for tag in tags:
            self.manifests[(repo, tag)] = self.manifests[(repo, descriptor["digest"])]

    def manifest(self, repo, reference):
        return self.manifests.get((repo, reference))

    def open(self, url, headers):
        expected = base64.b64encode(f"{ROBOT}:{SECRET}".encode()).decode()
        assert headers["Authorization"] == "Basic " + expected
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == f"https://{self.host}"
        self.requests.append(url)
        if self.failure:
            return response(self.failure)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        segments = parts.path.removeprefix("/api/v2.0/projects/").split("/")
        project = unquote(segments[0])
        if len(segments) == 2:
            rows = [
                {"name": name}
                for name in sorted(self.repositories)
                if name.startswith(project + "/")
            ]
        else:
            assert "/" not in unquote(segments[2])
            name = f"{project}/{unquote(unquote(segments[2]))}"
            artifacts = self.repositories[name]
            if len(segments) == 4:
                rows = [entry["record"] for entry in artifacts]
            else:
                (entry,) = [
                    entry
                    for entry in artifacts
                    if entry["record"]["digest"] == segments[4]
                ]
                rows = [{"name": tag} for tag in entry["tags"]]
        page, size = int(query["page"]), int(query["page_size"])
        body = json.dumps(rows[(page - 1) * size : page * size]).encode()
        return response(
            200, body, {"X-Total-Count": str(len(rows) + self.total_offset)}
        )


class FakeECR:
    def __init__(self, config):
        self.config = config
        self.repositories = {}
        self.writes = []
        self.layer_sizes = {}

    def check(self, registryId, repositoryName):
        assert registryId == ACCOUNT
        if repositoryName not in self.repositories:
            raise ClientError("RepositoryNotFoundException")
        return self.repositories[repositoryName]

    def describe_repositories(self, registryId, repositoryNames):
        (name,) = repositoryNames
        repo = self.check(registryId, name)
        return {
            "repositories": [
                {
                    "repositoryName": name,
                    "registryId": ACCOUNT,
                    "repositoryUri": f"{self.config.target_host}/{name}",
                    **repo["settings"],
                }
            ]
        }

    def create_repository(self, registryId, repositoryName, **settings):
        assert registryId == ACCOUNT
        self.writes.append(("create_repository", repositoryName, settings))
        if repositoryName in self.repositories:
            raise ClientError("RepositoryAlreadyExistsException")
        self.repositories[repositoryName] = {
            "settings": settings,
            "tags": {},
            "manifests": {},
        }
        return {"repository": {"repositoryName": repositoryName}}

    def describe_images(self, registryId, repositoryName, imageIds):
        repo = self.check(registryId, repositoryName)
        (image,) = imageIds
        identity = image.get("imageDigest") or repo["tags"].get(image.get("imageTag"))
        if identity not in repo["manifests"]:
            raise ClientError("ImageNotFoundException")
        return {"imageDetails": [{"imageDigest": identity}]}

    def batch_check_layer_availability(self, registryId, repositoryName, layerDigests):
        self.check(registryId, repositoryName)
        (layer,) = layerDigests
        if layer not in self.layer_sizes:
            return {"layers": [], "failures": [{"layerDigest": layer}]}
        return {
            "layers": [
                {
                    "layerDigest": layer,
                    "layerAvailability": "AVAILABLE",
                    "layerSize": self.layer_sizes[layer],
                }
            ],
            "failures": [],
        }

    def get_authorization_token(self):
        return {
            "authorizationData": [
                {
                    "authorizationToken": base64.b64encode(b"AWS:fixture").decode(),
                    "expiresAt": datetime.now(UTC) + timedelta(hours=1),
                }
            ]
        }

    def manifest(self, repo, reference):
        stored = self.repositories.get(repo)
        if stored is None:
            return None
        identity = stored["tags"].get(reference, reference)
        doc = stored["manifests"].get(identity)
        return None if doc is None else (identity, doc)

    def push(self, harbor, source_repo, repo, identity, tag=None):
        stored = self.repositories[repo]
        identity, doc = harbor.manifest(source_repo, identity)
        stored["manifests"][identity] = doc
        if tag is not None:
            stored["tags"][tag] = identity
        if doc["mediaType"] == OCI_INDEX:
            for child in doc["manifests"]:
                self.push(harbor, source_repo, repo, child["digest"])
        else:
            for blob in [doc["config"], *doc["layers"]]:
                self.layer_sizes[blob["digest"]] = blob["size"]


class FakeCopier:
    def __init__(self, config, harbor, ecr):
        self.config, self.harbor, self.ecr = config, harbor, ecr
        self.copies = []

    def transfer(self, source, target, source_auth, target_auth, source_ca=None):
        robot = base64.b64encode(f"{ROBOT}:{SECRET}".encode()).decode()
        assert source_auth == (self.config.source_host, robot)
        assert target_auth[0] == self.config.target_host
        assert base64.b64decode(target_auth[1]).startswith(b"AWS:")
        assert source_ca == self.config.ca_ref
        location, identity = source.split("@")
        address, tag = target.rsplit(":", 1)
        source_repo = location.removeprefix(self.config.source_host + "/")
        repo = address.removeprefix(self.config.target_host + "/")
        self.copies.append((identity, tag))
        self.ecr.push(self.harbor, source_repo, repo, identity, tag)


@pytest.fixture
def world(export_config):
    harbor = FakeHarbor(export_config.source_host)
    repo = "team/api"
    settings = harbor.blob(b"image-config", CONFIG_MEDIA)
    layer = harbor.blob(b"layer-bytes", LAYER_MEDIA)
    child = harbor.add(
        repo, {"mediaType": OCI_IMAGE, "config": settings, "layers": [layer]}
    )
    index = harbor.add(repo, {"mediaType": OCI_INDEX, "manifests": [child]})
    standalone = harbor.add(
        repo,
        {
            "mediaType": OCI_IMAGE,
            "config": settings,
            "layers": [layer],
            "annotations": {"version": "single"},
        },
    )
    release = harbor.add(
        repo,
        {
            "mediaType": OCI_IMAGE,
            "config": settings,
            "layers": [layer],
            "annotations": {"version": "two"},
        },
    )
    signature = harbor.add(
        repo,
        {
            "mediaType": OCI_IMAGE,
            "config": settings,
            "layers": [],
            "subject": index,
        },
    )
    harbor.artifact(repo, index, ("stable", "v1"), (signature,))
    harbor.artifact(repo, standalone)
    harbor.artifact(repo, release, ("v2",))
    harbor.repositories["team/empty"] = []
    ecr = FakeECR(export_config)
    target = Target(export_config, ecr, ecr)
    return {
        "config": export_config,
        "harbor": harbor,
        "source": Harbor(export_config, harbor),
        "ecr": ecr,
        "target": target,
        "copier": FakeCopier(export_config, harbor, ecr),
        "index": index,
        "child": child,
        "standalone": standalone,
        "release": release,
        "signature": signature,
        "layer": layer,
    }


def items_by_digest(catalog, name="team/api"):
    (repo,) = [repo for repo in catalog["repositories"] if repo["source"] == name]
    return {item["digest"]: item for item in repo["items"]}


def write_config(tmp_path, **changes):
    text = (Path(__file__).parents[1] / "config.image-export.example.yaml").read_text()
    values = {
        "<harbor 주소>": "harbor.invalid",
        "<harbor 프로젝트>": "team",
        "<robot 계정 이름>": ROBOT,
        "<계정 ID>": ACCOUNT,
        "<리전>": "ap-northeast-2",
        "/var/lib/oneport/image-export": str(tmp_path / "state"),
        **changes,
    }
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    path = tmp_path / "export.yaml"
    path.write_text(text)
    return path


def test_example_config_loads_and_secret_must_be_a_reference(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.projects == ("team",)
    assert config.secret_kind == "file"
    assert config.target("team/api") == "onprem/team/api"
    assert config.target_host == f"{ACCOUNT}.dkr.ecr.ap-northeast-2.amazonaws.com"
    assert config.binding["tag_immutability"] is True
    for change in (
        {"file: /run/oneport/harbor-export-robot.secret": "secret: inline-value"},
        {"file: /run/oneport/harbor-export-robot.secret": "file: /run/a\n      env: B"},
        {"encryption: AES256": "encryption: AES256\n    kms_key: alias/key"},
        {"repository_prefix: onprem": "repository_prefix: Upper"},
        {"https://harbor.invalid": "http://harbor.invalid"},
    ):
        with pytest.raises(MigrationError, match="INVALID_CONFIG|INVALID_SOURCE"):
            load_config(write_config(tmp_path, **change))


def test_robot_secret_is_read_from_private_file_or_environment(
    export_config, tmp_path, monkeypatch
):
    robot = export_config.robot()
    assert robot.secret == SECRET and SECRET not in repr(robot)
    secret_file = tmp_path / "robot.secret"
    secret_file.write_text("file-secret\n")
    secret_file.chmod(0o600)
    from_file = replace(export_config, secret_kind="file", secret_ref=str(secret_file))
    assert from_file.robot().secret == "file-secret"
    secret_file.chmod(0o644)
    with pytest.raises(MigrationError, match="UNSAFE_HARBOR_CREDENTIALS"):
        from_file.robot()
    monkeypatch.delenv(SECRET_ENV)
    with pytest.raises(MigrationError, match="INVALID_HARBOR_CREDENTIALS"):
        export_config.robot()


def test_harbor_inventory_paginates_encodes_names_and_lists_accessories(
    world, monkeypatch
):
    monkeypatch.setattr(harbor_source, "PAGE_SIZE", 1)
    harbor = world["harbor"]
    nested = harbor.add("team/sub/app", {"mediaType": OCI_INDEX, "manifests": []})
    harbor.artifact("team/sub/app", nested, ("latest",))
    inventory = world["source"].inventory()
    names = [repo["repository"] for repo in inventory["repositories"]]
    assert names == ["team/api", "team/empty", "team/sub/app"]
    images = {
        image["digest"]: image for image in inventory["repositories"][0]["images"]
    }
    assert images[world["index"]["digest"]]["tags"] == ["stable", "v1"]
    assert images[world["standalone"]["digest"]]["tags"] == []
    assert images[world["signature"]["digest"]]["subject"] == world["index"]["digest"]
    assert all(image["state"] == "ACTIVE" for image in images.values())
    assert any("/repositories/sub%252Fapp/artifacts" in url for url in harbor.requests)
    assert SECRET not in json.dumps(inventory)


def test_harbor_listing_errors_never_look_like_an_empty_inventory(world):
    harbor = world["harbor"]
    harbor.total_offset = 1
    with pytest.raises(MigrationError, match="HARBOR_INVENTORY_CHANGED"):
        world["source"].inventory()
    harbor.total_offset = 0
    harbor.failure = 403
    with pytest.raises(MigrationError, match="HARBOR_API_FORBIDDEN"):
        world["source"].inventory()


def test_plan_is_read_only_and_reports_missing_repositories(world):
    source, ecr = world["source"], world["ecr"]
    catalog = plan(
        world["config"], source.inventory(), source.registry, world["target"]
    )
    assert ecr.writes == [] and ecr.repositories == {}
    assert world["copier"].copies == []
    assert {
        repo["target"]: repo["target_exists"] for repo in catalog["repositories"]
    } == {
        "onprem/team/api": False,
        "onprem/team/empty": False,
    }
    items = items_by_digest(catalog)
    assert items[world["index"]["digest"]]["children"] == [world["child"]["digest"]]
    assert items[world["signature"]["digest"]]["kind"] == "attachment"
    assert items[world["standalone"]["digest"]]["target_tags"] == [
        "oneport-untagged-" + world["standalone"]["digest"].replace(":", "-")
    ]
    assert not any(item["issues"] for item in items.values())


def run_plan(world, tmp_path, name="plan.json"):
    config, source = world["config"], world["source"]
    args = Namespace(operation="plan", config=None, output=tmp_path / name)
    loaded = cli.load_config
    try:
        cli.load_config = lambda _path: config
        result = cli.execute(args, source, world["target"])
    finally:
        cli.load_config = loaded
    return result, json.loads(args.output.read_text())


def run(world, operation, plan_path, plan_digest):
    loaded = cli.load_config
    try:
        cli.load_config = lambda _path: world["config"]
        return cli.execute(
            Namespace(
                operation=operation,
                config=None,
                plan=plan_path,
                plan_digest=plan_digest,
            ),
            world["source"],
            world["target"],
            world["copier"],
        )
    finally:
        cli.load_config = loaded


def test_apply_creates_repositories_copies_resumes_and_verifies(world, tmp_path):
    ecr, copier = world["ecr"], world["copier"]
    planned, _ = run_plan(world, tmp_path)
    assert planned["status"] == "planned" and planned["repositories_to_create"] == 2
    plan_path, plan_digest = tmp_path / "plan.json", planned["plan_digest"]

    first = run(world, "apply", plan_path, plan_digest)
    assert first["status"] == "copied", first
    assert [row["status"] for row in first["repositories"]] == ["created", "created"]
    assert ecr.writes[0] == (
        "create_repository",
        "onprem/team/api",
        {
            "imageTagMutability": "IMMUTABLE",
            "imageScanningConfiguration": {"scanOnPush": True},
            "encryptionConfiguration": {"encryptionType": "AES256"},
        },
    )
    assert len(copier.copies) == 4
    assert {row["status"] for row in first["items"] if row["status"] != "copied"} == {
        "excluded_attachment"
    }

    resumed = run(world, "apply", plan_path, plan_digest)
    assert resumed["status"] == "copied"
    assert len(copier.copies) == 4
    assert [row["status"] for row in resumed["repositories"]] == [
        "existing",
        "existing",
    ]
    assert {
        row["status"] for row in resumed["items"] if row.get("previous_status")
    } == {"already_present"}

    checked = run(world, "verify", plan_path, plan_digest)
    assert checked["status"] == "verified", checked
    assert {row["target_tag"] for row in checked["verified_mappings"]} == {
        "stable",
        "v1",
        "v2",
        "oneport-untagged-" + world["standalone"]["digest"].replace(":", "-"),
    }
    assert all(
        row["target_digest_address"].startswith(world["config"].target_host)
        for row in checked["verified_mappings"]
    )


def test_verify_detects_ecr_digest_and_layer_mismatch_and_source_drift(world, tmp_path):
    ecr, harbor = world["ecr"], world["harbor"]
    planned, _ = run_plan(world, tmp_path)
    plan_path, plan_digest = tmp_path / "plan.json", planned["plan_digest"]
    run(world, "apply", plan_path, plan_digest)
    stored = ecr.repositories["onprem/team/api"]

    stored["tags"]["v2"] = world["standalone"]["digest"]
    checked = run(world, "verify", plan_path, plan_digest)
    assert checked["status"] == "partial"
    assert {
        row["target_tag"]
        for row in checked["items"]
        if row.get("reason") == "TARGET_DIGEST_MISMATCH"
    } == {"v2"}
    stored["tags"]["v2"] = world["release"]["digest"]

    layer = world["layer"]["digest"]
    ecr.layer_sizes[layer] += 1
    checked = run(world, "verify", plan_path, plan_digest)
    assert not checked["verified_mappings"]
    assert {row.get("reason") for row in checked["items"]} >= {"BLOB_SIZE_MISMATCH"}
    ecr.layer_sizes[layer] -= 1

    entry = harbor.repositories["team/api"][2]
    entry["tags"] = ["v3"]
    checked = run(world, "verify", plan_path, plan_digest)
    assert checked["status"] == "partial"
    assert checked["source_drift"]["changed_images"][0]["after_tags"] == ["v3"]
    assert "v2" not in {row["target_tag"] for row in checked["verified_mappings"]}


def test_existing_tag_with_other_digest_is_blocked_and_never_overwritten(
    world, tmp_path
):
    ecr = world["ecr"]
    ecr.repositories["onprem/team/api"] = {
        "settings": {
            "imageTagMutability": "MUTABLE",
            "imageScanningConfiguration": {"scanOnPush": True},
            "encryptionConfiguration": {"encryptionType": "AES256"},
        },
        "tags": {"v2": world["standalone"]["digest"]},
        "manifests": {world["standalone"]["digest"]: {}},
    }
    planned, catalog = run_plan(world, tmp_path)
    repo = catalog["repositories"][0]
    assert repo["target_exists"] is True
    assert repo["settings_mismatch"] == ["imageTagMutability"]
    items = items_by_digest(catalog)
    assert items[world["release"]["digest"]]["issues"] == ["TARGET_TAG_CONFLICT"]
    assert planned["repositories_to_create"] == 1

    result = run(world, "apply", tmp_path / "plan.json", planned["plan_digest"])
    assert result["status"] == "partial"
    blocked = [row for row in result["items"] if row["status"] == "blocked"]
    assert [(row["target_tag"], row["reason"]) for row in blocked] == [
        ("v2", "TARGET_TAG_CONFLICT")
    ]
    assert (
        ecr.repositories["onprem/team/api"]["tags"]["v2"]
        == (world["standalone"]["digest"])
    )
    assert "v2" not in {tag for _, tag in world["copier"].copies}


def test_apply_requires_exclusive_write_and_rejects_a_changed_plan(world, tmp_path):
    planned, _ = run_plan(world, tmp_path)
    plan_path = tmp_path / "plan.json"
    world["config"] = replace(world["config"], exclusive_write=False)
    with pytest.raises(MigrationError, match="EXCLUSIVE_DESTINATION_WRITE_REQUIRED"):
        run(world, "apply", plan_path, planned["plan_digest"])
    world["config"] = replace(world["config"], exclusive_write=True, scan_on_push=False)
    with pytest.raises(MigrationError, match="PLAN_BINDING_MISMATCH"):
        run(world, "apply", plan_path, planned["plan_digest"])
    with pytest.raises(MigrationError, match="PLAN_DIGEST_MISMATCH|PLAN_READ_FAILED"):
        run(world, "apply", plan_path, "sha256:" + "0" * 64)


def test_repository_failure_blocks_its_images(world, tmp_path):
    planned, _ = run_plan(world, tmp_path)

    def denied(**_kwargs):
        raise ClientError("AccessDeniedException")

    world["ecr"].create_repository = denied
    config = world["config"]
    doc = json.loads((tmp_path / "plan.json").read_text())
    with journal(config, planned["plan_digest"], "ecr") as connection:
        result = apply(
            config, doc, world["source"], world["target"], world["copier"], connection
        )
    assert result["status"] == "partial"
    assert {row["reason"] for row in result["repositories"]} == {
        "ECR_REPOSITORY_CREATE_FAILED"
    }
    assert world["copier"].copies == []


def test_ecr_api_calls_match_the_service_model(export_config):
    session = pytest.importorskip("botocore.session")
    stubber = pytest.importorskip("botocore.stub")
    client = session.get_session().create_client(
        "ecr",
        region_name=export_config.region,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    config = replace(export_config, encryption="KMS", kms_key="alias/oneport-export")
    name = "onprem/team/api"
    with stubber.Stubber(client) as stub:
        stub.add_client_error(
            "describe_repositories",
            service_error_code="RepositoryNotFoundException",
            expected_params={"registryId": ACCOUNT, "repositoryNames": [name]},
        )
        stub.add_response(
            "create_repository",
            {"repository": {"repositoryName": name}},
            {
                "registryId": ACCOUNT,
                "repositoryName": name,
                "imageTagMutability": "IMMUTABLE",
                "imageScanningConfiguration": {"scanOnPush": True},
                "encryptionConfiguration": {
                    "encryptionType": "KMS",
                    "kmsKey": "alias/oneport-export",
                },
            },
        )
        stub.add_client_error(
            "describe_images",
            service_error_code="ImageNotFoundException",
            expected_params={
                "registryId": ACCOUNT,
                "repositoryName": name,
                "imageIds": [{"imageTag": "v1"}],
            },
        )
        identity = digest(b"layer")
        stub.add_response(
            "batch_check_layer_availability",
            {
                "layers": [
                    {
                        "layerDigest": identity,
                        "layerAvailability": "AVAILABLE",
                        "layerSize": 5,
                    }
                ],
                "failures": [],
            },
            {"registryId": ACCOUNT, "repositoryName": name, "layerDigests": [identity]},
        )
        target = Target(config, client)
        assert target.ensure_repository(name) == "created"
        assert target.tag_digest(name, "v1") is None
        target.verify_layer(name, identity, 5)
        stub.assert_no_pending_responses()


def test_skopeo_receives_robot_for_harbor_and_token_for_ecr(
    export_config, tmp_path, monkeypatch
):
    from migration.image_migration import registry

    ca = tmp_path / "harbor-ca.pem"
    ca.write_text("fixture-ca")
    config = replace(export_config, ca_ref=ca)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-pass")
    monkeypatch.setattr(
        registry.subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"skopeo version 1.18.0\n",
    )
    seen = {}

    def run_copy(argv, env, **_kwargs):
        seen["argv"] = argv
        seen["env"] = env
        flag = argv.index("--src-authfile")
        seen["source"] = json.loads(Path(argv[flag + 1]).read_text())
        seen["target"] = json.loads(
            Path(argv[argv.index("--dest-authfile") + 1]).read_text()
        )
        seen["ca"] = (
            Path(argv[argv.index("--src-cert-dir") + 1]) / "registry-ca.crt"
        ).read_text()
        return type("Done", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(registry.subprocess, "run", run_copy)
    robot = base64.b64encode(f"{ROBOT}:{SECRET}".encode()).decode()
    registry.Skopeo(config).transfer(
        f"{config.source_host}/team/api@{digest(b'x')}",
        f"{config.target_host}/onprem/team/api:v1",
        (config.source_host, robot),
        (config.target_host, "ecr-token"),
        source_ca=ca,
    )
    assert seen["source"] == {"auths": {config.source_host: {"auth": robot}}}
    assert seen["target"] == {"auths": {config.target_host: {"auth": "ecr-token"}}}
    assert seen["ca"] == "fixture-ca"
    assert "--dest-cert-dir" not in seen["argv"]
    assert {"--all", "--preserve-digests"} <= set(seen["argv"])
    assert SECRET not in " ".join(seen["argv"])
    assert "AWS_SECRET_ACCESS_KEY" not in seen["env"]


def test_cli_prints_one_json_result_and_exit_code(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, **{"<리전>": "not-a-region"})
    monkeypatch.setattr(
        "sys.argv",
        ["image_export", "plan", "--config", str(path), "--output", "plan.json"],
    )
    assert cli.main() == 3
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert json.loads(out[0]) == {
        "schema_version": 1,
        "operation": "plan",
        "status": "failed",
        "reason": "INVALID_CONFIG",
    }


def test_verify_without_source_inventory_is_partial(world):
    def unreadable():
        raise MigrationError("HARBOR_API_FORBIDDEN")

    world["source"].inventory = unreadable
    result = verify(
        world["config"],
        {"repositories": []},
        world["source"],
        world["target"],
        sqlite3.connect(":memory:"),
    )
    assert result["status"] == "partial"
    assert result["source_inventory_error"] == "HARBOR_API_FORBIDDEN"
