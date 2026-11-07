from __future__ import annotations

import base64
import json
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from migration.image_migration.config import (
    Config,
    HarborCredentials,
    MigrationError,
    Robot,
)
from migration.image_migration.plan import plan, validate_plan
from migration.image_migration.registry import (
    OCI_IMAGE,
    OCI_INDEX,
    Registry,
    digest,
)
from migration.image_migration.runner import (
    apply,
    journal,
    read_plan,
    verify,
    write_artifact,
)
from migration.image_migration.source import ECR


@pytest.fixture
def config(tmp_path):
    return Config(
        "ecr-001",
        "123456789012",
        "ap-northeast-2",
        None,
        "https://harbor.example:8444",
        "ghost",
        "registry-01",
        "migrated",
        {},
        tmp_path / "credentials.json",
        None,
        True,
        "1.18.0",
        30,
        tmp_path / "state",
    )


def test_aws_discovery_reads_every_page_including_archived_and_untagged(config):
    session = pytest.importorskip("botocore.session")
    stubber = pytest.importorskip("botocore.stub")
    client = session.get_session().create_client(
        "ecr",
        region_name=config.region,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

    def repository(name):
        return {
            "repositoryName": name,
            "registryId": config.account,
            "repositoryUri": f"{config.source_host}/{name}",
        }

    def image(state, tags):
        return {
            "imageDigest": digest(state.encode()),
            "registryId": config.account,
            "repositoryName": "team/api",
            "imageStatus": state,
            "imageTags": tags,
        }

    account = {"registryId": config.account}
    filters = {"filter": {"tagStatus": "ANY", "imageStatus": "ANY"}}
    api = {"repositoryName": "team/api", **account, **filters}
    with stubber.Stubber(client) as stub:
        stub.add_response(
            "describe_repositories",
            {
                "repositories": [repository("team/api")],
                "nextToken": "repo-page",
            },
            account,
        )
        stub.add_response(
            "describe_images",
            {
                "imageDetails": [image("ARCHIVED", [])],
                "nextToken": "image-page",
            },
            api,
        )
        stub.add_response(
            "describe_images",
            {
                "imageDetails": [image("ACTIVE", ["stable", "latest"])],
            },
            {**api, "nextToken": "image-page"},
        )
        stub.add_response(
            "describe_repositories",
            {
                "repositories": [repository("team/empty")],
            },
            {**account, "nextToken": "repo-page"},
        )
        stub.add_response(
            "describe_images",
            {
                "imageDetails": [],
            },
            {"repositoryName": "team/empty", **account, **filters},
        )
        source = ECR(config, client)
        inventory = source.inventory()
        stub.assert_no_pending_responses()
    assert len(inventory["repositories"]) == 2
    images = {item["state"]: item for item in inventory["repositories"][0]["images"]}
    assert images["ACTIVE"]["tags"] == ["latest", "stable"]
    assert images["ARCHIVED"]["tags"] == []
    assert inventory["repositories"][1]["images"] == []


def test_aws_access_denied_does_not_report_an_empty_inventory(config):
    session = pytest.importorskip("botocore.session")
    stubber = pytest.importorskip("botocore.stub")
    client = session.get_session().create_client(
        "ecr",
        region_name=config.region,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with stubber.Stubber(client) as stub:
        stub.add_client_error(
            "describe_repositories", service_error_code="AccessDeniedException"
        )
        with pytest.raises(MigrationError, match="ECR_REPOSITORY_LIST_INCOMPLETE"):
            ECR(config, client).inventory()


def test_token_refreshes_before_expiration(config):
    values = [
        {
            "authorizationData": [
                {
                    "authorizationToken": base64.b64encode(b"AWS:old").decode(),
                    "expiresAt": datetime.now(UTC) + timedelta(seconds=30),
                }
            ]
        },
        {
            "authorizationData": [
                {
                    "authorizationToken": base64.b64encode(b"AWS:new").decode(),
                    "expiresAt": datetime.now(UTC) + timedelta(hours=1),
                }
            ]
        },
    ]
    source = ECR(config, SimpleNamespace(get_authorization_token=lambda: values.pop(0)))
    assert source.authorization() != source.authorization()
    assert source.authorization() == base64.b64encode(b"AWS:new").decode()


def test_harbor_credential_handoff_checks_role_binding_and_permissions(config):
    account = {
        "username": "robot$migrated+writer",
        "secret": "test-secret",
        "expires_at": time.time() + 60,
        "project": "migrated",
        "role": "writer",
        "pending": False,
    }
    config.credentials_ref.write_text(
        json.dumps(
            {
                "binding": {
                    "organization_id": config.organization,
                    "installation_id": config.installation,
                    "endpoint": config.endpoint,
                },
                "accounts": {"migrated/writer": account},
            }
        )
    )
    config.credentials_ref.chmod(0o600)
    assert (
        HarborCredentials(config).account("migrated", "writer").username
        == account["username"]
    )
    with pytest.raises(MigrationError, match="HARBOR_BINDING_MISMATCH"):
        HarborCredentials(replace(config, organization="other")).account(
            "migrated", "writer"
        )
    config.credentials_ref.chmod(0o644)
    with pytest.raises(MigrationError, match="UNSAFE_HARBOR_CREDENTIALS"):
        HarborCredentials(config).account("migrated", "writer")


class TestRegistryDouble:
    __test__ = False

    def __init__(self):
        self.manifests = {}
        self.blobs = {}

    def manifest(self, repo, ref):
        return self.manifests.get((repo, ref))

    def add(self, repo, value, tag=None):
        raw = json.dumps(value, sort_keys=True).encode()
        identity = digest(raw)
        self.manifests[(repo, identity)] = (identity, value)
        if tag:
            self.manifests[(repo, tag)] = (identity, value)
        return {"digest": identity, "size": len(raw)}

    def verify_blob(self, repo, expected, size):
        blob = self.blobs.get(expected)
        if blob is None or len(blob) != size or digest(blob) != expected:
            raise MigrationError("BLOB_DIGEST_MISMATCH")


def image_row(identity, *, tags=(), state="ACTIVE", media=OCI_IMAGE):
    return {
        "digest": identity,
        "tags": list(tags),
        "state": state,
        "media_type": media,
        "artifact_type": None,
        "size": 20,
    }


def test_plan_apply_resume_verify_and_source_drift(config, tmp_path):
    source_repo, target_repo = "team/api", "migrated/team/api"
    source, target = TestRegistryDouble(), TestRegistryDouble()
    data = b"image-config"
    settings = {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": digest(data),
        "size": len(data),
    }
    child = source.add(
        source_repo, {"mediaType": OCI_IMAGE, "config": settings, "layers": []}
    )
    multi = source.add(source_repo, {"mediaType": OCI_INDEX, "manifests": [child]})
    standalone = source.add(
        source_repo,
        {
            "mediaType": OCI_IMAGE,
            "config": settings,
            "layers": [],
            "annotations": {"version": "single"},
        },
    )
    attachment = source.add(
        source_repo,
        {
            "mediaType": OCI_IMAGE,
            "config": settings,
            "layers": [],
            "subject": child,
        },
    )
    archived = digest(b"archived")
    inventory = {
        "repositories": [
            {
                "repository": source_repo,
                "images": [
                    image_row(multi["digest"], tags=("stable", "v1"), media=OCI_INDEX),
                    image_row(child["digest"]),
                    image_row(standalone["digest"]),
                    image_row(attachment["digest"], tags=("sig",)),
                    image_row(archived, tags=("old",), state="ARCHIVED"),
                ],
            }
        ]
    }
    conflict = target.add(
        target_repo, {"mediaType": OCI_IMAGE, "config": settings}, "v1"
    )
    catalog = plan(config, inventory, source, lambda _project: target)
    validate_plan(config, catalog)
    items = {item["digest"]: item for item in catalog["repositories"][0]["items"]}
    assert items[child["digest"]]["target_tags"] == []
    assert items[standalone["digest"]]["target_tags"] == [
        "oneport-untagged-" + standalone["digest"].replace(":", "-")
    ]
    assert items[attachment["digest"]]["kind"] == "attachment"
    assert "TARGET_TAG_CONFLICT" in items[multi["digest"]]["issues"]

    plan_path = tmp_path / "plan.json"
    plan_digest = write_artifact(plan_path, catalog)
    assert read_plan(plan_path, plan_digest, config) == catalog
    with pytest.raises(MigrationError, match="OUTPUT_ALREADY_EXISTS"):
        write_artifact(plan_path, {})
    with pytest.raises(MigrationError, match="PLAN_BINDING_MISMATCH"):
        read_plan(plan_path, plan_digest, replace(config, project="other"))

    copied = []

    def copy(src, dest, auth, robot):
        assert auth == "fake-auth" and robot.secret == "fake-secret"
        identity = src.split("@")[1]
        tag = dest.rsplit(":", 1)[1]
        copied.append((identity, tag))
        target.manifests[(target_repo, tag)] = source.manifest(source_repo, identity)
        target.manifests[(target_repo, identity)] = source.manifest(
            source_repo, identity
        )
        if identity == multi["digest"]:
            target.manifests[(target_repo, child["digest"])] = source.manifest(
                source_repo, child["digest"]
            )
        target.blobs[settings["digest"]] = data

    ecr = SimpleNamespace(
        inventory=lambda: inventory, authorization=lambda **_kw: "fake-auth"
    )
    harbor = SimpleNamespace(
        registry=lambda *_args: target,
        credentials=SimpleNamespace(
            account=lambda *_args: Robot("robot$writer", "fake-secret")
        ),
    )
    copier = SimpleNamespace(copy=copy)
    with journal(config, plan_digest) as connection:
        first = apply(config, catalog, ecr, source, harbor, copier, connection)
        resumed = apply(config, catalog, ecr, source, harbor, copier, connection)
        assert first["status"] == "partial"
        assert resumed["status"] == "partial"
        assert [
            (row["reason"], row["status"])
            for row in first["items"]
            if row.get("reason")
        ] == [
            ("TARGET_TAG_CONFLICT", "blocked"),
            ("SOURCE_RESTORE_PENDING", "blocked"),
        ]
        assert any(row.get("previous_status") == "copied" for row in resumed["items"])
        assert target.manifest(target_repo, "v1")[0] == conflict["digest"]
        assert len(copied) == 2

        checks = verify(config, catalog, ecr, harbor, connection)
        assert checks["status"] == "partial"
        assert {row["target_tag"] for row in checks["verified_mappings"]} == {
            "stable",
            items[standalone["digest"]]["target_tags"][0],
            None,
        }
        assert any(
            row.get("reason") == "SOURCE_RESTORE_PENDING" for row in checks["items"]
        )

        inventory["repositories"][0]["images"][0]["tags"] = ["new", "v1"]
        checks = verify(config, catalog, ecr, harbor, connection)
        assert checks["source_drift"]["changed_images"][0]["after_tags"] == [
            "new",
            "v1",
        ]
        assert "stable" not in {
            row["target_tag"] for row in checks["verified_mappings"]
        }
        assert len(checks["verified_mappings"]) == 2

        target.blobs[settings["digest"]] = b"corrupt-data"
        checks = verify(config, catalog, ecr, harbor, connection)
        assert not checks["verified_mappings"]
        assert any(
            row.get("reason") == "BLOB_DIGEST_MISMATCH" for row in checks["items"]
        )


def test_harbor_never_sends_credentials_to_an_untrusted_token_realm():
    registry = Registry(
        "harbor.example", lambda: pytest.fail("must not obtain credentials")
    )
    with pytest.raises(MigrationError, match="UNTRUSTED_TOKEN_ENDPOINT"):
        registry.token(
            'Bearer realm="https://wrong.example/token",service="harbor"', "project/app"
        )


def test_harbor_token_is_scoped_and_storage_redirect_drops_authorization(monkeypatch):
    from email.message import Message
    from io import BytesIO

    registry = Registry("harbor.example", lambda: "fixture-basic")
    repo = "project/team/app"
    blob = b"verified data"
    checksum = digest(blob)
    path = f"https://harbor.example/v2/{repo}/blobs/{checksum}"
    external = "https://storage.example/object?signature=fixture"
    query = "service=harbor-registry&scope=repository%3Aproject%2Fteam%2Fapp%3Apull"
    steps = iter(
        [
            (
                path,
                "Basic fixture-basic",
                401,
                {
                    "WWW-Authenticate": 'Bearer realm="https://harbor.example/service/token",service="harbor-registry"',
                },
                b"",
            ),
            (
                "https://harbor.example/service/token?" + query,
                "Basic fixture-basic",
                200,
                {},
                b'{"token":"fixture-bearer"}',
            ),
            (path, "Bearer fixture-bearer", 307, {"Location": external}, b""),
            (external, None, 200, {}, blob),
        ]
    )

    def open_response(url, headers):
        expected, auth, code, response_headers, body = next(steps)
        assert url == expected
        assert headers.get("Authorization") == auth
        response = BytesIO(body)
        response.code = code
        response.headers = Message()
        for name, value in response_headers.items():
            response.headers[name] = value
        return response

    monkeypatch.setattr(registry, "open", open_response)
    registry.verify_blob(repo, checksum, len(blob))
    assert list(steps) == []


def test_verify_never_reports_complete_when_source_cannot_be_read(config):
    def cannot_read():
        raise MigrationError("ECR_REPOSITORY_LIST_INCOMPLETE")

    connection = sqlite3.connect(":memory:")
    result = verify(
        config,
        {"repositories": []},
        SimpleNamespace(inventory=cannot_read),
        None,
        connection,
    )
    assert result["status"] == "partial"
    assert result["source_inventory_error"] == "ECR_REPOSITORY_LIST_INCOMPLETE"
