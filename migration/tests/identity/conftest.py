from __future__ import annotations

import copy

import pytest
from migration.identity.common import digest, validate_config
from migration.identity.plan import create_plan, mapping_template

ACCOUNT = "111122223333"
PS = "arn:aws:sso:::permissionSet/ssoins-1234567890123456/ps-1234567890123456"


@pytest.fixture
def identity_config():
    return validate_config(
        {
            "schema_version": 1,
            "migration_id": "test-orders",
            "source": {
                "provider": "aws-identity-center",
                "region": "ap-northeast-2",
                "instance_arn": "arn:aws:sso:::instance/ssoins-1234567890123456",
                "identity_store_id": "d-1234567890",
                "account_ids": [ACCOUNT],
            },
            "targets": {
                "keycloak": {
                    "base_url": "https://keycloak.test",
                    "realm": "identity-test",
                    "admin_token_env": "KC_TOKEN",
                },
                "openbao": {
                    "base_url": "https://bao.test",
                    "token_env": "BAO_TOKEN",
                    "auth_mount": "identity-test",
                    "audience": "identity-bao",
                },
                "kubernetes": {
                    "kubeconfig": "/run/platform/kubeconfig",
                    "context": "onprem",
                    "group_prefix": "kc:",
                    "audience": "identity-kubernetes",
                    "redirect_uris": ["http://localhost:8000/callback"],
                },
                "applications": [
                    {
                        "id": "orders",
                        "client_id": "identity-orders",
                        "redirect_uris": ["https://orders.test/callback"],
                    }
                ],
            },
        }
    )


@pytest.fixture
def identity_inventory(identity_config):
    inv = {
        "schema_version": 1,
        "kind": "identity_inventory",
        "collected_at": "2026-11-07T00:00:00+00:00",
        "source": copy.deepcopy(identity_config["source"]),
        "status": "complete",
        "users": [
            {"id": "u-1", "username": "alice", "display_name": "Alice", "emails": []}
        ],
        "groups": [{"id": "g-1", "name": "Developers"}],
        "memberships": [{"group_id": "g-1", "user_id": "u-1"}],
        "permission_sets": [{"arn": PS, "name": "OrdersReader"}],
        "assignments": [
            {
                "account_id": ACCOUNT,
                "permission_set_arn": PS,
                "principal_type": "GROUP",
                "principal_id": "g-1",
            }
        ],
        "policies": [
            {
                "account_id": ACCOUNT,
                "permission_set_arn": PS,
                "boundary": None,
                "documents": [
                    {
                        "arn": "inline:" + PS,
                        "document": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": "secretsmanager:GetSecretValue",
                                    "Resource": "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:orders-dev",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
        "coverage": [{"scope": "users", "status": "complete"}],
        "limitations": [
            "AWS_EFFECTIVE_PERMISSIONS_NOT_EVALUATED",
            "WORKLOAD_IDENTITIES_REQUIRE_SEPARATE_ADAPTER",
        ],
    }
    inv["inventory_hash"] = digest(inv)
    return inv


@pytest.fixture
def identity_mapping(identity_inventory):
    m = mapping_template(identity_inventory)
    b = m["bindings"][0]
    b.update(
        business="orders",
        environment="dev",
        review_ref="review-001",
        source_access_evidence="Operator-reviewed source resource, organization controls and EKS access evidence record.",
        targets={
            "kubernetes": {
                "namespace": "orders-dev",
                "rules": [
                    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]}
                ],
            },
            "openbao": [
                {"path": "kv/data/orders/dev/database", "capabilities": ["read"]}
            ],
            "applications": [{"id": "orders", "roles": ["reader"]}],
        },
    )
    return m


@pytest.fixture
def identity_plan(identity_config, identity_inventory, identity_mapping):
    return create_plan(identity_config, identity_inventory, identity_mapping)


class MemoryTargets:
    def __init__(self):
        self.items = {}
        self.writes = []
        self.deleted = []
        self.fail_after_write = None

    def read(self, resource):
        return copy.deepcopy(self.items.get(resource["id"]))

    def write(self, resource):
        from migration.identity.common import IdentityError

        self.items[resource["id"]] = copy.deepcopy(resource["payload"])
        self.writes.append(resource["id"])
        if resource["id"] == self.fail_after_write:
            self.fail_after_write = None
            raise IdentityError("TARGET_TRANSPORT_ERROR", status="failed")

    def delete(self, resource):
        del self.items[resource["id"]]
        self.deleted.append(resource["id"])


@pytest.fixture
def memory_targets():
    return MemoryTargets()
