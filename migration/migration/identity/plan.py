"""Explicitly reviewed, account-scoped target entitlements; never name-based policy conversion."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from .collect import validate_inventory
from .common import digest, identifier, keys, require, stable_id, string


def assignment_id(a):
    return stable_id(
        a["account_id"], a["permission_set_arn"], a["principal_type"], a["principal_id"]
    )


def mapping_template(inv):
    validate_inventory(inv)
    return {
        "schema_version": 1,
        "inventory_hash": inv["inventory_hash"],
        "bindings": [
            {
                "assignment_id": assignment_id(a),
                "source": a,
                "business": "CHANGE_ME",
                "environment": "CHANGE_ME",
                "review_ref": "",
                "source_access_evidence": "",
                "targets": {},
            }
            for a in inv["assignments"]
        ],
        "excluded_assignments": [],
    }


def policy_blockers(doc):
    if not isinstance(doc, dict):
        return ["INVALID_POLICY"]
    statements = doc.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list) or not statements:
        return ["EMPTY_POLICY"]
    reasons = set()
    for st in statements:
        if not isinstance(st, dict) or st.get("Effect") != "Allow":
            reasons.add("EXPLICIT_DENY_OR_INVALID_EFFECT")
            continue
        if set(st) & {
            "NotAction",
            "NotResource",
            "Condition",
            "Principal",
            "NotPrincipal",
        }:
            reasons.add("UNSUPPORTED_POLICY_SEMANTICS")
        actions = st.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if (
            not isinstance(actions, list)
            or not actions
            or not all(isinstance(a, str) for a in actions)
        ):
            reasons.add("INVALID_ACTION")
        elif any("*" in a or "?" in a for a in actions):
            reasons.add("WILDCARD_ACTION_REQUIRES_FUTURE_TRANSLATOR")
        if not st.get("Resource"):
            reasons.add("RESOURCE_SCOPE_MISSING")
    return sorted(reasons)


def create_plan(config, inv, mapping):
    validate_inventory(inv)
    keys(
        mapping,
        {"schema_version", "inventory_hash", "bindings", "excluded_assignments"},
        {"schema_version", "inventory_hash", "bindings", "excluded_assignments"},
    )
    require(
        type(mapping["schema_version"]) is int and mapping["schema_version"] == 1,
        "MAPPING_VERSION",
    )
    require(mapping["inventory_hash"] == inv["inventory_hash"], "STALE_MAPPING")
    require(
        inv["source"] == {k: config["source"][k] for k in inv["source"]},
        "SOURCE_SCOPE_MISMATCH",
    )
    require(
        isinstance(mapping["bindings"], list)
        and isinstance(mapping["excluded_assignments"], list),
        "INVALID_MAPPING",
    )
    p = {
        "schema_version": 1,
        "kind": "identity_plan",
        "migration_id": config["migration_id"],
        "config_hash": digest(config),
        "inventory_hash": inv["inventory_hash"],
        "mapping_hash": digest(mapping),
        "resources": [],
        "blockers": [],
        "exclusions": [],
        "mapping_rows": [],
        "identity_rows": [],
        "handoffs": [],
        "deferred_iam_principals": [
            {k: r[k] for k in ("kind", "account_id", "arn", "migration_status")}
            for r in inv.get("iam_principals", [])
        ],
        "limitations": inv["limitations"],
        "verification": {"configuration": "not_run", "access": "not_run"},
    }
    p["handoffs"].append(
        {
            "kind": "credential_enrollment",
            "status": "required",
            "detail": "Imported users are disabled; provision credentials/MFA and enable them through an external onboarding process.",
        }
    )
    for item in inv["coverage"]:
        if item["status"] != "complete":
            p["blockers"].append(
                {"code": "SOURCE_COVERAGE_INCOMPLETE", "scope": item["scope"]}
            )
    users = {u["id"]: u for u in inv["users"]}
    groups = {g["id"]: g for g in inv["groups"]}
    assignments = {assignment_id(a): a for a in inv["assignments"]}
    require(len(assignments) == len(inv["assignments"]), "DUPLICATE_ASSIGNMENT")
    policies = {(r["account_id"], r["permission_set_arn"]): r for r in inv["policies"]}
    seen, resources = set(), {}
    owner = "identity:" + config["migration_id"]
    kc = config["targets"]["keycloak"]
    issuer = kc["base_url"].rstrip("/") + "/realms/" + kc["realm"]
    ca_pem = None
    if kc.get("ca_file"):
        ca_pem = Path(kc["ca_file"]).read_text()
        require(
            "-----BEGIN CERTIFICATE-----" in ca_pem and "PRIVATE KEY" not in ca_pem,
            "PUBLIC_CA_CERTIFICATE_REQUIRED",
        )

    def add(kind, name, payload, deps=()):
        resource_id = kind + ":" + name
        resource = {
            "id": resource_id,
            "kind": kind,
            "name": name,
            "payload": payload,
            "depends_on": list(deps),
        }
        if resource_id in resources:
            require(resources[resource_id] == resource, "CONFLICTING_TARGET_MAPPING")
        else:
            resources[resource_id] = resource
        return resource_id

    realm = add(
        "kc_realm",
        kc["realm"],
        {
            "realm": kc["realm"],
            "enabled": True,
            "attributes": {"identity.owner": owner},
        },
    )

    def client(name, redirects):
        identifier(name)
        require(name.startswith("identity-"), "DEDICATED_CLIENT_PREFIX_REQUIRED")
        c = add(
            "kc_client",
            name,
            {
                "clientId": name,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": True,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "implicitFlowEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": sorted(redirects),
                "attributes": {
                    "identity.owner": owner,
                    "pkce.code.challenge.method": "S256",
                },
            },
            [realm],
        )
        add(
            "kc_mapper",
            name + "/groups",
            {
                "client": name,
                "name": "identity-groups",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-group-membership-mapper",
                "consentRequired": False,
                "config": {
                    "claim.name": "groups",
                    "full.path": "false",
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                    "userinfo.token.claim": "true",
                },
            },
            [c],
        )
        # Explicit audience is needed for JWT consumers; do not accept tokens issued for arbitrary clients.
        add(
            "kc_mapper",
            name + "/audience",
            {
                "client": name,
                "name": "identity-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.client.audience": name,
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                },
            },
            [c],
        )
        return c

    for u in users.values():
        source_key = inv["source"]["identity_store_id"] + ":" + u["id"]
        p["identity_rows"].append(
            {
                "source_user_id": u["id"],
                "source_username": u["username"],
                "target_username": "aws-" + stable_id(source_key),
                "onboarding": "disabled_until_external_enrollment",
            }
        )
        add(
            "kc_user",
            u["id"],
            {
                "username": "aws-" + stable_id(source_key),
                "attributes": {
                    "identity.owner": [owner],
                    "identity.source": [source_key],
                    "identity.display_name": [u["display_name"]],
                    "identity.original_username": [u["username"]],
                },
            },
            [realm],
        )
    for g in groups.values():
        add(
            "kc_group",
            "source-" + g["id"],
            {
                "name": "identity-source-"
                + stable_id(inv["source"]["identity_store_id"], g["id"]),
                "attributes": {
                    "identity.owner": [owner],
                    "identity.source_group": [g["id"]],
                    "identity.display_name": [g["name"]],
                },
            },
            [realm],
        )
    for m in inv["memberships"]:
        require(m["user_id"] in users and m["group_id"] in groups, "ORPHAN_MEMBERSHIP")
        add(
            "kc_membership",
            m["user_id"] + "/source-" + m["group_id"],
            {"user": m["user_id"], "group": "source-" + m["group_id"]},
            ["kc_user:" + m["user_id"], "kc_group:source-" + m["group_id"]],
        )
    for exclusion in mapping["excluded_assignments"]:
        keys(exclusion, {"assignment_id", "reason"}, {"assignment_id", "reason"})
        aid = exclusion["assignment_id"]
        require(
            aid in assignments and aid not in seen, "UNKNOWN_OR_DUPLICATE_ASSIGNMENT"
        )
        string(exclusion["reason"])
        seen.add(aid)
        p["exclusions"].append(exclusion)
    scope_owners = {}
    for b in mapping["bindings"]:
        keys(
            b,
            {
                "assignment_id",
                "source",
                "business",
                "environment",
                "review_ref",
                "source_access_evidence",
                "targets",
            },
            {
                "assignment_id",
                "source",
                "business",
                "environment",
                "review_ref",
                "source_access_evidence",
                "targets",
            },
        )
        aid = b["assignment_id"]
        require(
            aid in assignments and aid not in seen, "UNKNOWN_OR_DUPLICATE_ASSIGNMENT"
        )
        seen.add(aid)
        a = assignments[aid]
        require(b["source"] == a, "SOURCE_ASSIGNMENT_CHANGED")
        identifier(b["business"])
        identifier(b["environment"])
        if (
            not b["review_ref"]
            or not b["source_access_evidence"]
            or "CHANGE_ME" in (b["business"], b["environment"])
        ):
            p["blockers"].append(
                {"code": "MAPPING_REVIEW_REQUIRED", "assignment_id": aid}
            )
        policy = policies.get((a["account_id"], a["permission_set_arn"]))
        if policy is None:
            p["blockers"].append(
                {"code": "ACCOUNT_POLICY_DOCUMENTS_MISSING", "assignment_id": aid}
            )
        else:
            reasons = ["PERMISSIONS_BOUNDARY_UNSUPPORTED"] if policy["boundary"] else []
            if not policy["documents"]:
                reasons.append("EMPTY_POLICY")
            for d in policy["documents"]:
                reasons.extend(policy_blockers(d["document"]))
            p["blockers"].extend(
                {"code": reason, "assignment_id": aid}
                for reason in sorted(set(reasons))
            )
        target = b["targets"]
        keys(target, {"kubernetes", "openbao", "applications"})
        if not target:
            p["blockers"].append(
                {"code": "TARGET_MAPPING_MISSING", "assignment_id": aid}
            )
        group_name = "identity-grant-" + stable_id(
            a["account_id"], a["permission_set_arn"], b["business"], b["environment"]
        )
        group_key = "grant-" + group_name
        group = add(
            "kc_group",
            group_key,
            {
                "name": group_name,
                "attributes": {
                    "identity.owner": [owner],
                    "identity.account": [a["account_id"]],
                    "identity.permission_set": [a["permission_set_arn"]],
                    "identity.business": [b["business"]],
                    "identity.environment": [b["environment"]],
                },
            },
            [realm],
        )
        if a["principal_type"] == "USER":
            require(a["principal_id"] in users, "ORPHAN_ASSIGNMENT")
            members = [a["principal_id"]]
        else:
            require(
                a["principal_type"] == "GROUP" and a["principal_id"] in groups,
                "ORPHAN_ASSIGNMENT",
            )
            members = [
                m["user_id"]
                for m in inv["memberships"]
                if m["group_id"] == a["principal_id"]
            ]
        for uid in members:
            add(
                "kc_membership",
                uid + "/" + group_key,
                {"user": uid, "group": group_key},
                ["kc_user:" + uid, group],
            )

        def claim_scope(
            scope, scope_identity=(a["account_id"], b["business"], b["environment"])
        ):
            previous = scope_owners.setdefault(scope, scope_identity)
            require(previous == scope_identity, "ACCOUNT_OR_ENVIRONMENT_COLLAPSE")

        if "kubernetes" in target:
            k = config["targets"].get("kubernetes")
            require(k is not None, "KUBERNETES_TARGET_UNAVAILABLE")
            kt = target["kubernetes"]
            keys(kt, {"namespace", "rules"}, {"namespace", "rules"})
            ns = kt["namespace"]
            require(
                isinstance(ns, str)
                and re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", ns),
                "NAMESPACE",
            )
            require(
                ns not in {"default", "kube-system", "kube-public", "kube-node-lease"},
                "DEDICATED_NAMESPACE_REQUIRED",
            )
            claim_scope("kubernetes:" + ns)
            validate_rules(kt["rules"])
            c = client(k["audience"], k["redirect_uris"])
            name = "identity-" + stable_id(group_name, ns)
            role = add(
                "k8s_role",
                ns + "/" + name,
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": {
                        "name": name,
                        "namespace": ns,
                        "labels": {
                            "app.kubernetes.io/managed-by": "identity-migration"
                        },
                    },
                    "rules": kt["rules"],
                },
                [group, c],
            )
            add(
                "k8s_binding",
                ns + "/" + name,
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {
                        "name": name,
                        "namespace": ns,
                        "labels": {
                            "app.kubernetes.io/managed-by": "identity-migration"
                        },
                    },
                    "subjects": [
                        {
                            "kind": "Group",
                            "name": k["group_prefix"] + group_name,
                            "apiGroup": "rbac.authorization.k8s.io",
                        }
                    ],
                    "roleRef": {
                        "kind": "Role",
                        "name": name,
                        "apiGroup": "rbac.authorization.k8s.io",
                    },
                },
                [role],
            )
        if "openbao" in target:
            bao = config["targets"].get("openbao")
            require(bao is not None, "OPENBAO_TARGET_UNAVAILABLE")
            paths = target["openbao"]
            validate_paths(paths)
            for path in paths:
                claim_scope("openbao:" + path["path"])
            mount = add(
                "bao_mount", bao["auth_mount"], {"type": "jwt", "description": owner}
            )
            auth = add(
                "bao_config",
                bao["auth_mount"],
                {
                    "oidc_discovery_url": issuer,
                    "bound_issuer": issuer,
                    "jwt_supported_algs": ["RS256"],
                    **({"oidc_discovery_ca_pem": ca_pem} if ca_pem else {}),
                },
                [mount, realm],
            )
            name = "identity-" + stable_id(group_name)
            policy_id = add("bao_policy", name, {"paths": paths})
            add(
                "bao_role",
                bao["auth_mount"] + "/" + name,
                {
                    "role_type": "jwt",
                    "user_claim": "sub",
                    "groups_claim": "groups",
                    "claim_mappings": {"sub": "source_subject"},
                    "bound_audiences": [bao["audience"]],
                    "bound_claims": {"groups": group_name},
                    "token_policies": [name],
                    "token_no_default_policy": True,
                    "token_ttl": 900,
                    "token_max_ttl": 900,
                },
                [auth, policy_id, group],
            )
            client(bao["audience"], ["http://localhost:8250/callback"])
        for at in target.get("applications", []):
            keys(at, {"id", "roles"}, {"id", "roles"})
            app = next(
                (
                    x
                    for x in config["targets"].get("applications", [])
                    if x["id"] == at["id"]
                ),
                None,
            )
            require(app is not None, "APPLICATION_TARGET_UNAVAILABLE")
            claim_scope("application:" + app["id"])
            c = client(app["client_id"], app["redirect_uris"])
            require(isinstance(at["roles"], list) and at["roles"], "APPLICATION_ROLES")
            for role_name in at["roles"]:
                identifier(role_name)
                role = add(
                    "kc_role",
                    app["client_id"] + "/" + role_name,
                    {
                        "client": app["client_id"],
                        "name": role_name,
                        "description": owner,
                    },
                    [c],
                )
                add(
                    "kc_group_role",
                    group_key + "/" + app["client_id"] + "/" + role_name,
                    {"group": group_key, "client": app["client_id"], "role": role_name},
                    [group, role],
                )
        p["mapping_rows"].append(
            {
                "assignment_id": aid,
                "source": a,
                "business": b["business"],
                "environment": b["environment"],
                "review_ref": b["review_ref"],
                "source_access_evidence": b["source_access_evidence"],
                "target_group": group_name,
                "targets": target,
                "semantics": "explicitly_reviewed_target_entitlements_not_universal_AWS_equivalence",
            }
        )
    for aid in sorted(set(assignments) - seen):
        p["blockers"].append({"code": "UNMAPPED_ASSIGNMENT", "assignment_id": aid})
    if "kubernetes" in config["targets"]:
        k = config["targets"]["kubernetes"]
        p["handoffs"].append(
            {
                "kind": "kubernetes_authentication",
                "status": "requires_cluster_operator",
                "authentication_config": {
                    "apiVersion": "apiserver.config.k8s.io/v1",
                    "kind": "AuthenticationConfiguration",
                    "jwt": [
                        {
                            "issuer": {
                                "url": issuer,
                                "audiences": [k["audience"]],
                                "audienceMatchPolicy": "MatchAny",
                                **({"certificateAuthority": ca_pem} if ca_pem else {}),
                            },
                            "claimMappings": {
                                "username": {"claim": "sub", "prefix": "keycloak:"},
                                "groups": {
                                    "claim": "groups",
                                    "prefix": k["group_prefix"],
                                },
                            },
                        }
                    ],
                },
                "detail": "Install the authentication config using the distribution-specific control-plane procedure; existing authentication must be preserved. Requires real-token verification.",
            }
        )
    for app in config["targets"].get("applications", []):
        p["handoffs"].append(
            {
                "kind": "application_configuration",
                "application_id": app["id"],
                "status": "requires_platform_configuration",
                "issuer": issuer,
                "client_id": app["client_id"],
                "groups_claim": "groups",
                "roles_claim": "resource_access." + app["client_id"] + ".roles",
                "redirect_uris": app["redirect_uris"],
                "detail": "Apply through the application's existing OIDC configuration. Source-code changes are excluded.",
            }
        )
    p["resources"] = list(resources.values())
    p["status"] = "blocked" if p["blockers"] else "planned"
    p["plan_hash"] = digest(p)
    return p


def validate_rules(rules):
    require(isinstance(rules, list) and rules, "KUBERNETES_RULES_REQUIRED")
    allowed = {
        "": {"pods", "pods/log", "services", "endpoints", "configmaps", "events"},
        "apps": {"deployments", "replicasets", "statefulsets", "daemonsets"},
        "batch": {"jobs", "cronjobs"},
    }
    for r in rules:
        keys(
            r,
            {"apiGroups", "resources", "verbs", "resourceNames"},
            {"apiGroups", "resources", "verbs"},
        )
        require(
            all(
                isinstance(r[f], list) and r[f]
                for f in ("apiGroups", "resources", "verbs")
            ),
            "KUBERNETES_RULE_LIST",
        )
        require(
            set(r["verbs"]) <= {"get", "list", "watch"},
            "KUBERNETES_READ_ONLY_INITIAL_SCOPE",
        )
        require(
            all(
                g in allowed and set(r["resources"]) <= allowed[g]
                for g in r["apiGroups"]
            ),
            "UNSUPPORTED_KUBERNETES_RESOURCE",
        )
        if "resourceNames" in r:
            require(
                isinstance(r["resourceNames"], list)
                and r["resourceNames"]
                and all(
                    isinstance(n, str) and n and "*" not in n
                    for n in r["resourceNames"]
                ),
                "RESOURCE_NAMES",
            )


def validate_paths(paths):
    require(isinstance(paths, list) and paths, "OPENBAO_PATHS_REQUIRED")
    seen = set()
    for item in paths:
        keys(item, {"path", "capabilities"}, {"path", "capabilities"})
        path = item["path"]
        require(
            isinstance(path, str)
            and re.fullmatch(r"[a-zA-Z0-9_-]+/data/[a-zA-Z0-9_./-]+", path)
            and all(p not in {"", ".", ".."} for p in path.split("/")),
            "EXACT_KV2_DATA_PATH_REQUIRED",
        )
        require(
            path.split("/", 1)[0] not in {"sys", "auth", "identity", "cubbyhole"},
            "RESERVED_OPENBAO_MOUNT",
        )
        require(path not in seen, "DUPLICATE_OPENBAO_PATH")
        seen.add(path)
        require(item["capabilities"] == ["read"], "OPENBAO_READ_ONLY_INITIAL_SCOPE")


def validate_plan(plan, config, *, allow_blocked=False):
    require(
        plan.get("kind") == "identity_plan" and plan.get("schema_version") == 1,
        "INVALID_PLAN",
    )
    require(
        plan.get("plan_hash")
        == digest({k: v for k, v in plan.items() if k != "plan_hash"}),
        "PLAN_HASH_MISMATCH",
    )
    require(plan["config_hash"] == digest(config), "CONFIG_CHANGED")
    require(
        allow_blocked or (not plan["blockers"] and plan["status"] == "planned"),
        "PLAN_BLOCKED",
    )
    require(
        len({r["id"] for r in plan["resources"]}) == len(plan["resources"]),
        "DUPLICATE_RESOURCE",
    )
    return copy.deepcopy(plan)
