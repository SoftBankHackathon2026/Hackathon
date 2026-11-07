from __future__ import annotations

import copy

import pytest
from migration.secrets.common import (
    digest,
    save,
    seal_artifact,
    source_fingerprint,
    validate_config,
)
from migration.secrets.models import SecretValue
from migration.secrets.plan import build_plan, preflight

ACCOUNT = "111122223333"
ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:orders-AbCdEf"
PS = "arn:aws:sso:::permissionSet/ssoins-1234567890123456/ps-1234567890123456"
PATH = "migration/orders/dev/database"
CANARY = b"PRIVATE-SECRET-CANARY"


class Source:
    def __init__(self, inventory):
        self.data = inventory
        self.values = {
            "v1": SecretValue(b"previous", "string"),
            "v2": SecretValue(CANARY, "string"),
            "v0": SecretValue(b"omitted", "binary"),
        }
        self.reads = []
        self.change_during_read = False

    def inventory(self):
        result = copy.deepcopy(self.data)
        result["source_fingerprint"] = source_fingerprint(result)
        return seal_artifact(result, "inventory_hash")

    def read(self, arn, version):
        assert arn == ARN
        self.reads.append(version)
        if self.change_during_read:
            self.data["resources"][0]["description"] = "changed"
        return self.values[version]


class Target:
    def __init__(self):
        self.docs = {}
        self.writes = 0
        self.crash = None
        self.observed = {
            "cluster_id": "cluster-test",
            "accessor": "kv-test",
            "mount": "kv",
            "version": "2.7.2",
        }

    def inspect(self, paths):
        assert paths == [PATH]
        return copy.deepcopy(self.observed)

    def read_document(self, path):
        return copy.deepcopy(self.docs.get(path))

    def write_document(self, path, document, version):
        from migration.secrets.common import SecretsError

        old = self.docs.get(path)
        if (old[1] if old else 0) != version:
            raise SecretsError("TARGET_WRITE_CONFLICT")
        if self.crash == "before":
            self.crash = None
            raise SecretsError("TARGET_UNAVAILABLE")
        self.writes += 1
        self.docs[path] = (copy.deepcopy(document), version + 1)
        if self.crash == "after":
            self.crash = None
            raise SecretsError("TARGET_UNAVAILABLE")
        return version + 1


