import re

import yaml

from ..wazuh_infra.config import boolean, keys, local_file, require, text

OPERATIONS = ("plan", "check")
GROUPS = ("registry", "secret_store", "identity", "audit_log", "data_target")
CORE = GROUPS[:4]
ACCOUNT = re.compile(r"[0-9]{12}")
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+")
PROFILE = re.compile(r"[A-Za-z0-9_.+@-]{1,64}")
REPOSITORY = re.compile(
    r"(?=.{2,256}\Z)(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*"
)
KMS_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/_+=,.@-]{0,2047}")
INSTANCE = re.compile(r"arn:aws[a-z-]*:sso:::instance/[A-Za-z0-9.-]{1,64}")
PERMISSION_SET = re.compile(r"[A-Za-z0-9_+=,.@-]{1,32}")
RDS = re.compile(r"[A-Za-z](?:-?[A-Za-z0-9]){0,62}")
EKS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
ENCRYPTION = ("AES256", "KMS", "KMS_DSSE")


def names(value, field, pattern, limit=100):
    require(isinstance(value, list) and len(value) <= limit, field)
    for name in value:
        text(name, field, pattern)
    require(len(set(value)) == len(value), field + ".duplicate")
    return value


def group(config, name, allowed):
    section = config.get(name)
    keys(section, "required " + allowed, name)
    section["required"] = boolean(section.get("required", True), name + ".required")
    return section


def load(path):
    local_file(path, "config")
    with open(path, encoding="utf-8") as stream:
        config = yaml.safe_load(stream.read(1024 * 1024))
    keys(
        config, "schema_version account_id region profile " + " ".join(GROUPS), "config"
    )
    require(
        type(config.get("schema_version")) is int and config["schema_version"] == 1,
        "schema_version",
    )
    text(config.get("account_id"), "account_id", ACCOUNT)
    text(config.get("region"), "region", REGION)
    if "profile" in config:
        text(config["profile"], "profile", PROFILE)
    for name in CORE:
        require(name in config, name)

    registry = group(config, "registry", "repositories scan_on_push encryption")
    names(registry.setdefault("repositories", []), "registry.repositories", REPOSITORY)
    boolean(registry.setdefault("scan_on_push", False), "registry.scan_on_push")
    if "encryption" in registry:
        require(registry["encryption"] in ENCRYPTION, "registry.encryption")
    require(
        registry["repositories"]
        or not (registry["scan_on_push"] or "encryption" in registry),
        "registry.repositories",
    )

    secret_store = group(config, "secret_store", "kms_key")
    text(secret_store.get("kms_key"), "secret_store.kms_key", KMS_KEY)

    identity = group(config, "identity", "region instance_arn permission_sets")
    text(identity.setdefault("region", config["region"]), "identity.region", REGION)
    if "instance_arn" in identity:
        text(identity["instance_arn"], "identity.instance_arn", INSTANCE)
    names(
        identity.setdefault("permission_sets", []),
        "identity.permission_sets",
        PERMISSION_SET,
    )

    audit_log = group(config, "audit_log", "multi_region")
    boolean(audit_log.setdefault("multi_region", True), "audit_log.multi_region")

    if "data_target" in config:
        data = group(config, "data_target", "rds_instance approved_regions eks_cluster")
        require("rds_instance" in data or "eks_cluster" in data, "data_target")
        if "rds_instance" in data:
            text(data["rds_instance"], "data_target.rds_instance", RDS)
        if "eks_cluster" in data:
            text(data["eks_cluster"], "data_target.eks_cluster", EKS)
        if "approved_regions" in data:
            require("rds_instance" in data, "data_target.approved_regions")
            names(data["approved_regions"], "data_target.approved_regions", REGION, 50)
            require(data["approved_regions"], "data_target.approved_regions")
    return config
