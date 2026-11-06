#!/usr/bin/python
"""Product API operations. Sensitive module results are never returned."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from pathlib import Path

from ansible.module_utils.bao_common import (
    BaoError,
    Client,
    atomic,
    bundle,
    digest,
    ensure,
    read_json,
    same,
    save_json,
)
from ansible.module_utils.basic import AnsibleModule


def policy_for(mount, prefix, capabilities):
    caps = json.dumps(capabilities)
    return (
        f'path "{mount}/data/{prefix}/*" {{ capabilities = {caps} }}\n'
        f'path "{mount}/metadata/{prefix}/*" {{ capabilities = ["list", "read"] }}\n'
    )


class Manager:
    def __init__(self, config, state_dir, api=None):
        self.c = config
        self.state = Path(state_dir)
        self.id = config["target"]["installation_id"]
        self.prefix = "oi-" + self.id
        self.bootstrap_path = self.state / "bootstrap.json"
        self.ledger_path = self.state / "managed.json"
        self.ledger = (
            read_json(self.ledger_path)
            if self.ledger_path.exists()
            else {"resources": {}}
        )
        self.api = api or Client(config["endpoint"]["url"], config["remote_ca"])

    def authenticate(self):
        supplied = self.c.get("credentials", {}).get("admin_token")
        if supplied:
            self.api.token = supplied
        elif self.bootstrap_path.exists():
            self.api.token = read_json(self.bootstrap_path).get("root_token")
        if not self.api.token:
            self.api.token = self.c.get("credentials", {}).get("operator_token")
        ensure(bool(self.api.token), "ADMIN_CREDENTIAL_REQUIRED")

    def owned(self, path, desired, read_path=None, field=None, opaque=False):
        entry = self.ledger["resources"].get(path)
        wanted = digest(desired)
        try:
            actual = self.api.call("GET", read_path or path).get("data", {})
            if field:
                actual = actual.get(field)
        except BaoError as exc:
            if str(exc) != "NOT_FOUND":
                raise
            actual = None
        if same(actual, desired) or (opaque and entry == wanted and actual is not None):
            return False
        ensure(actual is None or entry is not None, "RESOURCE_OWNERSHIP_CONFLICT")
        self.ledger["resources"][path] = wanted
        save_json(self.ledger_path, self.ledger)
        self.api.call("POST", path, desired)
        return True

    def auth_mount(self, path, type_):
        auths = self.api.call("GET", "sys/auth")["data"]
        entry = auths.get(path + "/")
        if entry:
            ensure(entry["type"] == type_, "AUTH_TYPE_CONFLICT")
            ensure(
                self.ledger["resources"].get("sys/auth/" + path) == type_,
                "RESOURCE_OWNERSHIP_CONFLICT",
            )
            return False
        self.ledger["resources"]["sys/auth/" + path] = type_
        save_json(self.ledger_path, self.ledger)
        self.api.call("POST", "sys/auth/" + path, {"type": type_})
        return True

    def bootstrap(self):
        health = self.api.health()
        if health.get("initialized"):
            ensure(
                self.bootstrap_path.exists(), "INITIALIZED_WITHOUT_BOOTSTRAP_RECEIPT"
            )
            return {
                "status": "installed",
                "initialized": True,
                "sealed": bool(health.get("sealed")),
            }, False
        ensure(
            not (self.state / "init-attempt.json").exists(),
            "INITIALIZATION_OUTCOME_UNKNOWN",
        )
        save_json(self.state / "init-attempt.json", {"attempted": True})
        response = self.api.call(
            "POST", "sys/init", {"recovery_shares": 1, "recovery_threshold": 1}
        )
        # Preserve received recovery material even if a future API response is incomplete.
        save_json(self.bootstrap_path, response)
        ensure(
            response.get("root_token") and response.get("recovery_keys_base64"),
            "INITIALIZATION_RESPONSE_INCOMPLETE",
        )
        self.api.token = response["root_token"]
        authenticated = False
        for _ in range(30):
            health = self.api.health()
            if not health.get("sealed") and not health.get("standby"):
                try:
                    self.api.call("GET", "auth/token/lookup-self")
                    authenticated = True
                    break
                except BaoError as exc:
                    if str(exc) not in (
                        "HTTP_STATUS_500",
                        "SEALED",
                        "CONNECTION_OR_TLS_FAILED",
                    ):
                        raise
            time.sleep(1)
        ensure(authenticated, "AUTO_UNSEAL_OR_LEADERSHIP_FAILED")
        return {"status": "installed", "initialized": True, "sealed": False}, True

    def configure(self):
        self.authenticate()
        ensure(
            self.c["endpoint"]["product_confirmed"]
            or (self.state / "installation.json").exists(),
            "PRODUCT_CONFIRMATION_REQUIRED",
        )
        mounts = self.api.call("GET", "sys/mounts")["data"]
        access = self.c["access"]
        mount, prefix = access["mount"], access["prefix"]
        changed = False
        if mount + "/" not in mounts:
            self.api.call(
                "POST",
                "sys/mounts/" + mount,
                {"type": "kv", "options": {"version": "2"}},
            )
            changed = True
        else:
            m = mounts[mount + "/"]
            ensure(
                m["type"] == "kv" and str(m.get("options", {}).get("version")) == "2",
                "KV_VERSION_CONFLICT",
            )
        # Dedicated audit device, never reconfigure an existing device.
        audits = self.api.call("GET", "sys/audit")["data"]
        auditname = self.prefix
        auditpath = "/var/log/openbao/audit.json"
        reuse = self.c["target"]["mode"] == "reuse"
        if reuse:
            ensure(bool(audits), "EXISTING_AUDIT_REQUIRED")
        else:
            ensure(auditname + "/" in audits, "DECLARATIVE_AUDIT_REQUIRED")
            ensure(
                audits[auditname + "/"].get("type") == "file"
                and audits[auditname + "/"].get("options", {}).get("file_path")
                == auditpath,
                "AUDIT_CONFLICT",
            )
        policy = policy_for(mount, prefix, ["create", "update", "read"])
        changed |= self.owned(
            "sys/policies/acl/" + self.prefix + "-migration", {"policy": policy}
        )
        changed |= self.auth_mount(self.prefix + "-migration", "approle")
        role = {
            "token_policies": [self.prefix + "-migration"],
            "token_ttl": access["token_ttl_seconds"],
            "token_max_ttl": access["token_ttl_seconds"],
            "secret_id_ttl": access["secret_id_ttl_seconds"],
            "secret_id_num_uses": 1,
            "token_no_default_policy": False,
        }
        changed |= self.owned("auth/" + self.prefix + "-migration/role/transfer", role)
        pending = []
        kc = self.c.get("keycloak")
        if kc:
            changed |= self.auth_mount(self.prefix + "-operators", "oidc")
            operator_policy = (
                f'path "{mount}/data/{prefix}/*" {{ capabilities = ["create", "update", "read", "delete"] }}\n'
                f'path "{mount}/metadata/{prefix}/*" {{ capabilities = ["create", "update", "read", "list", "delete"] }}\n'
                f'path "auth/{self.prefix}-migration/role/transfer/role-id" {{ capabilities = ["read"] }}\n'
                f'path "auth/{self.prefix}-migration/role/transfer/secret-id" {{ capabilities = ["update"] }}\n'
                'path "auth/token/revoke-self" { capabilities = ["update"] }\n'
            )
            changed |= self.owned(
                "sys/policies/acl/" + self.prefix + "-operators",
                {"policy": operator_policy},
            )
            oidc = {
                "oidc_discovery_url": kc["issuer"],
                "oidc_client_id": kc["client_id"],
                "oidc_client_secret": self.c["credentials"]["oidc_client_secret"],
                "oidc_discovery_ca_pem": self.c["credentials"]["oidc_ca"],
                "default_role": "operator",
            }
            changed |= self.owned(
                "auth/" + self.prefix + "-operators/config", oidc, opaque=True
            )
            callbacks = [
                self.c["endpoint"]["url"]
                + "/ui/vault/auth/"
                + self.prefix
                + "-operators/oidc/callback"
            ]
            # Input has the actual mount in its callback; no wildcard callbacks.
            ensure(kc["redirect_uris"] == callbacks, "OIDC_CALLBACK_MISMATCH")
            operator_role = {
                "role_type": "oidc",
                "user_claim": "sub",
                "groups_claim": kc["groups_claim"],
                "bound_audiences": [kc["client_id"]],
                "allowed_redirect_uris": callbacks,
                "bound_claims": {kc["groups_claim"]: kc["allowed_groups"]},
                "token_policies": [self.prefix + "-operators"],
                "token_ttl": 300,
                "token_max_ttl": 900,
            }
            changed |= self.owned(
                "auth/" + self.prefix + "-operators/role/operator", operator_role
            )
        else:
            pending.append("KEYCLOAK_CLIENT_REQUIRED")
        if not self.c["applications"]:
            pending.append("APPLICATION_IDENTITY_REQUIRED")
        for app in self.c["applications"]:
            name = self.prefix + "-app-" + app["name"]
            changed |= self.auth_mount(name, "jwt")
            changed |= self.owned(
                "sys/policies/acl/" + name,
                {"policy": policy_for(mount, app["prefix"], ["read"])},
            )
            jwt = {
                "oidc_discovery_url": app["discovery_url"],
                "bound_issuer": app["issuer"],
                "oidc_discovery_ca_pem": self.c["credentials"]["app_ca"][app["name"]],
            }
            changed |= self.owned("auth/" + name + "/config", jwt)
            app_role = {
                "role_type": "jwt",
                "user_claim": "sub",
                "bound_audiences": [app["audience"]],
                "bound_subject": "system:serviceaccount:"
                + app["namespace"]
                + ":"
                + app["service_account"],
                "token_policies": [name],
                "token_ttl": app["token_ttl_seconds"],
                "token_max_ttl": app["token_ttl_seconds"],
            }
            changed |= self.owned("auth/" + name + "/role/application", app_role)
        save_json(
            self.state / "configured.json",
            {
                "config_digest": digest(
                    {
                        k: v
                        for k, v in self.c.items()
                        if k
                        not in ("credentials", "request_id", "handoff", "verification")
                    }
                ),
                "configured": True,
            },
        )
        return {
            "status": "configured" if not pending else "partially_ready",
            "reason_codes": pending,
            "migration_auth_mount": self.prefix + "-migration",
            "operator_auth_mount": self.prefix + "-operators",
            "kv_mount": mount,
            "path_prefix": prefix,
        }, changed

    def migration_token(self, persist=False):
        self.authenticate()
        root = "auth/" + self.prefix + "-migration/"
        role_id = self.api.call("GET", root + "role/transfer/role-id")["data"][
            "role_id"
        ]
        secret_id = self.api.call("POST", root + "role/transfer/secret-id", {})["data"][
            "secret_id"
        ]
        auth = self.api.call(
            "POST", root + "login", {"role_id": role_id, "secret_id": secret_id}
        )["auth"]
        if persist:
            ensure(
                0
                < auth.get("lease_duration", 0)
                <= self.c["access"]["token_ttl_seconds"],
                "TOKEN_TTL_INVALID",
            )
            atomic(self.state / "migration.token", auth["client_token"])
            return {
                "status": "token_issued",
                "credential_remote_ref": str(self.state / "migration.token"),
                "expires_at": int(time.time()) + auth["lease_duration"],
            }, True
        return Client(
            self.c["endpoint"]["url"], self.c["remote_ca"], auth["client_token"]
        )

    def verify_seed(self):
        self.authenticate()
        path = self.c["access"]["prefix"] + "/_infra_verify/" + self.id
        value = secrets.token_hex(32)
        save_json(self.state / "canary.json", {"path": path, "value": value})
        migration = self.migration_token()
        try:
            migration.call(
                "POST",
                self.c["access"]["mount"] + "/data/" + path,
                {"data": {"value": value}},
            )
        finally:
            migration.call("POST", "auth/token/revoke-self", {})
        return {"status": "seeded"}, True

    def verify(self):
        self.authenticate()
        for _ in range(30):
            h = self.api.health()
            if h.get("initialized") and not h.get("sealed"):
                break
            time.sleep(1)
        ensure(h.get("initialized") and not h.get("sealed"), "SERVICE_NOT_READY")
        checks = {
            "service": "passed",
            "migration": "not_run",
            "application": "not_run",
            "operator": "not_run",
            "restart": "not_run",
            "key_handoff": "not_run",
            "bootstrap_revoked": "not_run",
            "recovery": "not_run",
        }
        migration = self.migration_token()
        mount = self.c["access"]["mount"]
        outside = mount + "/data/denied-" + secrets.token_hex(8) + "/probe"
        try:
            caps = migration.call(
                "POST",
                "sys/capabilities-self",
                {"paths": [outside, "sys/auth", "sys/policies/acl/unrelated"]},
            )["data"]
            ensure(
                all(
                    caps.get(p) == ["deny"]
                    for p in [outside, "sys/auth", "sys/policies/acl/unrelated"]
                ),
                "MIGRATION_OVERPRIVILEGED",
            )
            if (self.state / "canary.json").exists():
                canary = read_json(self.state / "canary.json")
                found = migration.call("GET", mount + "/data/" + canary["path"])[
                    "data"
                ]["data"]
                ensure(found.get("value") == canary["value"], "PERSISTENCE_FAILED")
                checks["migration"] = "passed"
                if self.c.get("restart_performed"):
                    checks["restart"] = "passed"
                self.api.call("DELETE", mount + "/metadata/" + canary["path"])
                (self.state / "canary.json").unlink()
        finally:
            migration.call("POST", "auth/token/revoke-self", {})
        apps = self.c["applications"]
        if apps and all(
            a["name"] in self.c.get("credentials", {}).get("app_jwt", {}) for a in apps
        ):
            for app in apps:
                identity = self.c["credentials"]["app_jwt"][app["name"]]
                token = self.api.call(
                    "POST",
                    "auth/" + self.prefix + "-app-" + app["name"] + "/login",
                    {"role": "application", "jwt": identity},
                )["auth"]["client_token"]
                app_client = Client(
                    self.c["endpoint"]["url"], self.c["remote_ca"], token
                )
                try:
                    own = mount + "/data/" + app["prefix"] + "/probe"
                    invalid = mount + "/data/denied-" + secrets.token_hex(8) + "/probe"
                    caps = app_client.call(
                        "POST", "sys/capabilities-self", {"paths": [own, invalid]}
                    )["data"]
                    ensure(
                        caps.get(own) == ["read"] and caps.get(invalid) == ["deny"],
                        "APPLICATION_OVERPRIVILEGED",
                    )
                finally:
                    app_client.call("POST", "auth/token/revoke-self", {})
            checks["application"] = "passed"
        operator = self.c.get("credentials", {}).get("operator_token")
        if operator:
            op = Client(self.c["endpoint"]["url"], self.c["remote_ca"], operator)
            info = op.call("GET", "auth/token/lookup-self")["data"]
            ensure(
                info.get("path", "").startswith("auth/" + self.prefix + "-operators/")
                and self.prefix + "-operators" in info.get("policies", [])
                and "root" not in info.get("policies", []),
                "OPERATOR_IDENTITY_INVALID",
            )
            checks["operator"] = "passed"
        if (self.state / "handoff-ack.json").exists():
            checks["key_handoff"] = "passed"
        if (self.state / "finalized.json").exists():
            checks["bootstrap_revoked"] = "passed"
        if self.c["target"]["mode"] == "reuse":
            for key in ("restart", "key_handoff", "bootstrap_revoked"):
                checks[key] = "not_applicable"
        ready = all(
            checks[k] in ("passed", "not_applicable")
            for k in (
                "service",
                "migration",
                "application",
                "operator",
                "restart",
                "key_handoff",
                "bootstrap_revoked",
            )
        )
        return {
            "status": "ready" if ready else "partially_ready",
            "checks": checks,
            "recovery_verified": False,
            "initialized": True,
            "sealed": False,
        }, False

    def finalize(self):
        self.authenticate()
        if (self.state / "finalized.json").exists():
            return {"status": "configured", "bootstrap_revoked": True}, False
        ensure((self.state / "handoff-ack.json").exists(), "KEY_HANDOFF_REQUIRED")
        token = self.c.get("credentials", {}).get("operator_token")
        ensure(token and token != self.api.token, "INDEPENDENT_OPERATOR_REQUIRED")
        op = Client(self.c["endpoint"]["url"], self.c["remote_ca"], token)
        info = op.call("GET", "auth/token/lookup-self")["data"]
        ensure(
            info.get("path", "").startswith("auth/" + self.prefix + "-operators/")
            and self.prefix + "-operators" in info.get("policies", [])
            and "root" not in info.get("policies", []),
            "OPERATOR_IDENTITY_INVALID",
        )
        if self.bootstrap_path.exists():
            bootstrap = read_json(self.bootstrap_path)
            if bootstrap.get("root_token"):
                root = Client(
                    self.c["endpoint"]["url"],
                    self.c["remote_ca"],
                    bootstrap["root_token"],
                )
                try:
                    root.call("POST", "auth/token/revoke-self", {})
                except BaoError as exc:
                    if str(exc) != "ACCESS_DENIED":
                        raise
                del bootstrap["root_token"]
                save_json(self.bootstrap_path, bootstrap)
        save_json(self.state / "finalized.json", {"finalized": True})
        return {"status": "configured", "bootstrap_revoked": True}, True


def execute(module):
    p = module.params
    c = p["config"]
    c["credentials"] = p["credentials"]
    state = Path(p["state_dir"])
    ensure(state.is_dir() and not state.is_symlink(), "STATE_DIRECTORY_REQUIRED")
    fd = os.open(
        state / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BaoError("CONCURRENT_OPERATION") from None
        action = p["action"]
        manager = Manager(c, state)
        if action == "bundle":
            h = manager.api.health()
            save_json(
                state / "recovery-bundle.json",
                bundle(
                    "/etc/openbao/keys/static.key",
                    manager.bootstrap_path,
                    manager.id,
                    h["cluster_id"],
                ),
            )
            report, changed = {"status": "bundle_prepared"}, True
        elif action == "ack":
            ensure(c.get("handoff_receipt_digest"), "HANDOFF_RECEIPT_REQUIRED")
            save_json(
                state / "handoff-ack.json",
                {"receipt_digest": c["handoff_receipt_digest"]},
            )
            (state / "recovery-bundle.json").unlink(missing_ok=True)
            report, changed = {"status": "handoff_acknowledged"}, True
        else:
            fn = {
                "bootstrap": manager.bootstrap,
                "configure": manager.configure,
                "verify_seed": manager.verify_seed,
                "verify": manager.verify,
                "finalize": manager.finalize,
                "issue-migration-token": lambda: manager.migration_token(persist=True),
            }[action]
            report, changed = fn()
        module.exit_json(changed=changed, report=report)
    finally:
        os.close(fd)


def main():
    module = AnsibleModule(
        argument_spec={
            "action": {"type": "str", "required": True},
            "config": {"type": "dict", "required": True},
            "credentials": {"type": "dict", "default": {}, "no_log": True},
            "state_dir": {"type": "path", "required": True},
        },
        supports_check_mode=False,
    )
    try:
        execute(module)
    except BaoError as exc:
        module.fail_json(msg="OpenBao operation blocked", reason_code=str(exc))
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        module.fail_json(
            msg="OpenBao operation failed", reason_code="PRODUCT_OPERATION_FAILED"
        )


if __name__ == "__main__":
    main()
