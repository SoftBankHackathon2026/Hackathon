from __future__ import annotations

from migration.secrets.common import (
    SecretsError,
    check_artifact,
    digest,
    now,
    require,
    seal_artifact,
)

from .common import (
    BINDING_TAG,
    PATH_LIMIT,
    TAG_PREFIX,
    hash_key,
    key_check,
    render_name,
    same_hash,
    value_hash,
)
from .source import ROW_BLOCKING_CODES

ACTIONS = ("create", "update", "unchanged", "blocked")


def binding_id(config, observed, path, name):
    return digest(
        {
            "source": {
                "cluster_id": observed["cluster_id"],
                "mount": config["source"]["mount"],
                "path": path,
            },
            "target": {
                "account_id": config["target"]["account_id"],
                "region": config["target"]["region"],
                "name": name,
            },
        }
    )


def desired_tags(plan, row):
    return {
        TAG_PREFIX + "migration-id": plan["migration_id"],
        BINDING_TAG: row["binding_id"],
        TAG_PREFIX + "source-mount": row["source"]["mount"],
        TAG_PREFIX + "source-path": row["source"]["path"],
        TAG_PREFIX + "source-version": str(row["source"]["version"]),
    }


def existing_blockers(existing, owned, adopt):
    flags = (
        (existing["scheduled_deletion"], "TARGET_SCHEDULED_FOR_DELETION"),
        (existing["managed_by_service"], "TARGET_MANAGED_BY_SERVICE"),
        (existing["rotation_enabled"], "TARGET_ROTATION_ENABLED"),
        (existing["pending"], "TARGET_ROTATION_PENDING"),
        (existing["current_version"] is None, "TARGET_CURRENT_VERSION_MISSING"),
        (not owned and not adopt, "TARGET_EXISTS_NOT_OWNED"),
    )
    return [code for flag, code in flags if flag]


def plan_row(config, key, observed, path, source, target):
    name = render_name(config["target"]["name_template"], path)
    value, version = source.read(path)
    row = {
        "binding_id": binding_id(config, observed, path, name),
        "source": {
            "mount": config["source"]["mount"],
            "path": path,
            "version": version,
        },
        "target": {"name": name, "arn": None, "version_id": None},
        "action": "create",
        "ownership": "new",
        "source_value_hash": value_hash(key, value),
        "target_value_hash": None,
        "blockers": [],
    }
    existing = target.describe(name)
    if existing is None:
        return row
    owned = existing["tags"].get(BINDING_TAG) == row["binding_id"]
    adopt = config["target"].get("adopt_existing", False)
    row["ownership"] = "owned" if owned else "adopted" if adopt else "unowned"
    row["target"]["arn"] = existing["arn"]
    row["blockers"] = existing_blockers(existing, owned, adopt)
    if existing["current_version"] and not existing["scheduled_deletion"]:
        version_id, current = target.current_value(existing["arn"])
        require(version_id == existing["current_version"], "TARGET_CHANGED_DURING_PLAN")
        row["target"]["version_id"] = version_id
        row["target_value_hash"] = value_hash(key, current)
    if row["blockers"]:
        row["action"] = "blocked"
    elif same_hash(row["target_value_hash"], row["source_value_hash"]):
        row["action"] = "unchanged"
    else:
        row["action"] = "update"
    return row


def build_plan(config, source, target):
    key = hash_key(config)
    observed = source.identity()
    paths, blockers = source.discover()
    if paths:
        source.check_read_only(paths)
    target_identity = target.identity()
    rows = []
    for path in paths:
        if len(path) > PATH_LIMIT:
            blockers.append({"path": path, "code": "SOURCE_PATH_TOO_LONG"})
            continue
        try:
            row = plan_row(config, key, observed, path, source, target)
        except SecretsError as error:
            if error.code not in ROW_BLOCKING_CODES | {"TARGET_NAME_INVALID"}:
                raise
            blockers.append({"path": path, "code": error.code})
            continue
        blockers.extend({"path": path, "code": code} for code in row["blockers"])
        rows.append(row)
    if not paths and not blockers:
        blockers.append({"code": "SOURCE_SELECTION_EMPTY"})
    require(
        len({r["target"]["name"] for r in rows}) == len(rows), "TARGET_NAME_COLLISION"
    )
    plan = {
        "schema_version": 1,
        "kind": "secret_export_plan",
        "migration_id": config["migration_id"],
        "created_at": now(),
        "config_hash": digest(config),
        "hash_algorithm": "hmac-sha256",
        "hash_key_check": key_check(key),
        "source": {"endpoint": config["source"]["endpoint"], "observed": observed},
        "target": {
            "identity": target_identity,
            "account_id": config["target"]["account_id"],
            "region": config["target"]["region"],
            "kms_key_id": config["target"].get("kms_key_id"),
        },
        "rows": rows,
        "summary": {a: sum(r["action"] == a for r in rows) for a in ACTIONS},
        "blockers": blockers,
        "value_payloads_in_plan": False,
        "rotation_status": "not_supported_by_this_tool",
        "application_status": "external_handoff_required",
        "status": "blocked" if blockers else "planned",
    }
    return seal_artifact(plan, "plan_hash")


def validate_plan(config, plan, key):
    check_artifact(plan, "secret_export_plan", "plan_hash")
    require(
        plan.get("status") == "planned" and not plan["blockers"] and plan["rows"],
        "PLAN_BLOCKED",
    )
    require(plan["config_hash"] == digest(config), "CONFIG_CHANGED")
    require(same_hash(plan["hash_key_check"], key_check(key)), "HASH_KEY_CHANGED")
    require(
        all(r["action"] in {"create", "update", "unchanged"} for r in plan["rows"]),
        "PLAN_BLOCKED",
    )
