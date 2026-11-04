"""Scoped AWS IAM/S3 collection and additive IAM changes.

IAM has no compare-and-swap for these writes. The pre-write read detects stale
plans but cannot exclude another writer racing between that read and the write.
Existing trust/policy content is never replaced by this adapter.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from urllib.parse import unquote

from ..models import (BlockedError, ConfigError, ConflictError, Endpoint, IdentityError,
                      Operation, UnknownOutcome, UnsupportedError, canonical, reject_secrets)
from .base import check_operation, empty_inventory, observation, observation_key, same_observation

KINDS = {"aws.role", "aws.inline_policy", "aws.instance_profile"}
_ROLE = re.compile(r"arn:aws:iam::(?P<account>\d{12}):role/(?P<path>[\w+=,.@/-]+)\Z")
_PROFILE = re.compile(r"arn:aws:iam::(?P<account>\d{12}):instance-profile/(?P<path>[\w+=,.@/-]+)\Z")
_NAME = re.compile(r"[\w+=,.@-]{1,128}\Z")
_BUCKET = re.compile(r"(?=.{3,63}\Z)[a-z0-9][a-z0-9.-]*[a-z0-9]\Z")
_MISSING = {"NoSuchEntity", "NoSuchEntityException"}
_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
_MAX_PAGES = 10000


class _ReadDenied(BlockedError):
    """Classify sanitized access failures without retaining provider messages."""


def _code(error: Exception) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _document(value: object) -> dict:
    if isinstance(value, str):
        try:
            # SDK handlers usually already decode the URL-encoded IAM value.
            # Do not decode literal percent escapes inside an ordinary JSON
            # policy a second time (they can be significant resource names).
            try:
                value = json.loads(value)
            except ValueError:
                value = json.loads(unquote(value))
        except (ValueError, TypeError):
            raise ConfigError("invalid AWS policy document") from None
    if not isinstance(value, dict):
        raise ConfigError("invalid AWS policy document")
    reject_secrets(value)
    return deepcopy(value)


def _safe_read(call, **kwargs):
    try:
        return call(**kwargs)
    except IdentityError:
        raise
    except Exception as error:
        if _code(error) in _DENIED:
            raise _ReadDenied("AWS read permission denied") from None
        raise BlockedError("AWS read unavailable; state was not established") from None


class AwsAdapter:
    def __init__(self, endpoint: Endpoint, iam=None, sts=None, s3=None, organizations=None):
        if endpoint.provider != "aws" or not re.fullmatch(r"\d{12}", endpoint.scope):
            raise ConfigError("AWS scope must be an explicit account ID")
        role = _ROLE.fullmatch(endpoint.identity)
        if role is None or role["account"] != endpoint.scope:
            raise ConfigError("AWS role ARN is outside account scope")
        if not _BUCKET.fullmatch(endpoint.resource) or ".." in endpoint.resource:
            raise ConfigError("AWS resource must be an explicit S3 bucket name")
        self.endpoint = endpoint
        self.iam, self.sts, self.s3, self.organizations = iam, sts, s3, organizations
        self.role_path = role["path"]
        self.role_name = self.role_path.rsplit("/", 1)[-1]
        if not _NAME.fullmatch(self.role_name) or len(self.role_name) > 64:
            raise ConfigError("invalid AWS role name")
        self.policy_name = endpoint.options.get("policy_name", "MigrationObjectRead")
        if not isinstance(self.policy_name, str) or not _NAME.fullmatch(self.policy_name):
            raise ConfigError("invalid AWS policy name")

    def _account(self) -> None:
        if self.sts is None or self.iam is None:
            raise UnsupportedError("AWS IAM and STS clients are required")
        caller = _safe_read(self.sts.get_caller_identity)
        if not isinstance(caller, dict) or caller.get("Account") != self.endpoint.scope:
            raise BlockedError("authenticated AWS account differs from declared scope")

    def _role(self) -> dict | None:
        try:
            result = self.iam.get_role(RoleName=self.role_name)
        except Exception as error:
            if _code(error) in _MISSING:
                if self.endpoint.options.get("stable_id"):
                    raise BlockedError("declared immutable AWS role no longer exists") from None
                return None
            if _code(error) in _DENIED:
                raise _ReadDenied("AWS role read permission denied") from None
            raise BlockedError("AWS role state unavailable") from None
        role = result.get("Role") if isinstance(result, dict) else None
        if not isinstance(role, dict) or role.get("Arn") != self.endpoint.identity or not role.get("RoleId"):
            raise BlockedError("AWS role ARN or immutable identity is not established")
        stable = self.endpoint.options.get("stable_id")
        if stable is not None and stable != role["RoleId"]:
            raise BlockedError("AWS role immutable identity differs from declared identity")
        return role

    def _role_observation(self, role: dict | None) -> dict:
        if role is None:
            return observation()
        return observation({"arn": role["Arn"], "stable_id": role["RoleId"],
                            "trust_policy": _document(role.get("AssumeRolePolicyDocument")),
                            "boundary": deepcopy(role.get("PermissionsBoundary"))})

    def _check(self, op: Operation) -> None:
        check_operation(self.endpoint, op, KINDS)
        reject_secrets(op.desired)
        if op.kind in {"aws.role", "aws.inline_policy"}:
            if op.resource != self.endpoint.identity:
                raise BlockedError("AWS role operation is outside declared identity")
        else:
            profile = _PROFILE.fullmatch(op.resource)
            allowed = self.endpoint.options.get("role_name", self.role_name)
            if (profile is None or profile["account"] != self.endpoint.scope or profile["path"] != allowed
                    or not _NAME.fullmatch(profile["path"])):
                raise BlockedError("AWS profile operation is outside declared resource")
        if op.kind == "aws.role":
            if set(op.desired) != {"trust_policy"}:
                raise UnsupportedError("unsupported AWS role change")
            trust = _document(op.desired["trust_policy"])
            # This first AWS execution path supports an EC2 service role only.
            # Federation and arbitrary assume-role trust require separate rules.
            statements = trust.get("Statement")
            if isinstance(statements, dict):
                statements = [statements]
            if (set(trust) - {"Version", "Statement"} or not isinstance(statements, list)
                    or len(statements) != 1 or not isinstance(statements[0], dict)
                    or set(statements[0]) - {"Sid", "Effect", "Principal", "Action"}
                    or statements[0].get("Effect") != "Allow"
                    or statements[0].get("Principal") != {"Service": "ec2.amazonaws.com"}
                    or statements[0].get("Action") not in ("sts:AssumeRole", ["sts:AssumeRole"])):
                raise UnsupportedError("only explicit EC2 execution trust is supported")
            declared = self.endpoint.options.get("trust_policy")
            if declared is not None and canonical(declared) != canonical(trust):
                raise BlockedError("AWS trust differs from declared trust configuration")
        elif op.kind == "aws.inline_policy":
            if set(op.desired) != {"policy_name", "policy"} or op.desired["policy_name"] != self.policy_name:
                raise BlockedError("AWS inline policy is outside declared policy name")
            policy = _document(op.desired["policy"])
            statements = policy.get("Statement")
            if isinstance(statements, dict):
                statements = [statements]
            if set(policy) - {"Version", "Statement"} or not isinstance(statements, list) or not statements:
                raise UnsupportedError("unsupported AWS inline policy form")
            allowed = {("s3:ListBucket", f"arn:aws:s3:::{self.endpoint.resource}"),
                       ("s3:GetObject", f"arn:aws:s3:::{self.endpoint.resource}/*")}
            for statement in statements:
                if (not isinstance(statement, dict) or set(statement) - {"Sid", "Effect", "Action", "Resource"}
                        or statement.get("Effect") != "Allow"):
                    raise UnsupportedError("AWS policy restrictions require separate conversion rules")
                actions = statement.get("Action")
                resources = statement.get("Resource")
                actions = [actions] if isinstance(actions, str) else actions
                resources = [resources] if isinstance(resources, str) else resources
                if (not isinstance(actions, list) or not actions or not isinstance(resources, list) or not resources
                        or any(not isinstance(v, str) for v in actions + resources)
                        or any(action not in {"s3:ListBucket", "s3:GetObject"} for action in actions)
                        or any(resource not in {item[1] for item in allowed} for resource in resources)):
                    raise UnsupportedError("AWS inline policy exceeds specified bucket read scope")
                if any(not any((action, resource) in allowed for resource in resources) for action in actions):
                    raise UnsupportedError("AWS inline action lacks its matching resource scope")
        elif set(op.desired) != {"role_name"} or op.desired["role_name"] != self.role_name:
            raise BlockedError("AWS profile role is outside declared identity")

    def observe(self, operation: Operation) -> dict:
        self._check(operation)
        self._account()
        role = self._role()
        if operation.kind == "aws.role":
            return self._role_observation(role)
        if operation.kind == "aws.inline_policy":
            if role is None:
                return observation()
            try:
                result = self.iam.get_role_policy(RoleName=self.role_name, PolicyName=self.policy_name)
            except Exception as error:
                if _code(error) in _MISSING:
                    return observation()
                raise BlockedError("AWS inline policy state unavailable") from None
            return observation({"policy_name": self.policy_name, "policy": _document(result.get("PolicyDocument"))})
        name = operation.resource.split(":instance-profile/", 1)[1]
        try:
            profile = self.iam.get_instance_profile(InstanceProfileName=name).get("InstanceProfile")
        except Exception as error:
            if _code(error) in _MISSING:
                return observation()
            raise BlockedError("AWS instance profile state unavailable") from None
        if not isinstance(profile, dict) or profile.get("Arn") != operation.resource or not profile.get("InstanceProfileId"):
            raise BlockedError("AWS instance profile identity is not established")
        roles = profile.get("Roles")
        if not isinstance(roles, list):
            raise BlockedError("AWS instance profile role list unavailable")
        return observation({"arn": profile["Arn"], "stable_id": profile.get("InstanceProfileId"),
                            "roles": sorted(r["Arn"] for r in roles)})

    def satisfied(self, operation: Operation, observed: dict) -> bool:
        self._check(operation)
        if observed.get("exists") is not True or not isinstance(observed.get("body"), dict):
            return False
        body = observed["body"]
        if operation.kind == "aws.role":
            return (body.get("arn") == self.endpoint.identity and bool(body.get("stable_id"))
                    and canonical(body.get("trust_policy")) == canonical(operation.desired["trust_policy"]))
        if operation.kind == "aws.inline_policy":
            return canonical(body) == canonical(operation.desired)
        return body.get("arn") == operation.resource and body.get("roles") == [self.endpoint.identity]

    def apply(self, operation: Operation, before: dict) -> dict:
        """Only create or accept identical content; never replace existing policy."""
        self._check(operation)
        current = self.observe(operation)
        if not same_observation(current, before):
            raise ConflictError("AWS state changed since reviewed observation")
        if self.satisfied(operation, current):
            return current
        if current.get("exists") is not False:
            raise ConflictError("existing AWS configuration differs; replacement prohibited")
        try:
            if operation.kind == "aws.role":
                path = "/" + self.role_path.rsplit("/", 1)[0] + "/" if "/" in self.role_path else "/"
                self.iam.create_role(RoleName=self.role_name, Path=path,
                                     AssumeRolePolicyDocument=canonical(operation.desired["trust_policy"]))
            elif operation.kind == "aws.inline_policy":
                if self._role() is None:
                    raise BlockedError("AWS role must exist before inline policy creation")
                self.iam.put_role_policy(RoleName=self.role_name, PolicyName=self.policy_name,
                                         PolicyDocument=canonical(operation.desired["policy"]))
            else:
                if self._role() is None:
                    raise BlockedError("AWS role must exist before instance profile creation")
                name = operation.resource.split(":instance-profile/", 1)[1]
                self.iam.create_instance_profile(InstanceProfileName=name)
                self.iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=self.role_name)
        except IdentityError:
            raise
        except Exception as error:
            if _code(error) in {"EntityAlreadyExists", "ConcurrentModification"}:
                raise ConflictError("AWS concurrent creation detected") from None
            raise UnknownOutcome("AWS write response did not establish outcome; re-read required") from None
        try:
            result = self.observe(operation)
        except IdentityError:
            raise UnknownOutcome("AWS write read-back unavailable; outcome must be reconciled") from None
        if not self.satisfied(operation, result):
            raise UnknownOutcome("AWS write not yet confirmed by read-back")
        return result

    def _authorization(self) -> tuple[list[dict], list[dict]]:
        roles, policies, seen = [], [], set()
        marker = None
        page_count = 0
        while True:
            page_count += 1
            if page_count > _MAX_PAGES:
                raise BlockedError("AWS authorization pagination exceeded bounded collection limit")
            request = {"Filter": ["Role", "LocalManagedPolicy", "AWSManagedPolicy"], "MaxItems": 1000}
            if marker is not None:
                request["Marker"] = marker
            page = _safe_read(self.iam.get_account_authorization_details, **request)
            if not isinstance(page, dict) or not isinstance(page.get("RoleDetailList", []), list) or not isinstance(page.get("Policies", []), list):
                raise BlockedError("AWS authorization response malformed")
            roles.extend(page.get("RoleDetailList", []))
            policies.extend(page.get("Policies", []))
            if page.get("IsTruncated") is False:
                break
            if page.get("IsTruncated") is not True:
                raise BlockedError("AWS authorization pagination status unavailable")
            marker = page.get("Marker")
            if not isinstance(marker, str) or not marker or marker in seen:
                raise BlockedError("AWS authorization pagination incomplete")
            seen.add(marker)
        return roles, policies

    def _managed_document(self, arn: str, details: list[dict]) -> dict:
        match = [p for p in details if p.get("Arn") == arn]
        if len(match) == 1:
            versions = match[0].get("PolicyVersionList", [])
            default = [v for v in versions if v.get("IsDefaultVersion") is True]
            if len(default) == 1:
                return _document(default[0].get("Document"))
        # Managed policies omitted by IAM response filters are read explicitly.
        policy = _safe_read(self.iam.get_policy, PolicyArn=arn).get("Policy", {})
        version = policy.get("DefaultVersionId")
        if not isinstance(version, str) or not version:
            raise BlockedError("AWS managed policy default version unavailable")
        result = _safe_read(self.iam.get_policy_version, PolicyArn=arn, VersionId=version)
        return _document(result.get("PolicyVersion", {}).get("Document"))

    def collect(self) -> dict:
        self._account()
        result = empty_inventory(self.endpoint)
        coverage = result["coverage"]
        coverage.update({"identity": "unknown", "identity_policies": "unknown", "resource_policy": "unknown",
                         "boundary": "unknown", "organization": "unknown", "session": "unknown",
                         "resource_constraints": "unknown"})
        result["limitations"].extend([
            "Organization SCP/RCP and session policies are not established by IAM account authorization details.",
            "AWS IAM writes have no conditional version update; an external-writer race remains.",
        ])
        try:
            role = self._role()
            result["observations"][observation_key("aws.role", self.endpoint.identity)] = self._role_observation(role)
            coverage["identity"] = "complete"
            if role is None:
                coverage["boundary"] = "not_applicable"
                coverage["identity_policies"] = "not_applicable"
                result["observations"][observation_key("aws.inline_policy", self.endpoint.identity)] = observation()
            else:
                result["identities"].append({"provider": "aws", "scope": self.endpoint.scope,
                                             "name": role["Arn"], "stable_id": role["RoleId"], "kind": "role"})
                result["trust"].append(_document(role.get("AssumeRolePolicyDocument")))
                coverage["boundary"] = "unsupported" if role.get("PermissionsBoundary") else "not_applicable"
                roles, policy_details = self._authorization()
                matches = [r for r in roles if r.get("Arn") == self.endpoint.identity]
                if len(matches) != 1 or matches[0].get("RoleId") != role["RoleId"]:
                    raise BlockedError("AWS authorization snapshot does not match immutable role")
                detail = matches[0]
                if "RolePolicyList" not in detail or "AttachedManagedPolicies" not in detail:
                    raise BlockedError("AWS role policy enumeration incomplete")
                for policy in detail["RolePolicyList"]:
                    result["policies"].append(_document(policy.get("PolicyDocument")))
                for attachment in detail["AttachedManagedPolicies"]:
                    result["policies"].append(self._managed_document(attachment["PolicyArn"], policy_details))
                if role.get("PermissionsBoundary"):
                    arn = role["PermissionsBoundary"].get("PermissionsBoundaryArn")
                    if not isinstance(arn, str):
                        raise BlockedError("AWS permission boundary reference unavailable")
                    result["policies"].append(self._managed_document(arn, policy_details))
                coverage["identity_policies"] = "complete"
                policy_op = Operation("collect-inline", "aws", "aws.inline_policy", self.endpoint.scope,
                                      self.endpoint.identity, {"policy_name": self.policy_name, "policy": {
                                          "Statement": [{"Effect": "Allow", "Action": "s3:GetObject",
                                                         "Resource": f"arn:aws:s3:::{self.endpoint.resource}/*"}]}}, None)
                result["observations"][observation_key("aws.inline_policy", self.endpoint.identity)] = self.observe(policy_op)
        except Exception as error:
            status = "denied" if isinstance(error, _ReadDenied) else "failed"
            coverage["identity_policies"] = status
            if coverage["identity"] != "complete":
                coverage["identity"] = status
            result["evidence"].append("AWS identity collection failed; no absence inference is valid.")
        if self.s3 is None:
            result["evidence"].append("S3 client unavailable; resource collection not executed.")
            return result
        bucket_args = {"Bucket": self.endpoint.resource, "ExpectedBucketOwner": self.endpoint.scope}
        try:
            try:
                policy = self.s3.get_bucket_policy(**bucket_args)
            except Exception as error:
                if _code(error) != "NoSuchBucketPolicy":
                    raise
                policy = None
            if policy is not None:
                result["policies"].append(_document(policy.get("Policy")))
            coverage["resource_policy"] = "complete"
        except Exception as error:
            coverage["resource_policy"] = "denied" if _code(error) in _DENIED else "failed"
            result["evidence"].append("S3 bucket policy was not established.")
        try:
            acl = self.s3.get_bucket_acl(**bucket_args)
            ownership = self.s3.get_bucket_ownership_controls(**bucket_args)
            encryption = self.s3.get_bucket_encryption(**bucket_args)
            public = self.s3.get_public_access_block(**bucket_args)
            owner_id = acl.get("Owner", {}).get("ID")
            grants = acl.get("Grants")
            owner_only = bool(owner_id) and isinstance(grants, list) and all(
                g.get("Grantee", {}).get("Type") == "CanonicalUser" and g.get("Grantee", {}).get("ID") == owner_id
                for g in grants)
            rules = ownership.get("OwnershipControls", {}).get("Rules", [])
            uniform = rules == [{"ObjectOwnership": "BucketOwnerEnforced"}]
            algorithms = [r.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
                          for r in encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])]
            block = public.get("PublicAccessBlockConfiguration", {})
            fully_blocked = all(block.get(k) is True for k in
                                ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
            # Bucket defaults do not prove how existing individual objects were
            # encrypted; KMS or object-version requirements remain uncollected.
            coverage["resource_constraints"] = "unknown" if owner_only and uniform and algorithms == ["AES256"] and fully_blocked else "unsupported"
            result["evidence"].append("S3 bucket settings inspected; existing individual-object encryption, KMS and version requirements are not established.")
        except Exception as error:
            coverage["resource_constraints"] = "denied" if _code(error) in _DENIED else "failed"
            result["evidence"].append("S3 resource constraints were not established.")
        return result


def from_environment(endpoint: Endpoint) -> AwsAdapter:
    """Resolve optional SDK and standard AWS credential chain only on live use."""
    AwsAdapter(endpoint)  # Reject malformed scope before credential resolution.
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise UnsupportedError("install migration[identity-aws] for AWS live commands") from None
    try:
        session = boto3.Session(profile_name=endpoint.options.get("profile"), region_name=endpoint.options.get("region"))
        # Ambiguous writes are reconciled by the journal engine. An SDK retry
        # must not silently issue a second write after a lost first response.
        config = Config(retries={"total_max_attempts": 1, "mode": "standard"})
        return AwsAdapter(endpoint, iam=session.client("iam", config=config),
                          sts=session.client("sts", config=config), s3=session.client("s3", config=config))
    except IdentityError:
        raise
    except Exception:
        raise BlockedError("AWS SDK client initialization failed") from None
