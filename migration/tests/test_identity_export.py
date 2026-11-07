from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from migration.identity.common import IdentityError, digest, load, private_write
from migration.identity.execute import status
from migration.identity_export import __main__ as cli
from migration.identity_export import collect as collect_module
from migration.identity_export.adapters import IdentityCenter
from migration.identity_export.collect import collect, validate_inventory
from migration.identity_export.common import secret_file, validate_config
from migration.identity_export.execute import apply, preflight
from migration.identity_export.plan import create_plan, mapping_template
from migration.identity_export.report import markdown
from migration.identity_export.verify import verify

ClientError = pytest.importorskip("botocore.exceptions").ClientError

ACCOUNT = "111122223333"
OTHER_ACCOUNT = "444455556666"
INSTANCE = "arn:aws:sso:::instance/ssoins-0000000000000000"
STORE = "d-0000000000"
PS_READ = "arn:aws:sso:::permissionSet/ssoins-0000000000000000/ps-0000000000000001"
PS_ADMIN = "arn:aws:sso:::permissionSet/ssoins-0000000000000000/ps-0000000000000002"
SECRET = "kc-client-secret-value"
TOKEN = "kc-access-token"
EXAMPLES = Path(__file__).resolve().parents[1] / "identity-examples"


def not_found(operation):
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
        operation,
    )


def kc_user(uid, username, enabled=True, **extra):
    return {
        "id": uid,
        "username": username,
        "email": username + "@example.com",
        "firstName": "First-" + username,
        "lastName": "Last-" + username,
        "enabled": enabled,
        "emailVerified": True,
        **extra,
    }


class FakeKeycloak:
    def __init__(self):
        self.headers = {}
        self.forms = []
        self.calls = []
        self.fail = set()
        self.users = [
            kc_user("u-1", "user-a"),
            kc_user("u-2", "user-b"),
            kc_user("u-3", "user-c", enabled=False),
            kc_user("u-4", "user-d"),
            kc_user("u-5", "service-account-ci"),
        ]
        self.top = [
            {
                "id": "g-1",
                "name": "platform",
                "path": "/platform",
                "subGroupCount": 1,
                "subGroups": [],
            },
            {
                "id": "g-3",
                "name": "finance",
                "path": "/finance",
                "subGroups": [{"id": "g-4", "name": "audit", "path": "/finance/audit"}],
            },
            {"id": "g-5", "name": "unmapped", "path": "/unmapped"},
        ]
        self.children = {"g-1": [{"id": "g-2", "name": "dev", "path": "/platform/dev"}]}
        self.members = {
            "g-1": ["u-1"],
            "g-2": ["u-2", "u-3"],
            "g-3": [],
            "g-4": ["u-4"],
            "g-5": ["u-1", "u-4"],
        }
        self.group_roles = {
            "g-4": {
                "realmMappings": [{"name": "auditor"}],
                "clientMappings": {
                    "orders": {
                        "client": "orders",
                        "mappings": [{"name": "reader"}],
                    }
                },
            }
        }
        self.user_roles = {
            "u-1": {
                "realmMappings": [
                    {"name": "default-roles-identity-test"},
                    {"name": "operator"},
                ]
            }
        }

    def form(self, path, data):
        self.forms.append((path, dict(data)))
        return {"access_token": TOKEN, "token_type": "Bearer"}

    def request(self, method, path, *, payload=None, params=None, missing=False):
        assert method == "GET"
        assert self.headers == {"Authorization": "Bearer " + TOKEN}
        self.calls.append((path, dict(params or {})))
        prefix = "/admin/realms/identity-test"
        assert path.startswith(prefix)
        rest = path[len(prefix) :]
        if rest in self.fail:
            raise IdentityError("TARGET_HTTP_500", status="failed")
        parts = rest.strip("/").split("/")
        if parts == ["users"]:
            return self.page(self.users, params)
        if parts[0] == "users" and parts[2] == "role-mappings":
            return self.user_roles.get(parts[1], {})
        if parts == ["groups"]:
            return self.page(self.top, params)
        if parts[2] == "children":
            return self.page(self.children.get(parts[1], []), params)
        if parts[2] == "members":
            ids = self.members.get(parts[1], [])
            return self.page([{"id": i} for i in ids], params)
        if parts[2] == "role-mappings":
            return self.group_roles.get(parts[1], {})
        raise AssertionError(path)

    @staticmethod
    def page(items, params):
        first, size = params["first"], params["max"]
        return copy.deepcopy(items[first : first + size])


