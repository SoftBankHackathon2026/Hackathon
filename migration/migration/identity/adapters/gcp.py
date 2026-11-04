"""Google API adapter for narrowly scoped service-account object-read migrations.

No credential discovery occurs until ``from_environment`` is explicitly called.
An allow-policy snapshot is not a claim of complete effective permissions: deny,
principal boundaries, object ACLs and session restrictions remain explicit gaps.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from ..models import (BlockedError, ConfigError, ConflictError, Endpoint, IdentityError,
                      Operation, UnknownOutcome, UnsupportedError, reject_secrets)
from .base import check_operation, empty_inventory, observation, observation_key, same_observation

KINDS = {"gcp.service_account", "gcp.custom_role", "gcp.bucket_binding"}
READ_PERMISSIONS = {"storage.objects.get", "storage.objects.list"}


def _status(exc: Exception) -> int | None:
    """Read a numeric status only. Provider messages can contain credentials."""
    value = getattr(getattr(exc, "resp", None), "status", None)
    return value if type(value) is int else None


def _read(request: Any, *, absent_ok: bool = False) -> dict | None:
    try:
        value = request.execute(num_retries=0)
        if not isinstance(value, dict):
            raise ConfigError("malformed GCP response")
        reject_secrets(value)
        return value
    except IdentityError:
        raise
    except Exception as exc:
        status = _status(exc)
        if status == 404 and absent_ok:
            return None
        if status in (401, 403):
            raise BlockedError("GCP read denied; inventory is incomplete") from None
        if status == 404:
            raise BlockedError("GCP required resource not found") from None
        raise BlockedError("GCP read failed; inventory is incomplete") from None


def _write(request: Any) -> None:
    try:
        # A timeout after commit must not cause an automatic second write.
        request.execute(num_retries=0)
    except Exception as exc:
        if _status(exc) in (409, 412):
            raise ConflictError("GCP concurrent change or existing resource conflict") from None
        if _status(exc) in (400, 401, 403, 404):
            raise BlockedError("GCP mutation rejected") from None
        raise UnknownOutcome("GCP mutation response unavailable; re-observe before resume") from None


class GcpAdapter:
    def __init__(self, endpoint: Endpoint, iam: Any = None, storage: Any = None,
                 crm: Any = None, credentials: Any = None):
        if endpoint.provider != "gcp":
            raise ConfigError("GCP endpoint required")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", endpoint.scope):
            raise ConfigError("GCP scope must be a project ID")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@" + re.escape(endpoint.scope) +
                            r"\.iam\.gserviceaccount\.com", endpoint.identity):
            raise ConfigError("service account must belong to the scoped GCP project")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", endpoint.resource):
            raise ConfigError("GCP resource must be a bucket name")
        role_id = endpoint.options.get("role_id", "MigrationObjectRead")
        if not isinstance(role_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.]{3,64}", role_id):
            raise ConfigError("invalid scoped GCP role ID")
        self.endpoint = endpoint
        self.iam, self.storage, self.crm = iam, storage, crm
        self.credentials = credentials  # Never serialized or included in errors.
        self.role_name = f"projects/{endpoint.scope}/roles/{role_id}"
        self.service_account_name = f"projects/{endpoint.scope}/serviceAccounts/{endpoint.identity}"

    def _project(self) -> dict:
        project = _read(self.crm.projects().get(projectId=self.endpoint.scope))
        if (project.get("projectId") != self.endpoint.scope or not str(project.get("projectNumber", "")).isdigit()
                or project.get("lifecycleState", "ACTIVE") != "ACTIVE"):
            raise BlockedError("GCP project identity mismatch or inactive project")
        expected_number = self.endpoint.options.get("project_number")
        if expected_number is not None and str(expected_number) != str(project["projectNumber"]):
            raise BlockedError("GCP immutable project number mismatch")
        return project

    def _bucket(self, project: dict) -> dict:
        bucket = _read(self.storage.buckets().get(bucket=self.endpoint.resource))
        if (bucket.get("name") != self.endpoint.resource or
                str(bucket.get("projectNumber")) != str(project["projectNumber"])):
            raise BlockedError("GCP bucket does not belong to the scoped project")
        return bucket

    def _scope(self) -> None:
        self._bucket(self._project())

    def _check(self, operation: Operation) -> None:
        check_operation(self.endpoint, operation, KINDS)
        expected = {"gcp.service_account": self.endpoint.identity,
                    "gcp.custom_role": self.role_name,
                    "gcp.bucket_binding": self.endpoint.resource}[operation.kind]
        if operation.resource != expected:
            raise BlockedError("GCP operation resource is outside the declared endpoint")
        desired = operation.desired
        if operation.kind == "gcp.service_account":
            if set(desired) != {"account_id"} or desired["account_id"] != self.endpoint.identity.split("@")[0]:
                raise BlockedError("GCP service account creation identity mismatch")
        elif operation.kind == "gcp.custom_role":
            values = desired.get("included_permissions")
            if (set(desired) != {"included_permissions"} or not isinstance(values, list)
                    or not values or any(not isinstance(x, str) for x in values)
                    or len(values) != len(set(values)) or not set(values) <= READ_PERMISSIONS):
                raise UnsupportedError("only exact GCP object-read custom-role permissions are supported")
        elif (set(desired) != {"role", "member"} or desired["role"] != self.role_name
              or desired["member"] != f"serviceAccount:{self.endpoint.identity}"):
            raise BlockedError("GCP binding must use the scoped custom role and service account")

    def _account(self) -> dict | None:
        value = _read(self.iam.projects().serviceAccounts().get(name=self.service_account_name), absent_ok=True)
        if value is None and self.endpoint.options.get("stable_id") is not None:
            raise BlockedError("declared immutable GCP service account no longer exists")
        if value is not None:
            if (value.get("email") != self.endpoint.identity or value.get("projectId") != self.endpoint.scope
                    or not isinstance(value.get("uniqueId"), str) or not value["uniqueId"].isdigit()):
                raise BlockedError("GCP service account immutable identity is missing or mismatched")
            stable_id = self.endpoint.options.get("stable_id")
            if stable_id is not None and value["uniqueId"] != stable_id:
                raise BlockedError("GCP service account immutable identity mismatch")
        return value

    def _observe(self, operation: Operation) -> dict:
        if operation.kind == "gcp.service_account":
            body = self._account()
            return observation(body)
        if operation.kind == "gcp.custom_role":
            body = _read(self.iam.projects().roles().get(name=self.role_name), absent_ok=True)
            if body is not None and body.get("name") != self.role_name:
                raise BlockedError("GCP role identity mismatch")
            return observation(body, body.get("etag") if body else None)
        body = _read(self.storage.buckets().getIamPolicy(bucket=self.endpoint.resource,
                                                       optionsRequestedPolicyVersion=3))
        if not isinstance(body.get("etag"), str) or not body["etag"]:
            raise BlockedError("GCP bucket policy etag is missing")
        if not isinstance(body.get("bindings", []), list):
            raise BlockedError("malformed GCP bucket policy")
        for binding in body.get("bindings", []):
            if (not isinstance(binding, dict) or not isinstance(binding.get("role"), str)
                    or not isinstance(binding.get("members"), list)
                    or any(not isinstance(m, str) for m in binding["members"])):
                raise BlockedError("malformed GCP bucket binding")
        return observation(body, body["etag"])

    def observe(self, operation: Operation) -> dict:
        self._check(operation)
        self._scope()
        return self._observe(operation)

    def satisfied(self, operation: Operation, current: dict) -> bool:
        self._check(operation)
        if not current.get("exists"):
            return False
        body = current.get("body", {})
        if operation.kind == "gcp.service_account":
            return (body.get("email") == self.endpoint.identity and body.get("projectId") == self.endpoint.scope
                    and isinstance(body.get("uniqueId"), str) and body["uniqueId"].isdigit()
                    and not body.get("disabled", False))
        if operation.kind == "gcp.custom_role":
            return (body.get("name") == self.role_name
                    and set(body.get("includedPermissions", [])) == set(operation.desired["included_permissions"])
                    and not body.get("deleted", False) and body.get("stage", "GA") != "DISABLED")
        return any(binding.get("role") == operation.desired["role"] and "condition" not in binding
                   and operation.desired["member"] in binding.get("members", [])
                   for binding in body.get("bindings", []))

    def _validate_permissions(self, permissions: list[str]) -> None:
        available: dict[str, dict] = {}
        token: str | None = None
        seen: set[str] = set()
        for _ in range(1000):
            body = {"fullResourceName": f"//storage.googleapis.com/projects/_/buckets/{self.endpoint.resource}"}
            if token:
                body["pageToken"] = token
            result = _read(self.iam.permissions().queryTestablePermissions(body=body))
            values = result.get("permissions", [])
            if not isinstance(values, list) or any(not isinstance(p, dict) or not isinstance(p.get("name"), str) for p in values):
                raise BlockedError("malformed GCP permission-support response")
            available.update({p["name"]: p for p in values})
            token = result.get("nextPageToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen:
                raise BlockedError("GCP permission pagination did not complete")
            seen.add(token)
        else:
            raise BlockedError("GCP permission pagination limit reached")
        for name in permissions:
            permission = available.get(name)
            if (not permission or permission.get("customRolesSupportLevel", "SUPPORTED") != "SUPPORTED"
                    or permission.get("stage") in {"DISABLED", "DEPRECATED"} or permission.get("apiDisabled", False)):
                raise BlockedError("GCP permission not confirmed for custom roles on the target bucket")

    def apply(self, operation: Operation, before: dict) -> dict:
        self._check(operation)
        current = self.observe(operation)
        if not same_observation(current, before):
            raise ConflictError("GCP target changed since its recorded snapshot")
        if self.satisfied(operation, current):
            return current
        if operation.kind == "gcp.service_account":
            if current["exists"]:
                raise ConflictError("GCP service account already exists with incompatible state")
            request = self.iam.projects().serviceAccounts().create(
                name=f"projects/{self.endpoint.scope}",
                body={"accountId": operation.desired["account_id"],
                      "serviceAccount": {"displayName": "Scoped migration execution identity"}})
        elif operation.kind == "gcp.custom_role":
            if current["exists"]:
                raise ConflictError("GCP custom role already exists with different permissions")
            self._validate_permissions(operation.desired["included_permissions"])
            request = self.iam.projects().roles().create(
                parent=f"projects/{self.endpoint.scope}",
                body={"roleId": self.role_name.rsplit("/", 1)[1],
                      "role": {"title": "Migration object reader", "stage": "GA",
                               "includedPermissions": sorted(operation.desired["included_permissions"])}})
        else:
            # Do not grant a name that can be recreated after review to a new principal.
            account = self._account()
            if account is None or account.get("disabled", False):
                raise BlockedError("GCP binding identity must exist and be enabled")
            # A mutable custom-role name must not become a path to wider grants
            # between the role operation and this bucket operation.
            role = self._observe(Operation("check-role", "gcp", "gcp.custom_role", self.endpoint.scope,
                                          self.role_name, {"included_permissions": sorted(READ_PERMISSIONS)}, None))
            permissions = role.get("body", {}).get("includedPermissions", [])
            if (not role["exists"] or not permissions or not set(permissions) <= READ_PERMISSIONS
                    or role["body"].get("deleted", False) or role["body"].get("stage", "GA") == "DISABLED"):
                raise BlockedError("GCP binding custom role must contain only supported object-read permissions")
            policy = copy.deepcopy(current["body"])
            if not current.get("version") or policy.get("etag") != current["version"]:
                raise BlockedError("GCP conditional update requires the reviewed etag")
            bindings = policy.setdefault("bindings", [])
            for binding in bindings:
                if binding["role"] == operation.desired["role"] and "condition" not in binding:
                    binding["members"] = sorted(set(binding["members"]) | {operation.desired["member"]})
                    break
            else:
                bindings.append({"role": operation.desired["role"], "members": [operation.desired["member"]]})
            # Preserve all original conditional bindings and policy version.
            if any("condition" in b for b in bindings):
                policy["version"] = 3
            request = self.storage.buckets().setIamPolicy(bucket=self.endpoint.resource, body=policy)
        _write(request)
        after = self.observe(operation)
        if not self.satisfied(operation, after):
            raise UnknownOutcome("GCP accepted mutation is not yet observable")
        return after

    def _role(self, name: str) -> dict:
        if name.startswith("roles/"):
            return _read(self.iam.roles().get(name=name))
        if name.startswith("projects/"):
            return _read(self.iam.projects().roles().get(name=name))
        if name.startswith("organizations/"):
            return _read(self.iam.organizations().roles().get(name=name))
        raise BlockedError("invalid GCP role name in policy")

    def collect(self) -> dict:
        inventory = empty_inventory(self.endpoint)
        coverage = inventory["coverage"]
        coverage.update({key: "unknown" for key in ("identity", "identity_policies", "resource_policy", "boundary",
                                                    "organization", "session", "resource_constraints")})
        inventory["limitations"].extend([
            "IAM allow policies do not establish complete effective permissions",
            "Principal boundaries, IAM deny, organization policies and session context are not collected",
            "Per-object ACL, managed-folder policies and object encryption context are not collected",
        ])

        def capture(label: str, fn: Any) -> Any:
            try:
                return fn()
            except BlockedError as exc:
                coverage[label] = "denied" if "denied" in str(exc) else "failed"
                inventory["evidence"].append(f"{label}: {exc}")
                return None
            except IdentityError as exc:
                coverage[label] = "failed"
                inventory["evidence"].append(f"{label}: {exc}")
                return None
            except Exception:
                coverage[label] = "failed"
                inventory["evidence"].append(f"{label}: GCP read failed")
                return None

        project = capture("identity", self._project)
        if project is None:
            return inventory
        bucket = capture("resource_constraints", lambda: self._bucket(project))
        if bucket is None:
            return inventory
        inventory["evidence"].append(f"confirmed project number {project['projectNumber']}")
        inventory["evidence"].append("bucket ownership verified against project number")
        account_op = Operation("collect-account", "gcp", "gcp.service_account", self.endpoint.scope,
                               self.endpoint.identity, {"account_id": self.endpoint.identity.split("@")[0]}, None)
        role_op = Operation("collect-role", "gcp", "gcp.custom_role", self.endpoint.scope,
                            self.role_name, {"included_permissions": sorted(READ_PERMISSIONS)}, None)
        binding_op = Operation("collect-binding", "gcp", "gcp.bucket_binding", self.endpoint.scope,
                               self.endpoint.resource,
                               {"role": self.role_name, "member": f"serviceAccount:{self.endpoint.identity}"}, None)
        for label, op in (("identity", account_op), ("identity_policies", role_op), ("resource_policy", binding_op)):
            value = capture(label, lambda op=op: self._observe(op))
            if value is not None:
                inventory["observations"][observation_key(op.kind, op.resource)] = value
                if op.kind == "gcp.service_account":
                    coverage["identity"] = "complete"
                    if value["exists"]:
                        body = value["body"]
                        inventory["identities"].append({"provider": "gcp", "scope": self.endpoint.scope,
                                                         "id": body["uniqueId"], "name": body["email"]})
                        trust = capture("identity_policies", lambda: _read(self.iam.projects().serviceAccounts().getIamPolicy(
                            resource=self.service_account_name, options_requestedPolicyVersion=3)))
                        if trust is not None:
                            inventory["trust"].append({"kind": "gcp.service_account_policy", "resource": self.endpoint.identity,
                                                       "policy": trust})
                elif op.kind == "gcp.bucket_binding":
                    coverage["resource_policy"] = "complete"
                    inventory["policies"].append({"kind": "gcp.allow_policy", "resource": self.endpoint.resource,
                                                  "policy": value["body"]})

        project_policy = capture("identity_policies", lambda: _read(self.crm.projects().getIamPolicy(
            resource=self.endpoint.scope, body={"options": {"requestedPolicyVersion": 3}})))
        if project_policy is not None:
            inventory["policies"].append({"kind": "gcp.allow_policy", "resource": f"projects/{self.endpoint.scope}",
                                          "policy": project_policy})
        ancestors = capture("organization", lambda: _read(self.crm.projects().getAncestry(projectId=self.endpoint.scope, body={})))
        if ancestors is not None:
            inventory["evidence"].append("project ancestry enumerated; ancestor allow/deny/org constraints remain uncollected")
        roles = sorted({binding.get("role") for item in inventory["policies"] if item["kind"] == "gcp.allow_policy"
                        for binding in item["policy"].get("bindings", [])
                        if isinstance(binding, dict) and isinstance(binding.get("role"), str)})
        roles_complete = project_policy is not None
        for name in roles:
            value = capture("identity_policies", lambda name=name: self._role(name))
            if value is None:
                roles_complete = False
            else:
                permissions = value.get("includedPermissions")
                if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions):
                    roles_complete = False
                    inventory["evidence"].append("role includedPermissions missing")
                else:
                    inventory["policies"].append({"kind": "gcp.role", "name": name,
                                                  "included_permissions": sorted(permissions)})
        # This means these local allow-policy definitions were read, not that
        # other resources, inherited allow/deny or group memberships were read.
        if roles_complete and coverage["identity_policies"] not in {"denied", "failed"}:
            coverage["identity_policies"] = "complete"
        reject_secrets(inventory)
        return inventory


def from_environment(endpoint: Endpoint) -> GcpAdapter:
    try:
        import google.auth
        from googleapiclient.discovery import build
    except ImportError:
        raise ConfigError("install migration[identity-gcp] to use live GCP collection") from None
    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return GcpAdapter(endpoint,
                          iam=build("iam", "v1", credentials=credentials, cache_discovery=False),
                          storage=build("storage", "v1", credentials=credentials, cache_discovery=False),
                          crm=build("cloudresourcemanager", "v1", credentials=credentials, cache_discovery=False),
                          credentials=credentials)
    except IdentityError:
        raise
    except Exception:
        raise ConfigError("GCP application-default authentication could not be initialized") from None
