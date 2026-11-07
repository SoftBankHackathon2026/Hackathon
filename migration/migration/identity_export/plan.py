from __future__ import annotations

import copy

from ..identity.common import IdentityError, digest, keys, require, string
from .collect import validate_inventory
from .common import account_id, owner, primary_email

RESOURCE_KINDS = frozenset({"user", "group", "membership", "assignment"})
USER_SCOPES = frozenset({"mapped_members", "all_enabled"})
ONBOARDING = "identity_center_invitation_and_password_setup_required"

FEATURE_EXCLUSIONS = [
    ("credentials_passwords_and_mfa", "NOT_MIGRATED_IDENTITY_CENTER_ONBOARDING"),
    ("client_scopes_and_protocol_mappers", "KEYCLOAK_ONLY_FEATURE"),
    ("fine_grained_authorization", "KEYCLOAK_ONLY_FEATURE"),
    ("identity_provider_links", "KEYCLOAK_ONLY_FEATURE"),
    ("user_attributes_and_required_actions", "KEYCLOAK_ONLY_FEATURE"),
    ("sessions_and_offline_tokens", "KEYCLOAK_ONLY_FEATURE"),
    ("composite_role_expansion", "NOT_EXPANDED_MAP_ROLES_EXPLICITLY"),
]


def source_key(src):
    keys(src, {"kind", "path", "role", "client"}, {"kind"})
    kind = src["kind"]
    if kind == "group":
        keys(src, {"kind", "path"}, {"kind", "path"})
        return (kind, string(src["path"]))
    if kind == "realm_role":
        keys(src, {"kind", "role"}, {"kind", "role"})
        return (kind, string(src["role"]))
    require(kind == "client_role", "MAPPING_SOURCE_KIND")
    keys(src, {"kind", "client", "role"}, {"kind", "client", "role"})
    return (kind, string(src["client"]), string(src["role"]))


def role_key(r):
    if r["kind"] == "realm_role":
        return ("realm_role", r["role"])
    return ("client_role", r["client"], r["role"])


def source_of(key):
    if key[0] == "group":
        return {"kind": "group", "path": key[1]}
    if key[0] == "realm_role":
        return {"kind": "realm_role", "role": key[1]}
    return {"kind": "client_role", "client": key[1], "role": key[2]}


def default_target(key):
    if key[0] == "group":
        return "keycloak" + key[1]
    if key[0] == "realm_role":
        return "keycloak-realm-role/" + key[1]
    return "keycloak-client-role/" + key[1] + "/" + key[2]


def source_keys(inv):
    groups = [("group", g["path"]) for g in inv["groups"]]
    roles = sorted({role_key(r) for r in inv["group_roles"]})
    return groups + roles


def mapping_template(inv):
    validate_inventory(inv)
    return {
        "schema_version": 1,
        "kind": "identity_export_mapping",
        "inventory_hash": inv["inventory_hash"],
        "options": {"include_disabled_users": False, "user_scope": "mapped_members"},
        "bindings": [
            {
                "source": source_of(key),
                "target_group": default_target(key),
                "review_ref": "",
                "assignments": [],
            }
            for key in source_keys(inv)
        ],
    }


def expected(r):
    p = r["payload"]
    if r["kind"] == "user":
        return {"user_name": p["UserName"].lower(), "email": primary_email(p["Emails"])}
    if r["kind"] == "group":
        return {"display_name": p["DisplayName"], "description": p["Description"]}
    if r["kind"] == "membership":
        return {"member": True}
    return {"assigned": True}


def conflict_code(r, observed):
    if r["kind"] == "user":
        if "email_used_by" in observed:
            return "EMAIL_USED_BY_ANOTHER_USER"
        return "EXISTING_USER_EMAIL_MISMATCH"
    if r["kind"] == "group":
        return "EXISTING_GROUP_NOT_OWNED"
    return "EXISTING_RESOURCE_CONFLICT"


def validate_mapping(mapping, inv, config):
    fields = {"schema_version", "kind", "inventory_hash", "options", "bindings"}
    keys(mapping, fields, fields)
    require(
        type(mapping["schema_version"]) is int
        and mapping["schema_version"] == 1
        and mapping["kind"] == "identity_export_mapping",
        "MAPPING_VERSION",
    )
    require(mapping["inventory_hash"] == inv["inventory_hash"], "STALE_MAPPING")
    require(
        inv["source"] == {k: config["source"][k] for k in inv["source"]},
        "SOURCE_SCOPE_MISMATCH",
    )
    options = mapping["options"]
    keys(
        options,
        {"include_disabled_users", "user_scope"},
        {"include_disabled_users", "user_scope"},
    )
    require(type(options["include_disabled_users"]) is bool, "MAPPING_OPTIONS")
    require(options["user_scope"] in USER_SCOPES, "MAPPING_OPTIONS")
    require(isinstance(mapping["bindings"], list), "INVALID_MAPPING")
    seen, names = set(), set()
    for b in mapping["bindings"]:
        fields = {"source", "target_group", "review_ref", "assignments"}
        keys(b, fields, fields)
        key = source_key(b["source"])
        require(key not in seen, "DUPLICATE_MAPPING_SOURCE")
        seen.add(key)
        require(isinstance(b["review_ref"], str), "REVIEW_REF")
        require(isinstance(b["assignments"], list), "MAPPING_ASSIGNMENTS")
        pairs = set()
        for a in b["assignments"]:
            keys(a, {"account_id", "permission_set"}, {"account_id", "permission_set"})
            account_id(a["account_id"])
            string(a["permission_set"])
            pair = (a["account_id"], a["permission_set"])
            require(pair not in pairs, "DUPLICATE_MAPPING_ASSIGNMENT")
            pairs.add(pair)
        if b["assignments"]:
            name = string(b["target_group"])
            require(len(name) <= 1024, "TARGET_GROUP_NAME")
            require(name not in names, "DUPLICATE_TARGET_GROUP")
            names.add(name)
    return mapping


