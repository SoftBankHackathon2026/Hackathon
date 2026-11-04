"""AWS SDK-shape fakes: no credentials, network, or actual AWS writes."""
from copy import deepcopy
import json

import pytest

from migration.identity.adapters.aws import AwsAdapter
from migration.identity.adapters.base import observation, observation_key
from migration.identity.models import (BlockedError, ConfigError, ConflictError, Endpoint,
                                       Operation, UnknownOutcome, UnsupportedError)


ACCOUNT = "123456789012"
ARN = f"arn:aws:iam::{ACCOUNT}:role/Reader"
TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
POLICY = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::source-example/*"}]}


class AwsError(Exception):
    def __init__(self, code):
        super().__init__("raw provider error password=must-never-escape")
        self.response = {"Error": {"Code": code}}


class Sts:
    account = ACCOUNT

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": f"arn:aws:sts::{self.account}:assumed-role/Operator/session"}


class Iam:
    def __init__(self, role=True):
        self.role = {"RoleId": "AROAEXAMPLEIMMUTABLE", "Arn": ARN, "RoleName": "Reader", "AssumeRolePolicyDocument": deepcopy(TRUST)} if role else None
        self.inline = None
        self.profile = None
        self.writes = []
        self.calls = []
        self.pages = [{"RoleDetailList": [{**deepcopy(self.role), "RolePolicyList": [{"PolicyName": "Read", "PolicyDocument": deepcopy(POLICY)}], "AttachedManagedPolicies": []}], "Policies": [], "IsTruncated": False}] if role else []
        self.lose_write = False

    def get_role(self, **kw):
        assert kw == {"RoleName": "Reader"}
        if self.role is None:
            raise AwsError("NoSuchEntity")
        return {"Role": deepcopy(self.role)}

    def get_account_authorization_details(self, **kw):
        self.calls.append(kw)
        return deepcopy(self.pages[len(self.calls) - 1])

    def get_role_policy(self, **kw):
        if self.inline is None:
            raise AwsError("NoSuchEntity")
        return {"PolicyDocument": deepcopy(self.inline)}

    def create_role(self, **kw):
        self.writes.append(("create_role", kw))
        self.role = {"RoleId": "AROAEXAMPLEIMMUTABLE", "Arn": ARN, "AssumeRolePolicyDocument": json.loads(kw["AssumeRolePolicyDocument"])}
        if self.lose_write:
            raise TimeoutError("secret token")
        return {"Role": deepcopy(self.role)}

    def put_role_policy(self, **kw):
        self.writes.append(("put_role_policy", kw))
        self.inline = json.loads(kw["PolicyDocument"])
        if self.lose_write:
            raise TimeoutError("secret token")

    def get_instance_profile(self, **kw):
        if self.profile is None:
            raise AwsError("NoSuchEntity")
        return {"InstanceProfile": deepcopy(self.profile)}

    def create_instance_profile(self, **kw):
        self.writes.append(("create_instance_profile", kw))
        self.profile = {"Arn": f"arn:aws:iam::{ACCOUNT}:instance-profile/Reader", "InstanceProfileId": "AIPAEXAMPLE", "Roles": []}

    def add_role_to_instance_profile(self, **kw):
        self.writes.append(("add_role_to_instance_profile", kw))
        self.profile["Roles"] = [deepcopy(self.role)]


class S3:
    def get_bucket_policy(self, **kw):
        assert kw == {"Bucket": "source-example", "ExpectedBucketOwner": ACCOUNT}
        raise AwsError("NoSuchBucketPolicy")

    def get_bucket_acl(self, **kw):
        return {"Owner": {"ID": "owner"}, "Grants": [{"Grantee": {"Type": "CanonicalUser", "ID": "owner"}, "Permission": "FULL_CONTROL"}]}

    def get_bucket_ownership_controls(self, **kw):
        return {"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}}

    def get_bucket_encryption(self, **kw):
        return {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}

    def get_public_access_block(self, **kw):
        return {"PublicAccessBlockConfiguration": dict.fromkeys(("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"), True)}


def endpoint(**options):
    return Endpoint("aws", ACCOUNT, ARN, "source-example", options)


def adapter(role=True, **options):
    return AwsAdapter(endpoint(**options), iam=Iam(role), sts=Sts(), s3=S3())


def op(kind="aws.role", desired=None, resource=ARN):
    if desired is None:
        desired = {"trust_policy": deepcopy(TRUST)}
    return Operation("test", "aws", kind, ACCOUNT, resource, desired, None)


def inline():
    return op("aws.inline_policy", {"policy_name": "MigrationObjectRead", "policy": deepcopy(POLICY)})


def test_scoped_collection_carries_identity_policy_and_unknown_restrictions():
    a = adapter()
    value = a.collect()
    assert value["identities"][0]["stable_id"] == "AROAEXAMPLEIMMUTABLE"
    assert value["policies"] == [POLICY]
    assert value["trust"] == [TRUST]
    assert value["coverage"]["identity_policies"] == "complete"
    assert value["coverage"]["resource_constraints"] == "unknown"
    assert value["coverage"]["boundary"] == "not_applicable"
    assert value["coverage"]["organization"] == value["coverage"]["session"] == "unknown"
    assert value["observations"][observation_key("aws.role", ARN)]["body"]["stable_id"]
    assert "must-never-escape" not in json.dumps(value)


def test_collect_all_pages_even_when_role_is_on_later_page():
    a = adapter()
    page = a.iam.pages[0]
    a.iam.pages = [{"RoleDetailList": [], "Policies": [], "IsTruncated": True, "Marker": "next"}, page]
    value = a.collect()
    assert len(a.iam.calls) == 2
    assert a.iam.calls[1]["Marker"] == "next"
    assert value["coverage"]["identity_policies"] == "complete"


@pytest.mark.parametrize("pages", [
    [{"RoleDetailList": [], "IsTruncated": True}],
    [{"RoleDetailList": [], "IsTruncated": True, "Marker": "same"}, {"RoleDetailList": [], "IsTruncated": True, "Marker": "same"}],
    [{"RoleDetailList": []}],
])
def test_bad_pagination_is_not_empty_success(pages):
    a = adapter()
    a.iam.pages = pages
    value = a.collect()
    assert value["coverage"]["identity_policies"] == "failed"


def test_permission_denial_preserves_missing_coverage_without_raw_error():
    a = adapter()
    def denied(**kw):
        raise AwsError("AccessDenied")
    a.iam.get_account_authorization_details = denied
    value = a.collect()
    assert value["coverage"]["identity_policies"] != "complete"
    assert value["policies"] == []
    assert "must-never-escape" not in json.dumps(value)


def test_wrong_authenticated_account_is_blocked():
    a = adapter()
    a.sts.account = "111111111111"
    with pytest.raises(BlockedError, match="account"):
        a.collect()
    assert a.iam.writes == []


def test_wrong_role_immutable_id_is_blocked():
    a = adapter(stable_id="AROAOTHER")
    with pytest.raises(BlockedError, match="immutable"):
        a.observe(op())


@pytest.mark.parametrize("value", [Endpoint("aws", "111111111111", ARN, "source-example"), Endpoint("aws", ACCOUNT, ARN, "*")])
def test_invalid_endpoint_scope(value):
    with pytest.raises(ConfigError):
        AwsAdapter(value)


def test_operation_cannot_escape_declared_role_or_account():
    a = adapter()
    with pytest.raises(BlockedError):
        a.observe(op(resource=f"arn:aws:iam::{ACCOUNT}:role/Other"))
    with pytest.raises(BlockedError):
        a.observe(Operation("escape", "aws", "aws.role", "111111111111", ARN, {"trust_policy": TRUST}, None))


def test_create_role_readback_and_duplicate_is_noop():
    a = adapter(False)
    operation = op()
    after = a.apply(operation, observation())
    assert a.satisfied(operation, after)
    assert a.apply(operation, after) == after
    assert len(a.iam.writes) == 1


def test_role_path_is_preserved():
    ep = Endpoint("aws", ACCOUNT, f"arn:aws:iam::{ACCOUNT}:role/app/Reader", "source-example")
    a = AwsAdapter(ep, iam=Iam(False), sts=Sts())
    operation = op(resource=ep.identity)
    # Fake readback ARN is intentionally unrelated; the write itself retains path.
    with pytest.raises(UnknownOutcome):
        a.apply(operation, observation())
    assert a.iam.writes[0][1]["Path"] == "/app/"


def test_existing_trust_may_not_be_replaced():
    a = adapter()
    changed_trust = deepcopy(TRUST)
    changed_trust["Statement"][0]["Sid"] = "Changed"
    operation = op(desired={"trust_policy": changed_trust})
    with pytest.raises(ConflictError, match="replacement"):
        a.apply(operation, a.observe(operation))
    assert a.iam.writes == []


def test_changed_role_id_conflicts_with_reviewed_observation():
    a = adapter()
    before = a.observe(op())
    a.iam.role["RoleId"] = "AROAREPLACEMENT"
    with pytest.raises(ConflictError, match="changed"):
        a.apply(op(), before)


def test_inline_policy_is_additive_and_preserves_existing_other_policies():
    a = adapter()
    operation = inline()
    after = a.apply(operation, observation())
    assert a.satisfied(operation, after)
    assert a.apply(operation, after) == after
    assert a.iam.writes[0][0] == "put_role_policy"
    assert len(a.iam.writes) == 1


def test_inline_different_existing_content_is_blocked():
    a = adapter()
    a.iam.inline = {"Statement": []}
    operation = inline()
    with pytest.raises(ConflictError):
        a.apply(operation, a.observe(operation))
    assert a.iam.writes == []


def test_inline_policy_name_must_be_allowlisted():
    a = adapter()
    with pytest.raises(BlockedError, match="policy name"):
        a.observe(op("aws.inline_policy", {"policy_name": "Other", "policy": POLICY}))


def test_inline_requires_existing_role():
    a = adapter(False)
    with pytest.raises(BlockedError, match="must exist"):
        a.apply(inline(), observation())


def test_lost_response_stays_unknown_and_can_be_reconciled_by_observation():
    a = adapter()
    a.iam.lose_write = True
    operation = inline()
    with pytest.raises(UnknownOutcome) as error:
        a.apply(operation, observation())
    assert "secret" not in str(error.value)
    after = a.observe(operation)
    assert a.satisfied(operation, after)
    a.apply(operation, after)
    assert len(a.iam.writes) == 1


def test_instance_profile_create_add_and_duplicate():
    a = adapter()
    operation = op("aws.instance_profile", {"role_name": "Reader"}, f"arn:aws:iam::{ACCOUNT}:instance-profile/Reader")
    after = a.apply(operation, observation())
    assert a.satisfied(operation, after)
    a.apply(operation, after)
    assert len(a.iam.writes) == 2


def test_existing_empty_profile_is_blocked_without_adoption():
    a = adapter()
    operation = op("aws.instance_profile", {"role_name": "Reader"}, f"arn:aws:iam::{ACCOUNT}:instance-profile/Reader")
    a.iam.create_instance_profile(InstanceProfileName="Reader")
    a.iam.writes.clear()
    with pytest.raises(ConflictError):
        a.apply(operation, a.observe(operation))
    assert a.iam.writes == []


def test_bucket_policy_deny_is_not_discarded():
    a = adapter()
    deny = {"Statement": [{"Effect": "Deny", "Action": "s3:*", "Resource": "*", "Principal": "*"}]}
    a.s3.get_bucket_policy = lambda **kw: {"Policy": json.dumps(deny)}
    value = a.collect()
    assert deny in value["policies"]


def test_kms_or_acl_expansion_marks_resource_constraints_unsupported():
    a = adapter()
    a.s3.get_bucket_encryption = lambda **kw: {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]}}
    assert a.collect()["coverage"]["resource_constraints"] == "unsupported"


def test_managed_default_policy_is_collected_from_later_page():
    a = adapter()
    detail = a.iam.pages[0]["RoleDetailList"][0]
    detail["AttachedManagedPolicies"] = [{"PolicyArn": "arn:aws:iam::aws:policy/Example"}]
    a.iam.pages[0]["Policies"] = [{"Arn": "arn:aws:iam::aws:policy/Example", "PolicyVersionList": [{"IsDefaultVersion": True, "Document": {"Statement": []}}]}]
    value = a.collect()
    assert {"Statement": []} in value["policies"]
    assert value["coverage"]["identity_policies"] == "complete"


def test_boundary_document_preserved_and_blocks_conversion():
    a = adapter()
    boundary_arn = f"arn:aws:iam::{ACCOUNT}:policy/Boundary"
    a.iam.role["PermissionsBoundary"] = {"PermissionsBoundaryArn": boundary_arn}
    a.iam.pages[0]["Policies"] = [{"Arn": boundary_arn, "PolicyVersionList": [{"IsDefaultVersion": True, "Document": {"Statement": []}}]}]
    value = a.collect()
    assert value["coverage"]["boundary"] == "unsupported"
    assert {"Statement": []} in value["policies"]


@pytest.mark.parametrize("statement", [
    {"Effect": "Allow", "Action": "s3:*", "Resource": "*"},
    {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::other-example/*"},
    {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::source-example/prefix/*"},
    {"Effect": "Allow", "NotAction": "s3:DeleteObject", "Resource": "*"},
    {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::source-example/*", "Condition": {}},
    {"Effect": "Deny", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::source-example/*"},
])
def test_inline_policy_expansion_and_restrictions_are_rejected(statement):
    a = adapter()
    operation = op("aws.inline_policy", {"policy_name": "MigrationObjectRead", "policy": {"Statement": [statement]}})
    with pytest.raises(UnsupportedError):
        a.observe(operation)
    assert a.iam.writes == []


def test_arbitrary_role_trust_is_rejected_before_write():
    a = adapter(False)
    operation = op(desired={"trust_policy": {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}]}})
    with pytest.raises(UnsupportedError):
        a.apply(operation, observation())
    assert a.iam.writes == []


def test_missing_predeclared_role_cannot_be_recreated_under_same_arn():
    a = adapter(False, stable_id="AROAEXAMPLEIMMUTABLE")
    with pytest.raises(BlockedError, match="no longer exists"):
        a.observe(op())


def test_continuously_new_markers_are_bounded(monkeypatch):
    monkeypatch.setattr("migration.identity.adapters.aws._MAX_PAGES", 2)
    a = adapter()
    a.iam.pages = [{"RoleDetailList": [], "IsTruncated": True, "Marker": "one"},
                   {"RoleDetailList": [], "IsTruncated": True, "Marker": "two"}]
    assert a.collect()["coverage"]["identity_policies"] == "failed"
    assert len(a.iam.calls) == 2


def test_instance_profile_path_is_explicitly_blocked():
    a = adapter(role_name="app/Reader")
    operation = op("aws.instance_profile", {"role_name": "Reader"}, f"arn:aws:iam::{ACCOUNT}:instance-profile/app/Reader")
    with pytest.raises(BlockedError):
        a.observe(operation)
    assert a.iam.writes == []


def test_environment_factory_disables_implicit_sdk_write_retries(monkeypatch):
    import sys
    from types import ModuleType
    from migration.identity.adapters.aws import from_environment
    calls = []
    class Session:
        def __init__(self, **kwargs):
            pass
        def client(self, name, config):
            calls.append((name, config))
            return object()
    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
    boto3 = ModuleType("boto3")
    boto3.Session = Session
    botocore_config = ModuleType("botocore.config")
    botocore_config.Config = Config
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    from_environment(endpoint())
    assert [name for name, config in calls] == ["iam", "sts", "s3"]
    assert all(config.kwargs == {"retries": {"total_max_attempts": 1, "mode": "standard"}} for name, config in calls)


def test_official_botocore_stubber_inline_request_contract():
    """Installed optional SDK validates actual service request/response models."""
    boto3 = pytest.importorskip("boto3", reason="identity-aws optional SDK not installed")
    from botocore.stub import Stubber
    from datetime import datetime, timezone
    from migration.identity.models import canonical
    clients = {name: boto3.client(name, region_name="us-east-1", aws_access_key_id="testing",
                                  aws_secret_access_key="testing") for name in ("iam", "sts")}
    iam_stub, sts_stub = Stubber(clients["iam"]), Stubber(clients["sts"])
    role = {"Path": "/", "RoleName": "Reader", "RoleId": "AROAEXAMPLEIMMUTABLE", "Arn": ARN,
            "CreateDate": datetime(2026, 1, 1, tzinfo=timezone.utc), "AssumeRolePolicyDocument": json.dumps(TRUST)}
    caller = {"Account": ACCOUNT, "UserId": "AROAEXAMPLEIMMUTABLE:session",
              "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/Operator/session"}
    for _ in range(2):
        sts_stub.add_response("get_caller_identity", caller, {})
    iam_stub.add_response("get_role", {"Role": deepcopy(role)}, {"RoleName": "Reader"})
    iam_stub.add_client_error("get_role_policy", "NoSuchEntity", expected_params={"RoleName": "Reader", "PolicyName": "MigrationObjectRead"})
    iam_stub.add_response("get_role", {"Role": deepcopy(role)}, {"RoleName": "Reader"})
    iam_stub.add_response("put_role_policy", {}, {"RoleName": "Reader", "PolicyName": "MigrationObjectRead", "PolicyDocument": canonical(POLICY)})
    iam_stub.add_response("get_role", {"Role": deepcopy(role)}, {"RoleName": "Reader"})
    iam_stub.add_response("get_role_policy", {"RoleName": "Reader", "PolicyName": "MigrationObjectRead", "PolicyDocument": json.dumps(POLICY)}, {"RoleName": "Reader", "PolicyName": "MigrationObjectRead"})
    with iam_stub, sts_stub:
        a = AwsAdapter(endpoint(), iam=clients["iam"], sts=clients["sts"])
        result = a.apply(inline(), observation())
        assert a.satisfied(inline(), result)
        iam_stub.assert_no_pending_responses()
        sts_stub.assert_no_pending_responses()


def test_policy_json_percent_escapes_are_not_decoded_twice():
    a = adapter()
    policy = {"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::source-example/a%2Fb"}]}
    a.iam.pages[0]["RoleDetailList"][0]["RolePolicyList"][0]["PolicyDocument"] = json.dumps(policy)
    assert a.collect()["policies"][0] == policy


def test_url_encoded_iam_policy_documents_are_decoded_once():
    from urllib.parse import quote
    a = adapter()
    a.iam.pages[0]["RoleDetailList"][0]["RolePolicyList"][0]["PolicyDocument"] = quote(json.dumps(POLICY))
    assert a.collect()["policies"][0] == POLICY


def test_official_botocore_stubber_paginated_role_collection():
    boto3 = pytest.importorskip("boto3", reason="identity-aws optional SDK not installed")
    from botocore.stub import Stubber
    from datetime import datetime, timezone
    clients = {name: boto3.client(name, region_name="us-east-1", aws_access_key_id="testing",
                                  aws_secret_access_key="testing") for name in ("iam", "sts")}
    iam_stub, sts_stub = Stubber(clients["iam"]), Stubber(clients["sts"])
    role = {"Path": "/", "RoleName": "Reader", "RoleId": "AROAEXAMPLEIMMUTABLE", "Arn": ARN,
            "CreateDate": datetime(2026, 1, 1, tzinfo=timezone.utc), "AssumeRolePolicyDocument": json.dumps(TRUST)}
    caller = {"Account": ACCOUNT, "UserId": "AROAEXAMPLEIMMUTABLE:session",
              "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/Operator/session"}
    for _ in range(2):
        sts_stub.add_response("get_caller_identity", deepcopy(caller), {})
    iam_stub.add_response("get_role", {"Role": deepcopy(role)}, {"RoleName": "Reader"})
    request = {"Filter": ["Role", "LocalManagedPolicy", "AWSManagedPolicy"], "MaxItems": 1000}
    iam_stub.add_response("get_account_authorization_details", {"IsTruncated": True, "Marker": "second", "RoleDetailList": []}, request)
    detail = {**deepcopy(role), "RolePolicyList": [{"PolicyName": "Read", "PolicyDocument": json.dumps(POLICY)}], "AttachedManagedPolicies": []}
    iam_stub.add_response("get_account_authorization_details", {"IsTruncated": False, "RoleDetailList": [detail]}, {**request, "Marker": "second"})
    iam_stub.add_response("get_role", {"Role": deepcopy(role)}, {"RoleName": "Reader"})
    iam_stub.add_client_error("get_role_policy", "NoSuchEntity", expected_params={"RoleName": "Reader", "PolicyName": "MigrationObjectRead"})
    with iam_stub, sts_stub:
        result = AwsAdapter(endpoint(), iam=clients["iam"], sts=clients["sts"]).collect()
        assert result["policies"] == [POLICY]
        assert result["coverage"]["identity_policies"] == "complete"
        assert result["coverage"]["resource_policy"] == "unknown"
        iam_stub.assert_no_pending_responses()
        sts_stub.assert_no_pending_responses()