@pytest.fixture
def scenario(tmp_path):
    from types import SimpleNamespace

    config = validate_config(
        {
            "schema_version": 1,
            "migration_id": "orders",
            "source": {
                "account_id": ACCOUNT,
                "region": "ap-northeast-2",
                "include_names": ["orders"],
            },
            "target": {
                "endpoint": "https://bao.test",
                "expected_version": "2.7.2",
                "cluster_id": "cluster-test",
                "mount": "kv",
                "prefix": "migration",
                "ca_file": str(tmp_path / "ca.pem"),
                "token_ref": "env:TEST_BAO_TOKEN",
            },
            "preparation": {
                "configure_result": str(tmp_path / "configured.json"),
                "verify_result": str(tmp_path / "verified.json"),
            },
            "identity": {
                "config": str(tmp_path / "identity-config.json"),
                "inventory": str(tmp_path / "identity-inventory.json"),
                "plan": str(tmp_path / "identity-plan.json"),
            },
        }
    )
    save(
        config["preparation"]["configure_result"],
        {
            "schema_version": 1,
            "operation": "configure",
            "status": "partially_ready",
            "request_id": "test",
            "secret_transfer_performed": False,
            "reports": [{"kv_mount": "kv", "path_prefix": "migration"}],
        },
    )
    save(
        config["preparation"]["verify_result"],
        {
            "schema_version": 1,
            "operation": "verify",
            "status": "partially_ready",
            "request_id": "test",
            "reports": [
                {
                    "checks": {
                        "service": "passed",
                        "migration": "passed",
                        "application": "not_run",
                    }
                }
            ],
        },
    )
    ic = {"targets": {"openbao": {"base_url": "https://bao.test"}}}
    assignment = {
        "account_id": ACCOUNT,
        "permission_set_arn": PS,
        "principal_type": "GROUP",
        "principal_id": "g-stable",
    }
    ii = {
        "schema_version": 1,
        "kind": "identity_inventory",
        "collected_at": "2026-11-07T00:00:00+00:00",
        "source": {"account_ids": [ACCOUNT], "identity_store_id": "d-test"},
        "policies": [
            {
                "account_id": ACCOUNT,
                "permission_set_arn": PS,
                "documents": [
                    {
                        "document": {
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": "secretsmanager:GetSecretValue",
                                    "Resource": ARN,
                                }
                            ]
                        }
                    }
                ],
            }
        ],
        "coverage": [{"scope": "policies", "status": "complete"}],
    }
    seal_artifact(ii, "inventory_hash")
    ip = {
        "schema_version": 1,
        "kind": "identity_plan",
        "status": "planned",
        "blockers": [],
        "config_hash": digest(ic),
        "inventory_hash": ii["inventory_hash"],
        "mapping_rows": [
            {
                "assignment_id": "assignment-1",
                "source": assignment,
                "business": "orders",
                "environment": "dev",
                "target_group": "orders-reader",
                "targets": {
                    "openbao": [{"path": "kv/data/" + PATH, "capabilities": ["read"]}]
                },
                "review_ref": "REVIEW-EXAMPLE",
                "source_access_evidence": "Fictional source review evidence",
            }
        ],
        "resources": [
            {
                "id": "bao-policy",
                "kind": "bao_policy",
                "payload": {
                    "paths": [{"path": "kv/data/" + PATH, "capabilities": ["read"]}]
                },
            },
            {
                "id": "bao-role",
                "kind": "bao_role",
                "name": "identity-test/reader",
                "payload": {"bound_claims": {"groups": "orders-reader"}},
            },
        ],
    }
    seal_artifact(ip, "plan_hash")
    for k, value in (("config", ic), ("inventory", ii), ("plan", ip)):
        save(config["identity"][k], value)
    inventory = {
        "schema_version": 1,
        "kind": "secret_inventory",
        "collected_at": "2026-11-07T00:00:00+00:00",
        "source": {"account_id": ACCOUNT, "region": "ap-northeast-2"},
        "resources": [
            {
                "arn": ARN,
                "name": "orders",
                "description": "example",
                "tags": [{"Key": "team", "Value": "orders"}],
                "versions": [
                    {"id": "v0", "stages": []},
                    {"id": "v1", "stages": ["AWSPREVIOUS"]},
                    {"id": "v2", "stages": ["AWSCURRENT"]},
                ],
                "kms": {"id": None, "coverage": "aws_managed_key"},
                "resource_policy": None,
                "rotation_enabled": False,
                "managed_by_service": None,
                "scheduled_deletion": False,
            }
        ],
        "coverage": [],
        "limitations": ["NOT_EFFECTIVE_AUTHORIZATION"],
    }
    source = Source(inventory)
    inventory = source.inventory()
    mapping = {
        "schema_version": 1,
        "inventory_hash": inventory["inventory_hash"],
        "bindings": [
            {
                "source_arn": ARN,
                "target_path": PATH,
                "business": "orders",
                "environment": "dev",
                "identity_assignments": ["assignment-1"],
                "review_ref": "TEST-ONLY",
            }
        ],
    }
    plan = build_plan(config, inventory, mapping)
    target = Target()
    prepared = preflight(config, plan, source, target)
    return SimpleNamespace(
        config=config,
        inventory=inventory,
        mapping=mapping,
        plan=plan,
        source=source,
        target=target,
        prepared=prepared,
        state=tmp_path / "copy.sqlite3",
        identity_config=ic,
        identity_inventory=ii,
        identity_plan=ip,
        directory=tmp_path,
    )
