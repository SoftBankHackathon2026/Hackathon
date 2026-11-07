from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from migration.secrets.common import SecretsError, canonical, load, save
from migration.secrets.state import State
from migration.secrets_export import __main__ as cli
from migration.secrets_export.common import validate_config
from migration.secrets_export.engine import report, run
from migration.secrets_export.plan import binding_id, build_plan
from migration.secrets_export.source import OpenBaoSource
from migration.secrets_export.target import SecretsManagerTarget

ACCOUNT = "000000000000"
REGION = "ap-northeast-2"
CLUSTER = "cluster-test"
DB_PATH = "apps/orders/database"
API_PATH = "apps/orders/config/api"
FLAG_PATH = "apps/orders/config/flags"
SOURCE = {
    DB_PATH: {"username": "orders_app", "password": "canary-db-value-7f3a"},
    API_PATH: {"token": "canary-api-value-91c2"},
    FLAG_PATH: {"mode": "canary-flag-value-44d0"},
}
STALE_API = {"token": "canary-stale-value-0b8e"}
CANARIES = (
    "canary-db-value-7f3a",
    "canary-api-value-91c2",
    "canary-flag-value-44d0",
    "canary-stale-value-0b8e",
)
EXAMPLE = Path(__file__).resolve().parents[1] / "secrets-examples"


def name_of(path):
    return "onprem/" + path


def arn_of(name):
    return (
        "arn:aws:secretsmanager:"
        + REGION
        + ":"
        + ACCOUNT
        + ":secret:"
        + name
        + "-AbC123"
    )


def leak_count(*blobs):
    needles = []
    for canary in CANARIES:
        needles.append(canary.encode())
        needles.append(base64.b64encode(canary.encode()))
    for value in (*SOURCE.values(), STALE_API):
        raw = canonical(value).encode()
        needles.append(hashlib.sha256(raw).hexdigest().encode())
        needles.append(base64.b64encode(raw))
    total = 0
    for blob in blobs:
        data = blob if isinstance(blob, bytes) else str(blob).encode()
        total += sum(needle in data for needle in needles)
    return total


def assert_no_values(*blobs):
    hits = leak_count(*blobs)
    assert hits == 0, "secret value material leaked"


def private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


class ClientError(Exception):
    def __init__(self, code, message="provider failure"):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeBao:
    def __init__(self):
        self.secrets = {}
        self.deleted = set()
        self.caps = {}
        self.requests = []

    def put(self, path, data):
        version = self.secrets.get(path, (None, 0))[1] + 1
        self.secrets[path] = (copy.deepcopy(data), version)

    def listing(self, folder):
        names = set()
        for path in self.secrets:
            if path.startswith(folder + "/"):
                head, sep, _ = path[len(folder) + 1 :].partition("/")
                names.add(head + "/" if sep else head)
        if not names:
            raise SecretsError("TARGET_NOT_FOUND")
        return {"data": {"keys": sorted(names)}}

    def request(self, method, path, payload=None):
        self.requests.append((method, path))
        if path == "sys/health?standbyok=true":
            return {
                "initialized": True,
                "sealed": False,
                "cluster_id": CLUSTER,
                "version": "2.7.2",
            }
        if path == "sys/internal/ui/mounts/kv":
            return {
                "data": {
                    "type": "kv",
                    "options": {"version": "2"},
                    "path": "kv/",
                    "accessor": "kv_test",
                }
            }
        if path == "auth/token/lookup-self":
            return {"data": {"policies": ["export-read"], "ttl": 600}}
        if path == "sys/capabilities-self":
            caps = {}
            for p in payload["paths"]:
                if p.startswith("kv/data/"):
                    caps[p] = ["read"]
                elif p.startswith("kv/metadata/"):
                    caps[p] = ["list", "read"]
                else:
                    caps[p] = ["deny"]
            caps.update(self.caps)
            return {"data": caps}
        if path.startswith("kv/metadata/") and path.endswith("?list=true"):
            return self.listing(path[len("kv/metadata/") : -len("?list=true")])
        if method == "GET" and path.startswith("kv/data/"):
            key = path[len("kv/data/") :]
            if key in self.deleted:
                raise SecretsError("TARGET_VERSION_DELETED")
            if key not in self.secrets:
                raise SecretsError("TARGET_NOT_FOUND")
            data, version = self.secrets[key]
            return {
                "data": {
                    "data": copy.deepcopy(data),
                    "metadata": {
                        "version": version,
                        "deletion_time": "",
                        "destroyed": False,
                    },
                }
            }
        raise AssertionError("unexpected OpenBao request")