class FakeStore:
    def __init__(self):
        self.users = {}
        self.groups = {}
        self.memberships = set()
        self.calls = []
        self.lose_response = set()

    def log(self, name, kwargs):
        self.calls.append((name, kwargs))
        assert kwargs.pop("IdentityStoreId") == STORE

    def get_user_id(self, **kw):
        self.log("get_user_id", kw)
        attr = kw["AlternateIdentifier"]["UniqueAttribute"]
        for uid, u in self.users.items():
            if attr["AttributePath"] == "userName":
                if u["UserName"].lower() == attr["AttributeValue"].lower():
                    return {"UserId": uid, "IdentityStoreId": STORE}
            elif attr["AttributePath"] == "emails.value" and any(
                e["Value"].lower() == attr["AttributeValue"].lower()
                for e in u.get("Emails", [])
            ):
                return {"UserId": uid, "IdentityStoreId": STORE}
        raise not_found("GetUserId")

    def get_group_id(self, **kw):
        self.log("get_group_id", kw)
        attr = kw["AlternateIdentifier"]["UniqueAttribute"]
        assert attr["AttributePath"] == "displayName"
        for gid, g in self.groups.items():
            if g["DisplayName"] == attr["AttributeValue"]:
                return {"GroupId": gid, "IdentityStoreId": STORE}
        raise not_found("GetGroupId")

    def describe_user(self, **kw):
        self.log("describe_user", kw)
        if kw["UserId"] not in self.users:
            raise not_found("DescribeUser")
        return {"UserId": kw["UserId"], **copy.deepcopy(self.users[kw["UserId"]])}

    def describe_group(self, **kw):
        self.log("describe_group", kw)
        return {"GroupId": kw["GroupId"], **copy.deepcopy(self.groups[kw["GroupId"]])}

    def get_group_membership_id(self, **kw):
        self.log("get_group_membership_id", kw)
        if (kw["GroupId"], kw["MemberId"]["UserId"]) in self.memberships:
            return {"MembershipId": "m-" + kw["GroupId"], "IdentityStoreId": STORE}
        raise not_found("GetGroupMembershipId")

    def create_user(self, **kw):
        self.log("create_user", kw)
        uid = "aws-u-" + str(len(self.users) + 1)
        self.users[uid] = copy.deepcopy(kw)
        if kw["UserName"] in self.lose_response:
            self.lose_response.discard(kw["UserName"])
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": SECRET}},
                "CreateUser",
            )
        return {"UserId": uid, "IdentityStoreId": STORE}

    def create_group(self, **kw):
        self.log("create_group", kw)
        gid = "aws-g-" + str(len(self.groups) + 1)
        self.groups[gid] = copy.deepcopy(kw)
        return {"GroupId": gid, "IdentityStoreId": STORE}

    def create_group_membership(self, **kw):
        self.log("create_group_membership", kw)
        self.memberships.add((kw["GroupId"], kw["MemberId"]["UserId"]))
        return {"MembershipId": "m", "IdentityStoreId": STORE}

    def count(self, name):
        return sum(1 for n, _ in self.calls if n == name)


