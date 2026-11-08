from .checks import CHECKS, ERROR, FAIL, PASS, AwsError, actions, aws
from .config import GROUPS


def selected(config):
    return [name for name in GROUPS if name in config]


def plan(config):
    groups = [
        {
            "id": name,
            "required": config[name]["required"],
            "actions": actions(name, config[name]),
        }
        for name in selected(config)
    ]
    return {
        "status": "planned",
        "region": config["region"],
        "groups": groups,
        "actions": sorted(
            {"sts:GetCallerIdentity"}
            | {action for group in groups for action in group["actions"]}
        ),
    }


def open_session(config):
    try:
        import boto3
        from botocore.exceptions import BotoCoreError
    except ImportError:
        raise AwsError("INSTALL_POLICY_RUNTIME_EXTRA") from None
    try:
        return boto3.Session(
            profile_name=config.get("profile"), region_name=config["region"]
        )
    except BotoCoreError:
        raise AwsError("AWS_PROFILE_UNAVAILABLE") from None


def client_factory(session, config):
    from botocore.config import Config

    options = Config(
        retries={"mode": "standard", "total_max_attempts": 3},
        connect_timeout=10,
        read_timeout=30,
        ignore_configured_endpoint_urls=True,
    )
    cache = {}

    def clients(service, region=None):
        key = service, region or config["region"]
        if key not in cache:
            cache[key] = session.client(service, region_name=key[1], config=options)
        return cache[key]

    return clients


def outcome(items):
    statuses = {item["status"] for item in items}
    if FAIL in statuses:
        return FAIL
    if ERROR in statuses or not statuses:
        return ERROR
    return PASS


def codes(groups):
    return sorted(
        {
            item["code"]
            for group in groups
            for item in group["items"]
            if item["status"] != PASS
        }
    )


def check(config, session=None):
    try:
        session = session or open_session(config)
        clients = client_factory(session, config)
        caller = aws(clients("sts").get_caller_identity)
    except AwsError as error:
        return {"status": "blocked", "reason_codes": [error.code]}
    if caller.get("Account") != config["account_id"]:
        return {"status": "blocked", "reason_codes": ["AWS_ACCOUNT_MISMATCH"]}
    groups = []
    for name in selected(config):
        section = config[name]
        items = CHECKS[name](clients, section, config)
        groups.append(
            {
                "id": name,
                "required": section["required"],
                "status": outcome(items),
                "items": items,
            }
        )
    required = [group for group in groups if group["required"]]
    optional = [group for group in groups if not group["required"]]
    if all(group["status"] == PASS for group in required):
        status = "ready"
    elif any(group["status"] == FAIL for group in required):
        status = "not_ready"
    else:
        status = "unverified"
    return {
        "status": status,
        "account_verified": True,
        "region": config["region"],
        "groups": groups,
        "reason_codes": codes(required),
        "optional_reason_codes": codes(optional),
    }


def run(operation, config, session=None):
    if operation == "plan":
        return plan(config)
    return check(config, session)