class FakeSTS:
    def __init__(self, account=ACCOUNT):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


class FakeSM:
    def __init__(self):
        self.secrets = {}
        self.calls = []
        self.lose_response = set()
        self.kms = {}

    def add(self, name, value, version, tags=None, **flags):
        self.secrets[name] = {
            "name": name,
            "arn": arn_of(name),
            "versions": {version: value},
            "current": version,
            "tags": dict(tags or {}),
            "flags": flags,
        }

    def find(self, secret_id):
        for secret in self.secrets.values():
            if secret_id in (secret["name"], secret["arn"]):
                return secret
        raise ClientError("ResourceNotFoundException")

    def lost(self, operation):
        if operation in self.lose_response:
            self.lose_response.discard(operation)
            raise ClientError("ServiceUnavailable")

    def describe_secret(self, SecretId):
        self.calls.append("describe_secret")
        s = self.find(SecretId)
        return {
            "ARN": s["arn"],
            "Name": s["name"],
            "VersionIdsToStages": {
                v: ["AWSCURRENT"] if v == s["current"] else ["AWSPREVIOUS"]
                for v in s["versions"]
            },
            "Tags": [{"Key": k, "Value": v} for k, v in s["tags"].items()],
            **s["flags"],
        }

    def get_secret_value(self, SecretId, VersionStage):
        self.calls.append("get_secret_value")
        assert VersionStage == "AWSCURRENT"
        s = self.find(SecretId)
        return {
            "ARN": s["arn"],
            "VersionId": s["current"],
            "SecretString": s["versions"][s["current"]],
        }

    def create_secret(
        self, Name, SecretString, ClientRequestToken, Description, Tags, KmsKeyId=None
    ):
        self.calls.append("create_secret")
        if Name in self.secrets:
            s = self.secrets[Name]
            if s["versions"].get(ClientRequestToken) != SecretString:
                raise ClientError("ResourceExistsException")
        else:
            tags = {t["Key"]: t["Value"] for t in Tags}
            self.add(Name, SecretString, ClientRequestToken, tags)
            self.kms[Name] = KmsKeyId
        self.lost("create_secret")
        return {"ARN": arn_of(Name), "Name": Name, "VersionId": ClientRequestToken}

    def put_secret_value(self, SecretId, SecretString, ClientRequestToken):
        self.calls.append("put_secret_value")
        s = self.find(SecretId)
        if ClientRequestToken in s["versions"]:
            if s["versions"][ClientRequestToken] != SecretString:
                raise ClientError("ResourceExistsException")
        else:
            s["versions"][ClientRequestToken] = SecretString
            s["current"] = ClientRequestToken
        self.lost("put_secret_value")
        return {"ARN": s["arn"], "Name": s["name"], "VersionId": ClientRequestToken}

    def tag_resource(self, SecretId, Tags):
        self.calls.append("tag_resource")
        self.find(SecretId)["tags"].update({t["Key"]: t["Value"] for t in Tags})
        return {}

    def writes(self):
        return [c for c in self.calls if c in {"create_secret", "put_secret_value"}]


def make_config(tmp_path, **target):
    token = tmp_path / "bao.token"
    key = tmp_path / "hash.key"
    private(token, "placeholder-readonly-token\n")
    private(key, "0" * 64 + "\n")
    return validate_config(
        {
            "schema_version": 1,
            "migration_id": "orders-export",
            "source": {
                "endpoint": "https://openbao.test:8200",
                "expected_version": "2.7.2",
                "cluster_id": CLUSTER,
                "mount": "kv",
                "paths": [DB_PATH],
                "prefixes": ["apps/orders/config"],
                "ca_file": str(tmp_path / "ca.pem"),
                "token_ref": "file:" + str(token),
            },
            "target": {
                "account_id": ACCOUNT,
                "region": REGION,
                "name_template": "onprem/{path}",
                "kms_key_id": "alias/example-export",
                **target,
            },
            "hash_key_ref": "file:" + str(key),
        }
    )