class FakeSso:
    def __init__(self):
        self.permission_sets = {PS_READ: "ReadOnly", PS_ADMIN: "Admin"}
        self.assignments = set()
        self.requests = {}
        self.calls = []
        self.polls_before_success = 0
        self.final_status = "SUCCEEDED"

    def list_instances(self, **kw):
        self.calls.append(("list_instances", kw))
        return {"Instances": [{"InstanceArn": INSTANCE, "IdentityStoreId": STORE}]}

    def list_permission_sets(self, **kw):
        self.calls.append(("list_permission_sets", kw))
        assert kw["InstanceArn"] == INSTANCE
        arns = sorted(self.permission_sets)
        if "NextToken" not in kw:
            return {"PermissionSets": arns[:1], "NextToken": "page-2"}
        return {"PermissionSets": arns[1:]}

    def describe_permission_set(self, **kw):
        self.calls.append(("describe_permission_set", kw))
        arn = kw["PermissionSetArn"]
        return {
            "PermissionSet": {
                "PermissionSetArn": arn,
                "Name": self.permission_sets[arn],
            }
        }

    def list_account_assignments(self, **kw):
        self.calls.append(("list_account_assignments", kw))
        return {
            "AccountAssignments": [
                {
                    "AccountId": account,
                    "PermissionSetArn": arn,
                    "PrincipalType": kind,
                    "PrincipalId": principal,
                }
                for account, arn, kind, principal in sorted(self.assignments)
                if account == kw["AccountId"] and arn == kw["PermissionSetArn"]
            ]
        }

    def create_account_assignment(self, **kw):
        self.calls.append(("create_account_assignment", kw))
        assert kw["TargetType"] == "AWS_ACCOUNT" and kw["InstanceArn"] == INSTANCE
        request_id = "req-" + str(len(self.requests) + 1)
        key = (
            kw["TargetId"],
            kw["PermissionSetArn"],
            kw["PrincipalType"],
            kw["PrincipalId"],
        )
        self.requests[request_id] = {"key": key, "polls": 0}
        return {
            "AccountAssignmentCreationStatus": {
                "Status": "IN_PROGRESS",
                "RequestId": request_id,
            }
        }

    def describe_account_assignment_creation_status(self, **kw):
        self.calls.append(("describe_account_assignment_creation_status", kw))
        request = self.requests[kw["AccountAssignmentCreationRequestId"]]
        request["polls"] += 1
        if request["polls"] <= self.polls_before_success:
            state = "IN_PROGRESS"
        else:
            state = self.final_status
            if state == "SUCCEEDED":
                self.assignments.add(request["key"])
        return {
            "AccountAssignmentCreationStatus": {
                "Status": state,
                "RequestId": kw["AccountAssignmentCreationRequestId"],
                "FailureReason": "provider detail " + SECRET,
            }
        }

    def count(self, name):
        return sum(1 for n, _ in self.calls if n == name)


class FakeSession:
    def __init__(self, store, sso):
        self.store, self.sso = store, sso

    def client(self, name, **options):
        return {"identitystore": self.store, "sso-admin": self.sso}[name]