def create_plan(config, inv, mapping, targets):
    validate_inventory(inv)
    validate_mapping(mapping, inv, config)
    options = mapping["options"]
    tag = owner(config)
    accounts = set(config["target"]["account_ids"])
    p = {
        "schema_version": 1,
        "kind": "identity_export_plan",
        "migration_id": config["migration_id"],
        "config_hash": digest(config),
        "inventory_hash": inv["inventory_hash"],
        "mapping_hash": digest(mapping),
        "resources": [],
        "blockers": [],
        "conflicts": [],
        "exclusions": [],
        "mapping_rows": [],
        "user_rows": [],
        "handoffs": [
            {
                "kind": "identity_center_onboarding",
                "status": "required",
                "detail": "Passwords, MFA devices and Keycloak credentials are not migrated. Every created user must complete IAM Identity Center onboarding (email verification invitation and password setup, then MFA registration) before signing in.",
            }
        ],
        "limitations": inv["limitations"],
        "verification": {"configuration": "not_run"},
    }
    for item in inv["coverage"]:
        if item["status"] != "complete":
            p["blockers"].append(
                {"code": "SOURCE_COVERAGE_INCOMPLETE", "scope": item["scope"]}
            )
    users = {u["id"]: u for u in inv["users"]}
    by_path = {g["path"]: g["id"] for g in inv["groups"]}
    children, members = {}, {}
    for g in inv["groups"]:
        children.setdefault(g["parent_id"], []).append(g["id"])
    for m in inv["memberships"]:
        if m["user_id"] not in users:
            p["blockers"].append(
                {"code": "ORPHAN_MEMBERSHIP", "group_id": m["group_id"]}
            )
            continue
        members.setdefault(m["group_id"], set()).add(m["user_id"])
    role_holders = {}
    for r in inv["group_roles"]:
        role_holders.setdefault(role_key(r), set()).add(r["group_id"])

    def effective(gids):
        found, queue = set(), list(gids)
        while queue:
            gid = queue.pop()
            found |= members.get(gid, set())
            queue.extend(children.get(gid, []))
        return found

    targets.check_instance()
    permission_sets = targets.permission_sets()
    by_name = {name: arn for arn, name in permission_sets.items()}
    bound, mapped = [], set()
    for b in mapping["bindings"]:
        key = source_key(b["source"])
        if key[0] == "group":
            holders = {by_path[key[1]]} if key[1] in by_path else None
        else:
            holders = role_holders.get(key)
        if holders is None:
            p["blockers"].append(
                {"code": "UNKNOWN_MAPPING_SOURCE", "source": b["source"]}
            )
            continue
        if not b["assignments"]:
            continue
        mapped.add(key)
        if not b["review_ref"]:
            p["blockers"].append(
                {"code": "MAPPING_REVIEW_REQUIRED", "source": b["source"]}
            )
        resolved = []
        for a in b["assignments"]:
            if a["account_id"] not in accounts:
                p["blockers"].append(
                    {
                        "code": "ACCOUNT_OUT_OF_SCOPE",
                        "source": b["source"],
                        "account_id": a["account_id"],
                    }
                )
                continue
            ps = a["permission_set"]
            arn = ps if ps in permission_sets else by_name.get(ps)
            if arn is None:
                p["blockers"].append(
                    {
                        "code": "PERMISSION_SET_NOT_FOUND",
                        "source": b["source"],
                        "permission_set": ps,
                    }
                )
                continue
            resolved.append(
                {
                    "account_id": a["account_id"],
                    "permission_set_arn": arn,
                    "permission_set_name": permission_sets[arn],
                }
            )
        bound.append((b, effective(holders), resolved))
    for key in source_keys(inv):
        if key not in mapped:
            p["exclusions"].append(
                {"kind": key[0], "source": source_of(key), "reason": "UNMAPPED"}
            )
    for r in inv["user_roles"]:
        p["exclusions"].append(
            {
                "kind": "user_role",
                "source": source_of(role_key(r)),
                "username": users[r["user_id"]]["username"],
                "reason": "USER_DIRECT_ROLE_MAPPING_NOT_MIGRATED",
            }
        )
    eligible = {}
    for u in users.values():
        if u["service_account"]:
            reason = "SERVICE_ACCOUNT_NOT_WORKFORCE_USER"
        elif not u["enabled"] and not options["include_disabled_users"]:
            reason = "DISABLED_USER"
        else:
            eligible[u["id"]] = u
            continue
        p["exclusions"].append(
            {"kind": "user", "username": u["username"], "reason": reason}
        )
    granted = set().union(*(m for _, m, _ in bound)) if bound else set()
    in_scope = {}
    for uid, u in eligible.items():
        if options["user_scope"] == "all_enabled" or uid in granted:
            in_scope[uid] = u
        else:
            p["exclusions"].append(
                {
                    "kind": "user",
                    "username": u["username"],
                    "reason": "NO_MAPPED_ACCESS",
                }
            )
    resources = []

    def add(kind, name, payload, deps=()):
        resources.append(
            {
                "id": kind + ":" + name,
                "kind": kind,
                "name": name,
                "payload": payload,
                "depends_on": list(deps),
            }
        )
        return kind + ":" + name

    user_ids = {}
    for uid, u in sorted(in_scope.items(), key=lambda x: x[1]["username"]):
        for field in ("email", "first_name", "last_name"):
            if not u[field]:
                p["blockers"].append(
                    {
                        "code": "USER_ATTRIBUTE_REQUIRED",
                        "username": u["username"],
                        "field": field,
                    }
                )
        first, last = u["first_name"] or "", u["last_name"] or ""
        user_ids[uid] = add(
            "user",
            u["username"],
            {
                "UserName": u["username"],
                "DisplayName": (first + " " + last).strip() or u["username"],
                "Name": {"GivenName": first, "FamilyName": last},
                "Emails": [{"Value": u["email"], "Type": "work", "Primary": True}]
                if u["email"]
                else [],
            },
        )
    for b, member_ids, resolved in bound:
        name = b["target_group"]
        group = add("group", name, {"DisplayName": name, "Description": tag})
        usernames = sorted(
            users[uid]["username"] for uid in member_ids if uid in in_scope
        )
        for username in usernames:
            add(
                "membership",
                name + "/" + username,
                {"group": name, "user": username},
                [group, "user:" + username],
            )
        for a in resolved:
            add(
                "assignment",
                a["account_id"] + "/" + a["permission_set_arn"] + "/" + name,
                {**a, "group": name},
                [group],
            )
        p["mapping_rows"].append(
            {
                "source": b["source"],
                "target_group": name,
                "review_ref": b["review_ref"],
                "members": usernames,
                "assignments": resolved,
            }
        )
    for r in resources:
        try:
            observed = targets.read(r)
        except IdentityError as error:
            r["planned_action"] = "unknown"
            p["blockers"].append({"code": error.code, "resource_id": r["id"]})
            continue
        if observed is None:
            r["planned_action"] = "create"
        elif observed == expected(r):
            r["planned_action"] = "exists"
        else:
            r["planned_action"] = "conflict"
            p["conflicts"].append(
                {"resource_id": r["id"], "code": conflict_code(r, observed)}
            )
    by_username = {u["username"]: u for u in in_scope.values()}
    for r in resources:
        if r["kind"] == "user":
            u = by_username[r["name"]]
            p["user_rows"].append(
                {
                    "username": u["username"],
                    "email": u["email"],
                    "federated": u["federated"],
                    "action": r["planned_action"],
                    "onboarding": ONBOARDING,
                }
            )
    p["exclusions"].extend(
        {"kind": "feature", "name": name, "reason": reason}
        for name, reason in FEATURE_EXCLUSIONS
    )
    p["resources"] = resources
    p["status"] = "blocked" if p["blockers"] or p["conflicts"] else "planned"
    p["plan_hash"] = digest(p)
    return p