def owned_tags(config, path):
    name = name_of(path)
    return {
        "secrets-export:binding": binding_id(
            config, {"cluster_id": CLUSTER}, path, name
        )
    }


@pytest.fixture
def env(tmp_path):
    config = make_config(tmp_path)
    bao = FakeBao()
    for path, data in SOURCE.items():
        bao.put(path, data)
    sm = FakeSM()
    sm.add(
        name_of(API_PATH),
        canonical(STALE_API),
        "existing-api-version-0001",
        owned_tags(config, API_PATH),
    )
    sm.add(
        name_of(FLAG_PATH),
        canonical(SOURCE[FLAG_PATH]),
        "existing-flag-version-0001",
        owned_tags(config, FLAG_PATH),
    )
    source = OpenBaoSource(config["source"], http=bao)
    target = SecretsManagerTarget(config["target"], sm=sm, sts=FakeSTS())
    return SimpleNamespace(
        config=config,
        bao=bao,
        sm=sm,
        source=source,
        target=target,
        state=tmp_path / "export.sqlite3",
        directory=tmp_path,
    )


def rows_by_path(plan):
    return {r["source"]["path"]: r for r in plan["rows"]}


def apply(env, plan, verify_only=False):
    with State(env.state) as state:
        return run(
            env.config, plan, env.source, env.target, state, verify_only=verify_only
        )


def stored(env, path):
    s = env.sm.secrets[name_of(path)]
    return s["versions"][s["current"]]


def test_plan_classifies_create_update_unchanged_without_writes(env):
    plan = build_plan(env.config, env.source, env.target)
    rows = rows_by_path(plan)
    assert plan["status"] == "planned" and not plan["blockers"]
    assert {p: r["action"] for p, r in rows.items()} == {
        DB_PATH: "create",
        API_PATH: "update",
        FLAG_PATH: "unchanged",
    }
    assert plan["summary"] == {"create": 1, "update": 1, "unchanged": 1, "blocked": 0}
    assert rows[DB_PATH]["target"]["name"] == "onprem/" + DB_PATH
    assert rows[FLAG_PATH]["source_value_hash"] == rows[FLAG_PATH]["target_value_hash"]
    assert rows[API_PATH]["source_value_hash"] != rows[API_PATH]["target_value_hash"]
    assert env.sm.writes() == [] and "tag_resource" not in env.sm.calls
    assert all(m == "GET" or p == "sys/capabilities-self" for m, p in env.bao.requests)
    assert plan["value_payloads_in_plan"] is False
    assert_no_values(json.dumps(plan))


def test_value_hash_is_keyed_and_not_a_plain_digest(env, tmp_path):
    first = build_plan(env.config, env.source, env.target)
    other = tmp_path / "other"
    other.mkdir()
    second_config = make_config(other)
    private(other / "hash.key", "1" * 64 + "\n")
    second = build_plan(second_config, env.source, env.target)
    a, b = rows_by_path(first)[DB_PATH], rows_by_path(second)[DB_PATH]
    assert a["source_value_hash"] != b["source_value_hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", a["source_value_hash"])


def test_plan_blocks_unowned_existing_secret_unless_adopted(env, tmp_path):
    env.sm.add(name_of(DB_PATH), canonical({"other": "x"}), "foreign-version-0001")
    blocked = build_plan(env.config, env.source, env.target)
    assert blocked["status"] == "blocked"
    assert {"path": DB_PATH, "code": "TARGET_EXISTS_NOT_OWNED"} in blocked["blockers"]
    adopted_dir = tmp_path / "adopt"
    adopted_dir.mkdir()
    config = make_config(adopted_dir, adopt_existing=True)
    target = SecretsManagerTarget(config["target"], sm=env.sm, sts=FakeSTS())
    adopted = build_plan(config, env.source, target)
    row = rows_by_path(adopted)[DB_PATH]
    assert adopted["status"] == "planned"
    assert row["action"] == "update" and row["ownership"] == "adopted"