@pytest.fixture
def secret_path(tmp_path):
    path = tmp_path / "kc-secret"
    path.write_text(SECRET + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def config(secret_path):
    return validate_config(
        {
            "schema_version": 1,
            "migration_id": "test-export",
            "source": {
                "provider": "keycloak",
                "base_url": "https://keycloak.test",
                "realm": "identity-test",
                "client_id": "identity-export-reader",
                "client_secret_file": str(secret_path),
            },
            "target": {
                "provider": "aws-identity-center",
                "region": "ap-northeast-2",
                "instance_arn": INSTANCE,
                "identity_store_id": STORE,
                "account_ids": [ACCOUNT],
            },
        }
    )


@pytest.fixture
def keycloak():
    return FakeKeycloak()


@pytest.fixture
def inventory(config, keycloak):
    return collect(config, keycloak)


@pytest.fixture
def aws():
    return FakeStore(), FakeSso()


@pytest.fixture
def center(config, aws):
    sleeps = []
    c = IdentityCenter(
        config,
        lambda **kw: FakeSession(*aws),
        sleep=sleeps.append,
        poll_interval=0,
        poll_attempts=4,
    )
    c.sleeps = sleeps
    return c


def binding(m, kind, assignments, review_ref="REVIEW-1", **source):
    b = next(
        x
        for x in m["bindings"]
        if x["source"]["kind"] == kind
        and all(x["source"].get(k) == v for k, v in source.items())
    )
    b["assignments"] = assignments
    b["review_ref"] = review_ref
    return b


@pytest.fixture
def mapping(inventory):
    m = mapping_template(inventory)
    binding(
        m,
        "group",
        [{"account_id": ACCOUNT, "permission_set": "ReadOnly"}],
        path="/platform",
    )
    binding(
        m,
        "realm_role",
        [{"account_id": ACCOUNT, "permission_set": PS_ADMIN}],
        role="auditor",
    )
    return m


@pytest.fixture
def plan(config, inventory, mapping, center):
    return create_plan(config, inventory, mapping, center)


def kinds(plan, kind):
    return [r for r in plan["resources"] if r["kind"] == kind]


def run_apply(config, plan, center, state_dir):
    prepared = preflight(config, plan, center)
    assert prepared["status"] == "complete", prepared["blockers"]
    return apply(config, plan, prepared, state_dir, center)


def test_collect_uses_service_account_and_walks_nested_groups(
    config, keycloak, monkeypatch
):
    monkeypatch.setattr(collect_module, "PAGE", 2)
    inv = collect(config, keycloak)
    path, data = keycloak.forms[0]
    assert path == "/realms/identity-test/protocol/openid-connect/token"
    assert data == {
        "grant_type": "client_credentials",
        "client_id": "identity-export-reader",
        "client_secret": SECRET,
    }
    assert inv["status"] == "complete"
    assert {g["path"]: g["parent_id"] for g in inv["groups"]} == {
        "/platform": None,
        "/platform/dev": "g-1",
        "/finance": None,
        "/finance/audit": "g-3",
        "/unmapped": None,
    }
    assert any(p.endswith("/groups/g-1/children") for p, _ in keycloak.calls)
    users = {u["username"]: u for u in inv["users"]}
    assert len(users) == 5 and users["user-c"]["enabled"] is False
    assert users["service-account-ci"]["service_account"] is True
    assert {"group_id": "g-2", "user_id": "u-3"} in inv["memberships"]
    assert {r["role"] for r in inv["group_roles"]} == {"auditor", "reader"}
    assert inv["user_roles"] == [
        {"user_id": "u-1", "kind": "realm_role", "role": "operator"}
    ]
    assert SECRET not in json.dumps(inv) and TOKEN not in json.dumps(inv)
    assert validate_inventory(inv) is inv


def test_secret_file_must_be_private_and_absolute(secret_path):
    assert secret_file(str(secret_path)) == SECRET
    secret_path.chmod(0o644)
    with pytest.raises(IdentityError) as error:
        secret_file(str(secret_path))
    assert error.value.code == "PRIVATE_FILE_REQUIRED"
    with pytest.raises(IdentityError):
        secret_file("relative/secret")


def test_config_rejects_unsafe_inputs(config):
    for path, value, code in (
        (("source", "realm"), "master", "BUSINESS_REALM_REQUIRED"),
        (("source", "base_url"), "http://keycloak.test", "HTTPS_URL_REQUIRED"),
        (("source", "client_secret_file"), "secret", "ABSOLUTE_FILE_REQUIRED"),
        (("target", "account_ids"), ["12345"], "ACCOUNT_ID"),
    ):
        broken = copy.deepcopy(config)
        broken[path[0]][path[1]] = value
        with pytest.raises(IdentityError) as error:
            validate_config(broken)
        assert error.value.code == code
    broken = copy.deepcopy(config)
    broken["source"]["client_secret"] = SECRET
    with pytest.raises(IdentityError):
        validate_config(broken)


def test_partial_collection_blocks_plan(config, keycloak, center):
    keycloak.fail.add("/groups/g-2/members")
    inv = collect(config, keycloak)
    assert inv["status"] == "partial"
    m = mapping_template(inv)
    p = create_plan(config, inv, m, center)
    assert p["status"] == "blocked"
    assert {"code": "SOURCE_COVERAGE_INCOMPLETE", "scope": "members:g-2"} in p[
        "blockers"
    ]


def test_plan_assigns_only_mapped_sources_and_excludes_the_rest(plan):
    assert plan["status"] == "planned", plan["blockers"]
    assert {r["name"] for r in kinds(plan, "group")} == {
        "keycloak/platform",
        "keycloak-realm-role/auditor",
    }
    assignments = {
        (r["payload"]["group"], r["payload"]["permission_set_arn"])
        for r in kinds(plan, "assignment")
    }
    assert assignments == {
        ("keycloak/platform", PS_READ),
        ("keycloak-realm-role/auditor", PS_ADMIN),
    }
    excluded = {
        (e["kind"], json.dumps(e.get("source"), sort_keys=True), e["reason"])
        for e in plan["exclusions"]
        if "source" in e and e["kind"] != "user_role"
    }
    for kind, source in (
        ("group", {"kind": "group", "path": "/unmapped"}),
        ("group", {"kind": "group", "path": "/platform/dev"}),
        ("group", {"kind": "group", "path": "/finance/audit"}),
        ("client_role", {"kind": "client_role", "client": "orders", "role": "reader"}),
    ):
        assert (kind, json.dumps(source, sort_keys=True), "UNMAPPED") in excluded
    for r in kinds(plan, "assignment") + kinds(plan, "group"):
        assert "unmapped" not in r["name"] and "finance/audit" not in r["name"]


def test_nested_members_disabled_and_unmapped_users(plan):
    members = {
        (r["payload"]["group"], r["payload"]["user"]) for r in kinds(plan, "membership")
    }
    assert members == {
        ("keycloak/platform", "user-a"),
        ("keycloak/platform", "user-b"),
        ("keycloak-realm-role/auditor", "user-d"),
    }
    assert {r["name"] for r in kinds(plan, "user")} == {"user-a", "user-b", "user-d"}
    reasons = {
        e["username"]: e["reason"] for e in plan["exclusions"] if e["kind"] == "user"
    }
    assert reasons == {
        "user-c": "DISABLED_USER",
        "service-account-ci": "SERVICE_ACCOUNT_NOT_WORKFORCE_USER",
    }
    assert {
        "kind": "user_role",
        "source": {"kind": "realm_role", "role": "operator"},
        "username": "user-a",
        "reason": "USER_DIRECT_ROLE_MAPPING_NOT_MIGRATED",
    } in plan["exclusions"]
    features = {e["name"] for e in plan["exclusions"] if e["kind"] == "feature"}
    assert {
        "credentials_passwords_and_mfa",
        "client_scopes_and_protocol_mappers",
        "fine_grained_authorization",
    } <= features
    assert all(r["onboarding"].startswith("identity_center") for r in plan["user_rows"])
    user = next(r for r in kinds(plan, "user") if r["name"] == "user-a")
    assert user["payload"] == {
        "UserName": "user-a",
        "DisplayName": "First-user-a Last-user-a",
        "Name": {"GivenName": "First-user-a", "FamilyName": "Last-user-a"},
        "Emails": [{"Value": "user-a@example.com", "Type": "work", "Primary": True}],
    }


def test_include_disabled_and_all_enabled_options(config, inventory, mapping, center):
    mapping["options"] = {"include_disabled_users": True, "user_scope": "all_enabled"}
    p = create_plan(config, inventory, mapping, center)
    assert {r["name"] for r in kinds(p, "user")} == {
        "user-a",
        "user-b",
        "user-c",
        "user-d",
    }
    assert ("keycloak/platform", "user-c") in {
        (r["payload"]["group"], r["payload"]["user"]) for r in kinds(p, "membership")
    }


def test_mapping_review_scope_and_permission_set_blockers(
    config, inventory, mapping, center
):
    binding(
        mapping,
        "client_role",
        [
            {"account_id": OTHER_ACCOUNT, "permission_set": "ReadOnly"},
            {"account_id": ACCOUNT, "permission_set": "Missing"},
        ],
        review_ref="",
        client="orders",
        role="reader",
    )
    mapping["bindings"].append(
        {
            "source": {"kind": "group", "path": "/does-not-exist"},
            "target_group": "x",
            "review_ref": "",
            "assignments": [],
        }
    )
    p = create_plan(config, inventory, mapping, center)
    codes = {b["code"] for b in p["blockers"]}
    assert codes == {
        "MAPPING_REVIEW_REQUIRED",
        "ACCOUNT_OUT_OF_SCOPE",
        "PERMISSION_SET_NOT_FOUND",
        "UNKNOWN_MAPPING_SOURCE",
    }
    assert p["status"] == "blocked"
    with pytest.raises(IdentityError) as error:
        preflight(config, p, center)
    assert error.value.code == "PLAN_BLOCKED"


def test_conflicts_with_existing_identity_store_objects(
    config, inventory, mapping, center, aws
):
    store, _ = aws
    store.users["x-1"] = {
        "UserName": "USER-A",
        "Emails": [{"Value": "other@example.com", "Primary": True}],
    }
    store.users["x-2"] = {
        "UserName": "someone-else",
        "Emails": [{"Value": "user-b@example.com", "Primary": True}],
    }
    store.groups["x-g"] = {"DisplayName": "keycloak/platform", "Description": "manual"}
    p = create_plan(config, inventory, mapping, center)
    assert p["status"] == "blocked"
    assert {(c["resource_id"], c["code"]) for c in p["conflicts"]} == {
        ("user:user-a", "EXISTING_USER_EMAIL_MISMATCH"),
        ("user:user-b", "EMAIL_USED_BY_ANOTHER_USER"),
        ("group:keycloak/platform", "EXISTING_GROUP_NOT_OWNED"),
    }


def test_apply_creates_polls_and_is_idempotent(config, plan, center, aws, tmp_path):
    store, sso = aws
    sso.polls_before_success = 2
    first = run_apply(config, plan, center, tmp_path)
    assert first["status"] == "complete", first["results"]
    assert store.count("create_user") == 3 and store.count("create_group") == 2
    assert store.count("create_group_membership") == 3
    assert sso.count("create_account_assignment") == 2
    assert sso.count("describe_account_assignment_creation_status") == 6
    assert len(center.sleeps) == 6
    assert len(sso.assignments) == 2
    created = {c[1]["UserName"] for c in store.calls if c[0] == "create_user"}
    assert created == {"user-a", "user-b", "user-d"}
    group_calls = [c[1] for c in store.calls if c[0] == "create_group"]
    assert all(c["Description"] == "identity-export:test-export" for c in group_calls)
    before = list(store.calls), list(sso.calls)
    second = run_apply(config, plan, center, tmp_path)
    assert second["status"] == "complete"
    for name in ("create_user", "create_group", "create_group_membership"):
        assert store.count(name) == sum(1 for n, _ in before[0] if n == name)
    assert sso.count("create_account_assignment") == 2
    assert all(op["state"] == "applied" for op in status(tmp_path)["operations"])
    assert (tmp_path / "identity.sqlite3").stat().st_mode & 0o777 == 0o600


def test_apply_finds_existing_matching_objects_before_create(
    config, inventory, mapping, center, aws, tmp_path
):
    store, _ = aws
    store.users["x-1"] = {
        "UserName": "user-a",
        "Emails": [{"Value": "USER-A@example.com", "Primary": True}],
    }
    p = create_plan(config, inventory, mapping, center)
    assert (
        next(r for r in p["resources"] if r["id"] == "user:user-a")["planned_action"]
        == "exists"
    )
    result = run_apply(config, p, center, tmp_path)
    assert result["status"] == "complete"
    assert {"id": "user:user-a", "state": "unchanged", "created": False} in result[
        "results"
    ]
    assert "user-a" not in {
        c[1]["UserName"] for c in store.calls if c[0] == "create_user"
    }
    assert any(uid == "x-1" for _, uid in store.memberships)


def test_lost_response_resumes_without_duplicate_create(
    config, plan, center, aws, tmp_path
):
    store, _ = aws
    store.lose_response.add("user-a")
    prepared = preflight(config, plan, center)
    first = apply(config, plan, prepared, tmp_path, center)
    assert first["status"] == "partial"
    assert first["results"][-1] == {
        "id": "user:user-a",
        "state": "unknown",
        "reason": "AWS_THROTTLING_EXCEPTION",
    }
    assert SECRET not in json.dumps(first)
    second = apply(config, plan, prepared, tmp_path, center)
    assert second["status"] == "complete", second["results"]
    created = [c[1]["UserName"] for c in store.calls if c[0] == "create_user"]
    assert created.count("user-a") == 1
    row = next(op for op in status(tmp_path)["operations"] if op["id"] == "user:user-a")
    assert row["state"] == "applied" and row["created"] == 0
    assert row["reason"] == "RECONCILED"


def test_assignment_pending_then_resume_reconciles(config, plan, center, aws, tmp_path):
    _, sso = aws
    sso.polls_before_success = 10
    prepared = preflight(config, plan, center)
    first = apply(config, plan, prepared, tmp_path, center)
    assert first["status"] == "partial"
    assert first["results"][-1]["reason"] == "ACCOUNT_ASSIGNMENT_PENDING"
    assert first["results"][-1]["state"] == "unknown"
    for request in sso.requests.values():
        request["polls"] = 100
        sso.assignments.add(request["key"])
    sso.polls_before_success = 0
    second = apply(config, plan, prepared, tmp_path, center)
    assert second["status"] == "complete", second["results"]
    assert sso.count("create_account_assignment") == 2


def test_assignment_failure_is_reported(config, plan, center, aws, tmp_path):
    _, sso = aws
    sso.final_status = "FAILED"
    result = run_apply(config, plan, center, tmp_path)
    assert result["status"] == "partial"
    assert result["results"][-1]["reason"] == "ACCOUNT_ASSIGNMENT_FAILED"
    assert SECRET not in json.dumps(result)
    assert sso.assignments == set()


def test_verify_compares_identity_store_and_assignments(
    config, plan, center, aws, tmp_path
):
    store, sso = aws
    before = verify(config, plan, center)
    assert before["status"] == "failed" and before["configuration_status"] == "failed"
    run_apply(config, plan, center, tmp_path)
    after = verify(config, plan, center)
    assert after["configuration_status"] == "complete"
    assert after["status"] == "partial"
    assert set(after["pending_onboarding"]) == {"user-a", "user-b", "user-d"}
    sso.assignments.pop()
    store.memberships.pop()
    drift = verify(config, plan, center)
    failed = [x["id"] for x in drift["configuration"] if x["status"] == "fail"]
    assert drift["status"] == "failed" and len(failed) == 2
    assert {f.split(":", 1)[0] for f in failed} == {"assignment", "membership"}


def test_report_states_onboarding_and_exclusions(plan):
    text = markdown(plan)
    assert "IAM Identity Center 등록 절차" in text
    assert "DISABLED_USER" in text and "UNMAPPED" in text
    assert "fine_grained_authorization" in text
    assert SECRET not in text


def test_cli_flow_single_json_and_exit_codes(
    config, keycloak, aws, tmp_path, capsys, monkeypatch
):
    sleeps = []
    monkeypatch.setattr(collect_module, "KeycloakHttp", lambda *a, **k: keycloak)
    monkeypatch.setattr(
        cli,
        "IdentityCenter",
        lambda c: IdentityCenter(
            c, lambda **kw: FakeSession(*aws), sleep=sleeps.append, poll_interval=0
        ),
    )
    files = {
        n: tmp_path / (n + ".json")
        for n in (
            "config",
            "inventory",
            "mapping",
            "plan",
            "preflight",
            "apply",
            "verify",
            "export",
        )
    }
    private_write(files["config"], config)

    def call(*argv):
        code = cli.main([str(a) for a in argv])
        out = capsys.readouterr().out
        assert len(out.strip().splitlines()) == 1
        assert SECRET not in out and TOKEN not in out
        return code, json.loads(out)

    code, out = call(
        "collect", "--config", files["config"], "--out", files["inventory"]
    )
    assert code == 0 and out["status"] == "complete" and out["artifact_written"]
    code, out = call(
        "mapping", "--inventory", files["inventory"], "--out", files["mapping"]
    )
    assert code == 3 and out["status"] == "review_required"
    m = load(files["mapping"])
    binding(
        m,
        "group",
        [{"account_id": ACCOUNT, "permission_set": "ReadOnly"}],
        path="/platform",
    )
    private_write(files["mapping"], m)
    code, out = call(
        "plan",
        "--config",
        files["config"],
        "--inventory",
        files["inventory"],
        "--mapping",
        files["mapping"],
        "--out",
        files["plan"],
    )
    assert code == 0 and out["status"] == "planned"
    plan_hash = out["plan_hash"]
    code, out = call(
        "preflight",
        "--config",
        files["config"],
        "--plan",
        files["plan"],
        "--out",
        files["preflight"],
    )
    assert code == 0
    base = (
        "apply",
        "--config",
        files["config"],
        "--plan",
        files["plan"],
        "--preflight",
        files["preflight"],
        "--state-dir",
        tmp_path / "state",
    )
    code, out = call(*base, "--confirm-plan", "0" * 64)
    assert code == 2 and out["reason_codes"] == ["CONFIRMED_PLAN_MISMATCH"]
    code, out = call(*base, "--confirm-plan", plan_hash, "--out", files["apply"])
    assert code == 0 and out["migration_status"].startswith("pending")
    code, out = call(
        "verify",
        "--config",
        files["config"],
        "--plan",
        files["plan"],
        "--out",
        files["verify"],
    )
    assert code == 3 and out["status"] == "partial"
    code, out = call("status", "--state-dir", tmp_path / "state")
    assert code == 0 and out["operations"]
    code, out = call(
        "export",
        "--config",
        files["config"],
        "--plan",
        files["plan"],
        "--out",
        files["export"],
    )
    assert code == 3
    report = tmp_path / "report.md"
    code, out = call(
        "report",
        "--config",
        files["config"],
        "--plan",
        files["plan"],
        "--state-dir",
        tmp_path / "state",
        "--verification",
        files["verify"],
        "--out",
        report,
    )
    assert code == 0
    for path in list(files.values()) + [report]:
        assert path.stat().st_mode & 0o777 == 0o600
        assert SECRET not in path.read_text() and TOKEN not in path.read_text()
    tampered = load(files["plan"])
    tampered["resources"].pop()
    private_write(files["plan"], tampered)
    code, out = call(
        "preflight",
        "--config",
        files["config"],
        "--plan",
        files["plan"],
        "--out",
        files["preflight"],
    )
    assert code == 2 and out["reason_codes"] == ["PLAN_HASH_MISMATCH"]


def test_examples_are_placeholder_templates(secret_path):
    config = json.loads((EXAMPLES / "identity-export-config.example.json").read_text())
    mapping = json.loads(
        (EXAMPLES / "identity-export-mapping.example.json").read_text()
    )
    raw = json.dumps([config, mapping], ensure_ascii=False)
    assert "<계정 ID>" in raw and "<Keycloak 주소>" in raw
    replacements = {
        "https://<Keycloak 주소>": "https://keycloak.test",
        "<realm>": "identity-test",
        "<이관 ID>": "test-export",
        "<서비스 계정 client ID>": "identity-export-reader",
        "<비밀 파일 경로>": str(secret_path),
        "<리전>": "ap-northeast-2",
        "<Identity Center 인스턴스 ARN>": INSTANCE,
        "<Identity Store ID>": STORE,
        "<계정 ID>": ACCOUNT,
    }
    text = json.dumps(config, ensure_ascii=False)
    for old, new in replacements.items():
        text = text.replace(old, new)
    validate_config(json.loads(text))
    assert mapping["kind"] == "identity_export_mapping"
    assert {b["source"]["kind"] for b in mapping["bindings"]} == {
        "group",
        "realm_role",
        "client_role",
    }


def test_inventory_hash_detects_tampering(inventory):
    inventory["users"][0]["enabled"] = False
    with pytest.raises(IdentityError) as error:
        validate_inventory(inventory)
    assert error.value.code == "INVENTORY_HASH_MISMATCH"
    assert digest(inventory) != inventory["inventory_hash"]
