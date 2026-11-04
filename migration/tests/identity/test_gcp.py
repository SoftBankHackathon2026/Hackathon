"""Credential-free tests using discovery-client-shaped API responses."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from migration.identity.adapters.base import observation, observation_key
from migration.identity.adapters.gcp import GcpAdapter, READ_PERMISSIONS
from migration.identity.models import (BlockedError, ConfigError, ConflictError, Endpoint,
                                       Operation, UnknownOutcome, UnsupportedError)

PROJECT = "migration-demo-example"
EMAIL = f"report-reader@{PROJECT}.iam.gserviceaccount.com"
BUCKET = "target-reports-example"
ROLE = f"projects/{PROJECT}/roles/MigrationObjectRead"


class ApiError(Exception):
    def __init__(self, status):
        super().__init__("Authorization: Bearer do-not-leak-this-secret")
        self.resp = SimpleNamespace(status=status)


class Request:
    def __init__(self, invoke):
        self.invoke = invoke

    def execute(self, num_retries=0):
        assert num_retries == 0
        return copy.deepcopy(self.invoke())


class Resource:
    def __init__(self, fake, path=""):
        self.fake, self.path = fake, path

    def __getattr__(self, name):
        path = f"{self.path}.{name}".strip(".")
        if name in {"projects", "organizations", "serviceAccounts", "roles", "buckets", "permissions"}:
            return lambda: Resource(self.fake, path)
        return lambda **kwargs: Request(lambda: self.fake.invoke(path, kwargs))


class Fake:
    def __init__(self):
        self.calls = []
        self.account = {"email": EMAIL, "projectId": PROJECT, "uniqueId": "100000000000000000001"}
        self.role = {"name": ROLE, "includedPermissions": sorted(READ_PERMISSIONS), "etag": "role-etag"}
        self.bucket = {"name": BUCKET, "projectNumber": "123456789012"}
        self.project = {"projectId": PROJECT, "projectNumber": "123456789012", "lifecycleState": "ACTIVE"}
        self.policy = {"etag": "reviewed-etag", "version": 3, "bindings": [
            {"role": "roles/storage.legacyBucketReader", "members": ["group:auditors@example.invalid"]},
            {"role": ROLE, "members": [f"serviceAccount:{EMAIL}"],
             "condition": {"title": "expires", "expression": "request.time < timestamp('2030-01-01T00:00:00Z')"}},
        ]}
        self.fail = {}
        self.lose_response = False
        self.permissions_pages = [{"permissions": [{"name": p} for p in sorted(READ_PERMISSIONS)]}]
        self.api = Resource(self)

    def invoke(self, path, kwargs):
        self.calls.append((path, copy.deepcopy(kwargs)))
        if path in self.fail:
            raise ApiError(self.fail[path])
        if path == "projects.get":
            assert kwargs == {"projectId": PROJECT}
            return self.project
        if path == "buckets.get":
            assert kwargs == {"bucket": BUCKET}
            return self.bucket
        if path == "projects.serviceAccounts.get":
            assert kwargs == {"name": f"projects/{PROJECT}/serviceAccounts/{EMAIL}"}
            if self.account is None:
                raise ApiError(404)
            return self.account
        if path == "projects.serviceAccounts.getIamPolicy":
            assert kwargs["options_requestedPolicyVersion"] == 3
            return {"bindings": [], "etag": "sa-etag"}
        if path == "projects.serviceAccounts.create":
            assert self.account is None
            assert kwargs["body"]["accountId"] == EMAIL.split("@")[0]
            self.account = {"email": EMAIL, "projectId": PROJECT, "uniqueId": "100000000000000000001"}
            return self.account
        if path == "projects.roles.get":
            if self.role is None:
                raise ApiError(404)
            return self.role
        if path == "projects.roles.create":
            assert kwargs["parent"] == f"projects/{PROJECT}"
            assert kwargs["body"]["roleId"] == "MigrationObjectRead"
            self.role = dict(kwargs["body"]["role"], name=ROLE, etag="role-etag")
            return self.role
        if path == "buckets.getIamPolicy":
            assert kwargs == {"bucket": BUCKET, "optionsRequestedPolicyVersion": 3}
            return self.policy
        if path == "buckets.setIamPolicy":
            if kwargs["body"]["etag"] != self.policy["etag"]:
                raise ApiError(412)
            self.policy = copy.deepcopy(kwargs["body"])
            self.policy["etag"] = "new-etag"
            if self.lose_response:
                raise TimeoutError("token=private-value")
            return self.policy
        if path == "projects.getIamPolicy":
            assert kwargs["body"] == {"options": {"requestedPolicyVersion": 3}}
            return {"bindings": [], "etag": "project-etag"}
        if path == "projects.getAncestry":
            return {"ancestor": [{"resourceId": {"type": "project", "id": "123456789012"}}]}
        if path == "roles.get":
            return {"name": kwargs["name"], "includedPermissions": ["storage.objects.list"]}
        if path == "permissions.queryTestablePermissions":
            return self.permissions_pages[int(kwargs["body"].get("pageToken", "0"))]
        raise AssertionError(f"unexpected fake API call {path}")


def endpoint(**options):
    return Endpoint("gcp", PROJECT, EMAIL, BUCKET, options)


def adapter(fake=None, **options):
    fake = fake or Fake()
    return GcpAdapter(endpoint(**options), iam=fake.api, storage=fake.api, crm=fake.api), fake


def operation(kind="gcp.bucket_binding", **kwargs):
    desired = {"gcp.bucket_binding": {"role": ROLE, "member": f"serviceAccount:{EMAIL}"},
               "gcp.custom_role": {"included_permissions": sorted(READ_PERMISSIONS)},
               "gcp.service_account": {"account_id": EMAIL.split("@")[0]}}[kind]
    resource = {"gcp.bucket_binding": BUCKET, "gcp.custom_role": ROLE, "gcp.service_account": EMAIL}[kind]
    return Operation("op-one", kwargs.get("provider", "gcp"), kind, kwargs.get("scope", PROJECT),
                     kwargs.get("resource", resource), kwargs.get("desired", desired), None)


def test_collect_retains_policies_trust_stable_id_and_explicit_gaps():
    obj, fake = adapter(stable_id="100000000000000000001", project_number="123456789012")
    fake.role = None
    result = obj.collect()
    assert result["identities"][0]["id"] == fake.account["uniqueId"]
    assert result["trust"][0]["kind"] == "gcp.service_account_policy"
    assert result["policies"][0]["policy"] == fake.policy
    assert any(p["kind"] == "gcp.role" for p in result["policies"])
    assert result["coverage"]["resource_policy"] == "complete"
    assert result["coverage"]["organization"] == "unknown"
    assert result["coverage"]["boundary"] == "unknown"
    assert result["coverage"]["resource_constraints"] == "unknown"
    assert result["observations"][observation_key("gcp.custom_role", ROLE)] == observation()


def test_bucket_merge_preserves_conditional_bindings_and_other_principals():
    obj, fake = adapter()
    op = operation()
    before = obj.observe(op)
    original = copy.deepcopy(fake.policy)
    assert not obj.satisfied(op, before)  # A conditional grant is not an unconditional grant.
    after = obj.apply(op, before)
    assert obj.satisfied(op, after)
    assert fake.policy["bindings"][:2] == original["bindings"]
    assert fake.policy["version"] == 3
    writes = [kw for path, kw in fake.calls if path == "buckets.setIamPolicy"]
    assert writes[0]["body"]["etag"] == "reviewed-etag"
    obj.apply(op, after)
    assert len([p for p, _ in fake.calls if p == "buckets.setIamPolicy"]) == 1


def test_merge_existing_unconditional_binding_only_adds_our_member():
    obj, fake = adapter()
    fake.policy["bindings"].append({"role": ROLE, "members": ["serviceAccount:other@example.invalid"]})
    op = operation()
    obj.apply(op, obj.observe(op))
    binding = fake.policy["bindings"][-1]
    assert set(binding["members"]) == {"serviceAccount:other@example.invalid", f"serviceAccount:{EMAIL}"}
    assert len(fake.policy["bindings"]) == 3


def test_etag_change_blocks_before_any_write():
    obj, fake = adapter()
    op = operation()
    before = obj.observe(op)
    fake.policy["etag"] = "concurrent"
    with pytest.raises(ConflictError):
        obj.apply(op, before)
    assert not any(p == "buckets.setIamPolicy" for p, _ in fake.calls)


def test_server_conditional_update_conflict_is_not_retried():
    obj, fake = adapter()
    op = operation()
    before = obj.observe(op)
    fake.fail["buckets.setIamPolicy"] = 412
    with pytest.raises(ConflictError):
        obj.apply(op, before)
    assert len([p for p, _ in fake.calls if p == "buckets.setIamPolicy"]) == 1


def test_response_loss_can_be_reconciled_by_observation_without_rewrite():
    obj, fake = adapter()
    op = operation()
    before = obj.observe(op)
    fake.lose_response = True
    with pytest.raises(UnknownOutcome) as error:
        obj.apply(op, before)
    assert "private-value" not in str(error.value)
    after = obj.observe(op)
    assert obj.satisfied(op, after)
    obj.apply(op, after)
    assert len([p for p, _ in fake.calls if p == "buckets.setIamPolicy"]) == 1


@pytest.mark.parametrize("kind", ["gcp.service_account", "gcp.custom_role"])
def test_creation_and_idempotent_reuse(kind):
    obj, fake = adapter()
    if kind == "gcp.service_account":
        fake.account = None
    else:
        fake.role = None
    op = operation(kind)
    before = obj.observe(op)
    assert not before["exists"]
    after = obj.apply(op, before)
    assert obj.satisfied(op, after)
    obj.apply(op, after)
    assert len([p for p, _ in fake.calls if p.endswith(".create")]) == 1


def test_different_existing_custom_role_never_overwritten():
    obj, fake = adapter()
    fake.role = {"name": ROLE, "includedPermissions": ["storage.objects.delete"], "etag": "role-etag"}
    op = operation("gcp.custom_role")
    with pytest.raises(ConflictError):
        obj.apply(op, obj.observe(op))
    assert not any(p.endswith(".create") for p, _ in fake.calls)


@pytest.mark.parametrize("status,expected", [(403, "denied"), (500, "failed")])
def test_collection_denied_or_failed_is_not_absent_success(status, expected):
    obj, fake = adapter()
    fake.fail["buckets.getIamPolicy"] = status
    result = obj.collect()
    assert result["coverage"]["resource_policy"] == expected
    assert observation_key("gcp.bucket_binding", BUCKET) not in result["observations"]
    assert "do-not-leak" not in str(result)


def test_only_404_counts_as_absence():
    obj, fake = adapter()
    fake.fail["projects.serviceAccounts.get"] = 403
    with pytest.raises(BlockedError):
        obj.observe(operation("gcp.service_account"))
    fake.fail["projects.serviceAccounts.get"] = 404
    assert not obj.observe(operation("gcp.service_account"))["exists"]


@pytest.mark.parametrize("change", ["project", "bucket", "unique"])
def test_immutable_identity_and_ownership_mismatch_blocks(change):
    obj, fake = adapter(stable_id="100000000000000000001")
    if change == "project":
        fake.project["projectId"] = "unapproved-project"
    elif change == "bucket":
        fake.bucket["projectNumber"] = "999999999999"
    else:
        fake.account["uniqueId"] = "100000000000000000002"
    with pytest.raises(BlockedError):
        obj.observe(operation("gcp.service_account"))


@pytest.mark.parametrize("kwargs", [{"scope": "other-project"}, {"resource": "other-bucket"}, {"provider": "aws"},
                                   {"desired": {"role": ROLE, "member": "allUsers"}},
                                   {"desired": {"role": "roles/storage.admin", "member": f"serviceAccount:{EMAIL}"}}])
def test_resource_or_principal_escape_rejected_before_network(kwargs):
    obj, fake = adapter()
    with pytest.raises(BlockedError):
        obj.observe(operation(**kwargs))
    assert not fake.calls


def test_custom_role_wider_permissions_rejected_before_network():
    obj, fake = adapter()
    with pytest.raises(UnsupportedError):
        obj.observe(operation("gcp.custom_role", desired={"included_permissions": ["storage.objects.delete"]}))
    assert not fake.calls


def test_permission_support_pagination_and_unsupported_status():
    obj, fake = adapter()
    fake.role = None
    fake.permissions_pages = [{"permissions": [{"name": "storage.objects.get"}], "nextPageToken": "1"},
                              {"permissions": [{"name": "storage.objects.list", "customRolesSupportLevel": "SUPPORTED"}]}]
    op = operation("gcp.custom_role")
    obj.apply(op, obj.observe(op))
    assert len([p for p, _ in fake.calls if p == "permissions.queryTestablePermissions"]) == 2
    obj, fake = adapter()
    fake.role = None
    fake.permissions_pages[0]["permissions"][0]["customRolesSupportLevel"] = "NOT_SUPPORTED"
    with pytest.raises(BlockedError):
        obj.apply(op, obj.observe(op))
    assert not any(p.endswith(".create") for p, _ in fake.calls)


def test_permission_pagination_repetition_blocked():
    obj, fake = adapter()
    fake.role = None
    fake.permissions_pages = [{"permissions": [], "nextPageToken": "0"}]
    op = operation("gcp.custom_role")
    with pytest.raises(BlockedError, match="pagination"):
        obj.apply(op, obj.observe(op))


def test_missing_etag_blocks_collection_and_mutation():
    obj, fake = adapter()
    fake.policy.pop("etag")
    with pytest.raises(BlockedError, match="etag"):
        obj.observe(operation())


def test_binding_rechecks_custom_role_does_not_gain_wider_permissions():
    obj, fake = adapter()
    op = operation()
    before = obj.observe(op)
    fake.role["includedPermissions"].append("storage.objects.delete")
    with pytest.raises(BlockedError, match="custom role"):
        obj.apply(op, before)
    assert not any(p == "buckets.setIamPolicy" for p, _ in fake.calls)


def test_invalid_endpoint_scope_and_cross_project_identity():
    with pytest.raises(ConfigError):
        GcpAdapter(Endpoint("gcp", "123456789012", EMAIL, BUCKET))
    with pytest.raises(ConfigError):
        GcpAdapter(Endpoint("gcp", PROJECT, "reader@other-project.iam.gserviceaccount.com", BUCKET))


def test_declared_immutable_identity_cannot_be_recreated_under_same_email():
    obj, fake = adapter(stable_id="100000000000000000001")
    fake.account = None
    with pytest.raises(BlockedError, match="no longer exists"):
        obj.apply(operation("gcp.service_account"), observation())
    assert not any(p.endswith(".create") for p, _ in fake.calls)