@pytest.mark.parametrize(
    "flags, code",
    [
        ({"DeletedDate": "2026-11-01T00:00:00Z"}, "TARGET_SCHEDULED_FOR_DELETION"),
        ({"RotationEnabled": True}, "TARGET_ROTATION_ENABLED"),
        ({"OwningService": "rds"}, "TARGET_MANAGED_BY_SERVICE"),
    ],
)
def test_plan_blocks_unsafe_targets(env, flags, code):
    env.sm.secrets[name_of(API_PATH)]["flags"] = flags
    plan = build_plan(env.config, env.source, env.target)
    assert {"path": API_PATH, "code": code} in plan["blockers"]
    assert rows_by_path(plan)[API_PATH]["action"] == "blocked"


def test_plan_blocks_deleted_or_missing_source(env):
    env.bao.deleted.add(API_PATH)
    del env.bao.secrets[DB_PATH]
    plan = build_plan(env.config, env.source, env.target)
    assert {"path": API_PATH, "code": "SOURCE_VERSION_DELETED"} in plan["blockers"]
    assert {"path": DB_PATH, "code": "SOURCE_NOT_FOUND"} in plan["blockers"]
    assert plan["status"] == "blocked"


@pytest.mark.parametrize(
    "caps, code",
    [
        (["read", "update"], "SOURCE_TOKEN_NOT_READ_ONLY"),
        (["list"], "SOURCE_PERMISSION_MISSING"),
    ],
)
def test_source_token_must_be_read_only(env, caps, code):
    env.bao.caps = {"kv/data/" + DB_PATH: caps}
    with pytest.raises(SecretsError, match=code):
        build_plan(env.config, env.source, env.target)


def test_source_token_with_lifecycle_rights_is_rejected(env):
    env.bao.caps = {"kv/delete/" + DB_PATH: ["update"]}
    with pytest.raises(SecretsError, match="SOURCE_TOKEN_NOT_READ_ONLY"):
        build_plan(env.config, env.source, env.target)


def test_source_errors_are_reported_as_source_codes(env):
    env.bao.secrets.clear()
    env.bao.caps = {}
    with pytest.raises(SecretsError, match="SOURCE_NOT_FOUND"):
        env.source.read(DB_PATH)


def test_target_account_mismatch_is_rejected(env):
    env.target.sts = FakeSTS("999999999999")
    with pytest.raises(SecretsError, match="TARGET_ACCOUNT_MISMATCH"):
        build_plan(env.config, env.source, env.target)


def test_apply_creates_puts_skips_unchanged_and_is_idempotent(env):
    plan = build_plan(env.config, env.source, env.target)
    result = apply(env, plan)
    rows = rows_by_path(plan)
    assert result["copy_status"] == "verified"
    assert sorted(env.sm.writes()) == ["create_secret", "put_secret_value"]
    db_ok = stored(env, DB_PATH) == canonical(SOURCE[DB_PATH])
    api_ok = stored(env, API_PATH) == canonical(SOURCE[API_PATH])
    assert db_ok and api_ok
    flag = env.sm.secrets[name_of(FLAG_PATH)]
    assert flag["current"] == "existing-flag-version-0001"
    assert len(flag["versions"]) == 1
    assert env.sm.kms[name_of(DB_PATH)] == "alias/example-export"
    tags = env.sm.secrets[name_of(DB_PATH)]["tags"]
    assert tags["secrets-export:source-path"] == DB_PATH
    assert tags["secrets-export:source-version"] == "1"
    assert tags["secrets-export:binding"] == rows[DB_PATH]["binding_id"]
    api_tags = env.sm.secrets[name_of(API_PATH)]["tags"]
    assert api_tags["secrets-export:source-path"] == API_PATH
    assert_no_values(json.dumps(tags), json.dumps(api_tags))
    before = list(env.sm.writes())
    again = apply(env, plan)
    assert again["copy_status"] == "verified"
    assert env.sm.writes() == before
    assert again["operation_counts"]["complete"] == 3


