from __future__ import annotations

import copy
from collections import defaultdict, deque

import pytest
from migration.identity.collect import (
    collect,
    pages,
    policy_document,
    validate_inventory,
)
from migration.identity.common import IdentityError


class ScriptedClient:
    def __init__(self):
        self.responses = defaultdict(deque)
        self.calls = []

    def add(self, method, response):
        self.responses[method].append(response)

    def __getattr__(self, method):
        def call(**kwargs):
            self.calls.append((method, kwargs))
            response = self.responses[method].popleft()
            if isinstance(response, Exception):
                raise response
            return copy.deepcopy(response)

        return call


def clients(config):
    c = {
        name: ScriptedClient() for name in ("sso-admin", "identitystore", "iam", "sts")
    }
    s = config["source"]
    ps = "arn:aws:sso:::permissionSet/ssoins-1234567890123456/ps-1234567890123456"
    c["sso-admin"].add(
        "list_instances",
        {
            "Instances": [
                {
                    "InstanceArn": s["instance_arn"],
                    "IdentityStoreId": s["identity_store_id"],
                }
            ]
        },
    )
    c["identitystore"].add(
        "list_users",
        {"Users": [{"UserId": "u-1", "UserName": "alice"}], "NextToken": "page2"},
    )
    c["identitystore"].add(
        "list_users", {"Users": [{"UserId": "u-2", "UserName": "bob"}]}
    )
    c["identitystore"].add(
        "list_groups", {"Groups": [{"GroupId": "g-1", "DisplayName": "Developers"}]}
    )
    c["identitystore"].add(
        "list_group_memberships",
        {"GroupMemberships": [{"MemberId": {"UserId": "u-1"}}]},
    )
    c["sts"].add("get_caller_identity", {"Account": s["account_ids"][0]})
    c["iam"].add(
        "get_account_authorization_details",
        {
            "RoleDetailList": [
                {
                    "Arn": "arn:aws:iam::111122223333:role/app",
                    "RoleId": "r-1",
                    "RoleName": "app",
                }
            ],
            "IsTruncated": True,
            "Marker": "next",
        },
    )
    c["iam"].add(
        "get_account_authorization_details",
        {"UserDetailList": [], "IsTruncated": False},
    )
    c["sso-admin"].add("list_permission_sets", {"PermissionSets": [ps]})
    c["sso-admin"].add(
        "describe_permission_set", {"PermissionSet": {"Name": "ReadOrders"}}
    )
    c["sso-admin"].add(
        "get_inline_policy_for_permission_set",
        {
            "InlinePolicy": '{"Statement":{"Effect":"Allow","Action":"s3:GetObject","Resource":"*"}}'
        },
    )
    c["sso-admin"].add(
        "list_managed_policies_in_permission_set", {"AttachedManagedPolicies": []}
    )
    c["sso-admin"].add(
        "list_customer_managed_policy_references_in_permission_set",
        {"CustomerManagedPolicyReferences": [{"Name": "Orders", "Path": "/team/"}]},
    )
    c["sso-admin"].add("get_permissions_boundary_for_permission_set", {})
    c["sso-admin"].add(
        "list_account_assignments",
        {"AccountAssignments": [{"PrincipalType": "GROUP", "PrincipalId": "g-1"}]},
    )
    c["sts"].add("get_caller_identity", {"Account": s["account_ids"][0]})
    c["iam"].add("get_policy", {"Policy": {"DefaultVersionId": "v2"}})
    c["iam"].add(
        "get_policy_version",
        {
            "PolicyVersion": {
                "Document": "%7B%22Statement%22%3A%7B%22Effect%22%3A%22Allow%22%2C%22Action%22%3A%22s3%3AGetObject%22%2C%22Resource%22%3A%22*%22%7D%7D"
            }
        },
    )

    class Session:
        def client(self, name, **kwargs):
            return c[name]

    return c, lambda **kwargs: Session()


def test_paginated_collection_resolves_policy_in_account(identity_config):
    c, factory = clients(identity_config)
    inventory = collect(identity_config, factory)
    validate_inventory(inventory)
    assert inventory["status"] == "complete"
    assert len(inventory["users"]) == 2
    assert inventory["iam_principals"][0]["kind"] == "iam_role"
    assert inventory["iam_principals"][0]["migration_status"].startswith(
        "requires_separate"
    )
    assert c["identitystore"].calls[1][1]["NextToken"] == "page2"
    assert c["iam"].calls[1][1]["Marker"] == "next"
    policy = inventory["policies"][0]["documents"][0]
    assert policy["arn"] == "arn:aws:iam::111122223333:policy/team/Orders"
    assert isinstance(policy["document"], dict)


def test_collection_errors_are_unknown_not_empty_or_leaked(identity_config):
    c, factory = clients(identity_config)
    c["identitystore"].responses["list_group_memberships"].clear()
    c["identitystore"].add(
        "list_group_memberships", RuntimeError("credential-VERY-SECRET")
    )
    result = collect(identity_config, factory)
    assert result["status"] == "partial"
    assert any(x["status"] == "unknown" for x in result["coverage"])
    assert "credential-VERY-SECRET" not in str(result)


def test_wrong_account_does_not_resolve_customer_policy(identity_config):
    c, factory = clients(identity_config)
    c["sts"].responses["get_caller_identity"][-1] = {"Account": "999900001111"}
    result = collect(identity_config, factory)
    assert not result["policies"] and result["status"] == "partial"
    assert not any(method == "get_policy" for method, args in c["iam"].calls)


def test_repeated_page_token_is_rejected():
    client = ScriptedClient()
    client.add("list_users", {"Users": [], "NextToken": "again"})
    client.add("list_users", {"Users": [], "NextToken": "again"})
    with pytest.raises(IdentityError, match="AWS_PAGINATION_LOOP"):
        list(pages(client, "list_users", "Users"))


def test_botocore_stubber_checks_real_api_shapes():
    boto3 = pytest.importorskip(
        "boto3",
        reason="AWS SDK checks run in the separate identity controller environment",
    )
    from botocore.stub import Stubber

    client = boto3.client(
        "sso-admin",
        region_name="ap-northeast-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    args = {
        "InstanceArn": "arn:aws:sso:::instance/ssoins-1234567890123456",
        "PermissionSetArn": "arn:aws:sso:::permissionSet/ssoins-1234567890123456/ps-1234567890123456",
        "AccountId": "111122223333",
    }
    with Stubber(client) as stub:
        stub.add_response(
            "list_account_assignments",
            {"AccountAssignments": [], "NextToken": "next"},
            args,
        )
        stub.add_response(
            "list_account_assignments",
            {"AccountAssignments": []},
            {**args, "NextToken": "next"},
        )
        assert (
            list(
                pages(client, "list_account_assignments", "AccountAssignments", **args)
            )
            == []
        )
        stub.assert_no_pending_responses()


def test_policy_documents_decode_without_double_decoding():
    assert policy_document({"Statement": []}) == {"Statement": []}
    assert policy_document('{"Statement": []}') == {"Statement": []}