def validate_plan(plan, config, *, allow_blocked=False):
    require(
        isinstance(plan, dict)
        and plan.get("kind") == "identity_export_plan"
        and plan.get("schema_version") == 1,
        "INVALID_PLAN",
    )
    require(
        plan.get("plan_hash")
        == digest({k: v for k, v in plan.items() if k != "plan_hash"}),
        "PLAN_HASH_MISMATCH",
    )
    require(plan["config_hash"] == digest(config), "CONFIG_CHANGED")
    require(
        allow_blocked
        or (
            not plan["blockers"]
            and not plan["conflicts"]
            and plan["status"] == "planned"
        ),
        "PLAN_BLOCKED",
    )
    ids = [r["id"] for r in plan["resources"]]
    require(len(ids) == len(set(ids)), "DUPLICATE_RESOURCE")
    accounts = set(config["target"]["account_ids"])
    for r in plan["resources"]:
        require(r.get("kind") in RESOURCE_KINDS, "UNSUPPORTED_RESOURCE_KIND")
        if r["kind"] == "group":
            require(r["payload"].get("Description") == owner(config), "GROUP_OWNER_TAG")
        if r["kind"] == "assignment":
            require(r["payload"].get("account_id") in accounts, "ACCOUNT_OUT_OF_SCOPE")
    return copy.deepcopy(plan)