def test_resume_after_lost_create_response_reuses_request_token(env):
    plan = build_plan(env.config, env.source, env.target)
    env.sm.lose_response.add("create_secret")
    with pytest.raises(SecretsError, match="TARGET_UNAVAILABLE"):
        apply(env, plan)
    with State(env.state) as state:
        assert state.status()["job"]["stage"] == "needs_recovery"
        assert state.status()["operations"]["unknown"] == 1
    result = apply(env, plan)
    assert result["copy_status"] == "verified"
    assert len(env.sm.secrets[name_of(DB_PATH)]["versions"]) == 1


def test_resume_after_lost_put_response_does_not_duplicate_version(env):
    plan = build_plan(env.config, env.source, env.target)
    env.sm.lose_response.add("put_secret_value")
    with pytest.raises(SecretsError):
        apply(env, plan)
    assert apply(env, plan)["copy_status"] == "verified"
    assert len(env.sm.secrets[name_of(API_PATH)]["versions"]) == 2


def test_external_change_after_plan_blocks_write(env):
    plan = build_plan(env.config, env.source, env.target)
    env.sm.put_secret_value(
        SecretId=name_of(API_PATH),
        SecretString=canonical({"token": "changed"}),
        ClientRequestToken="external-version-0002",
    )
    env.sm.calls.clear()
    with pytest.raises(SecretsError, match="TARGET_CHANGED_REPLAN_REQUIRED"):
        apply(env, plan)
    assert "put_secret_value" not in env.sm.calls


def test_target_created_outside_plan_is_not_overwritten(env):
    plan = build_plan(env.config, env.source, env.target)
    env.sm.add(name_of(DB_PATH), canonical({"x": "y"}), "foreign-version-0001")
    with pytest.raises(SecretsError, match="TARGET_CREATED_OUTSIDE_PLAN"):
        apply(env, plan)
    assert "create_secret" not in env.sm.calls


def test_source_change_after_plan_requires_replan(env):
    plan = build_plan(env.config, env.source, env.target)
    env.bao.put(DB_PATH, {"username": "orders_app", "password": "rotated"})
    with pytest.raises(SecretsError, match="SOURCE_CHANGED_REPLAN_REQUIRED"):
        apply(env, plan)
    assert env.sm.writes() == []


def test_verify_compares_value_hash_of_target(env):
    plan = build_plan(env.config, env.source, env.target)
    apply(env, plan)
    assert apply(env, plan, verify_only=True)["copy_status"] == "verified"
    secret = env.sm.secrets[name_of(DB_PATH)]
    secret["versions"][secret["current"]] = canonical({"tampered": "value"})
    with pytest.raises(SecretsError, match="VALUE_VERIFICATION_FAILED"):
        apply(env, plan, verify_only=True)
    with State(env.state) as state:
        assert report(state)["stage"] == "needs_recovery"


def test_verify_never_writes_missing_target(env):
    plan = build_plan(env.config, env.source, env.target)
    apply(env, plan)
    del env.sm.secrets[name_of(DB_PATH)]
    env.sm.calls.clear()
    with pytest.raises(SecretsError, match="TARGET_CHANGED_OR_NOT_EXPORTED"):
        apply(env, plan, verify_only=True)
    assert env.sm.writes() == []


def test_changed_hash_key_invalidates_plan(env):
    plan = build_plan(env.config, env.source, env.target)
    private(Path(env.config["hash_key_ref"][5:]), "2" * 64 + "\n")
    with pytest.raises(SecretsError, match="HASH_KEY_CHANGED"):
        apply(env, plan)


def test_plan_artifact_tampering_is_rejected(env):
    plan = build_plan(env.config, env.source, env.target)
    plan["rows"][0]["action"] = "unchanged"
    with pytest.raises(SecretsError, match="ARTIFACT_CHANGED"):
        apply(env, plan)


def test_cli_flow_outputs_and_artifacts_contain_no_values(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "OpenBaoSource", lambda config: env.source)
    monkeypatch.setattr(cli, "SecretsManagerTarget", lambda config: env.target)
    config = str(env.directory / "config.yaml")
    plan_path = str(env.directory / "plan.json")
    state = str(env.state)
    save(config, env.config)
    assert cli.main(["validate", "--config", config]) == 0
    assert cli.main(["plan", "--config", config]) == 2
    assert cli.main(["plan", "--config", config, "--out", plan_path]) == 0
    assert not os.stat(plan_path).st_mode & 0o077
    plan = load(plan_path)
    common = ["--config", config, "--plan", plan_path, "--state", state]
    assert cli.main(["apply", *common, "--confirm-plan", "0" * 64]) == 2
    assert cli.main(["apply", *common, "--confirm-plan", plan["plan_hash"]]) == 0
    assert cli.main(["verify", *common]) == 0
    assert cli.main(["status", "--config", config, "--state", state]) == 0
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[1] == {"error": "PRIVATE_OUTPUT_FILE_REQUIRED"}
    assert lines[3] == {"error": "CONFIRMED_PLAN_MISMATCH"}
    assert lines[-1]["copy_status"] == "verified"
    assert not os.stat(state).st_mode & 0o077
    assert_no_values(out, Path(plan_path).read_bytes(), Path(state).read_bytes())


def test_cli_hides_provider_error_text(env, monkeypatch, capsys):
    def boom(**kwargs):
        raise ClientError("ThrottlingException", "canary-db-value-7f3a")

    env.sm.describe_secret = boom
    monkeypatch.setattr(cli, "OpenBaoSource", lambda config: env.source)
    monkeypatch.setattr(cli, "SecretsManagerTarget", lambda config: env.target)
    config = str(env.directory / "config.yaml")
    save(config, env.config)
    code = cli.main(
        ["plan", "--config", config, "--out", str(env.directory / "plan.json")]
    )
    out = capsys.readouterr().out
    assert code == 2 and json.loads(out) == {"error": "TARGET_UNAVAILABLE"}
    assert_no_values(out)


def test_cli_blocked_plan_exits_3(env, monkeypatch, capsys):
    env.sm.add(name_of(DB_PATH), canonical({"x": "y"}), "foreign-version-0001")
    monkeypatch.setattr(cli, "OpenBaoSource", lambda config: env.source)
    monkeypatch.setattr(cli, "SecretsManagerTarget", lambda config: env.target)
    config = str(env.directory / "config.yaml")
    save(config, env.config)
    out = str(env.directory / "plan.json")
    assert cli.main(["plan", "--config", config, "--out", out]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda c: c["source"].update(token_ref="env:BAO_TOKEN"), "FILE_CREDENTIAL"),
        (lambda c: c["source"].update(token_ref="inline-token"), "FILE_CREDENTIAL"),
        (lambda c: c["source"].update(extra=True), "UNKNOWN_FIELD"),
        (lambda c: c["source"].update(paths=[], prefixes=[]), "EXPLICIT_SELECTION"),
        (lambda c: c["source"].update(mount="sys"), "DEDICATED_KV_MOUNT"),
        (lambda c: c["target"].update(name_template="onprem/"), "NAME_TEMPLATE"),
        (lambda c: c["target"].update(name_template="{x}/{path}"), "NAME_TEMPLATE"),
        (lambda c: c["target"].update(account_id="12345"), "ACCOUNT_ID"),
        (lambda c: c["target"].update(adopt_existing="yes"), "ADOPT_EXISTING"),
        (lambda c: c.update(hash_key_ref=c["source"]["token_ref"]), "HASH_KEY_MUST"),
    ],
)
def test_config_is_strict(env, mutate, code):
    config = copy.deepcopy(env.config)
    mutate(config)
    with pytest.raises(SecretsError, match=code):
        validate_config(config)


def test_hash_key_must_be_long(env):
    private(Path(env.config["hash_key_ref"][5:]), "short-key\n")
    with pytest.raises(SecretsError, match="HASH_KEY_TOO_SHORT"):
        build_plan(env.config, env.source, env.target)


def test_example_config_uses_placeholders_and_matches_schema():
    text = (EXAMPLE / "export.config.example.yaml").read_text()
    assert not re.search(r"\d{12}", text)
    config = load(EXAMPLE / "export.config.example.yaml", private=False)
    config["source"]["endpoint"] = "https://openbao.test:8200"
    config["target"]["account_id"] = ACCOUNT
    config["target"]["kms_key_id"] = "alias/example-export"
    assert validate_config(config)["target"]["name_template"] == "onprem/{path}"
