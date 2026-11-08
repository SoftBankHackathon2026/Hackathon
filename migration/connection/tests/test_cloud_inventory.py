"""Cloud discovery executes bounded, non-interactive read operations."""

import json
import sys

import pytest
from migration_connection.inventory.cloud import (
    Cloud,
    classify_failure,
    collect_cloud,
    discover_cloud,
)
from migration_connection.inventory.collector import InventoryError, run_bounded
from test_inventory import BINDING, CA


class LocalCli:
    def __init__(self, provider):
        self.provider = provider
        self.commands = []

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        assert kwargs["timeout"] <= 7
        command = argv[1:3]
        if command == ["sts", "get-caller-identity"]:
            return b'{"Account":"123456789012","Arn":"must-not-be-reported"}'
        if command == ["configure", "get"]:
            return b"ap-northeast-2\n"
        if command == ["eks", "list-clusters"]:
            return b'{"clusters":["allowed","denied"]}'
        if command == ["eks", "describe-cluster"]:
            name = argv[argv.index("--name") + 1]
            if name == "denied":
                raise InventoryError("PERMISSION_DENIED")
            result = {
                "cluster": {
                    "arn": "arn:aws:eks:ap-northeast-2:123456789012:cluster/allowed",
                    "endpoint": "https://private.example.test",
                    "certificateAuthority": {"data": CA},
                }
            }
        elif command == ["auth", "list"]:
            result = [{"account": "local@example.test", "status": "ACTIVE"}]
        elif command == ["config", "get-value"]:
            return b"available-project\n"
        elif command == ["projects", "list"]:
            result = [
                {"projectId": "available-project"},
                {"projectId": "restricted-project"},
            ]
        elif command == ["container", "clusters"]:
            project = argv[argv.index("--project") + 1]
            if project == "restricted-project":
                raise InventoryError("PERMISSION_DENIED")
            result = {
                "name": "allowed",
                "location": "asia-northeast3",
                "endpoint": "192.0.2.15",
                "masterAuth": {"clusterCaCertificate": CA},
            }
            if argv[3] == "list":
                result = [result]
        else:
            raise AssertionError(argv)
        return json.dumps(result).encode()


@pytest.mark.parametrize(
    ("provider", "restricted_scope"),
    [
        ("aws", "123456789012/ap-northeast-2/denied"),
        ("gcp", "restricted-project"),
    ],
)
def test_partial_permissions_keep_verified_candidates(
    monkeypatch, provider, restricted_scope
):
    cli = LocalCli(provider)
    monkeypatch.setattr(
        "migration_connection.inventory.cloud.shutil.which", lambda name: "/cli/" + name
    )
    monkeypatch.setattr(
        "migration_connection.inventory.cloud.Cloud",
        lambda name: Cloud(name, runner=cli),
    )
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")
    result = discover_cloud(provider)
    assert [(item["name"], item["kind"]) for item in result["discovered"]] == [
        ("allowed", provider)
    ]
    assert result["warnings"] == [
        {"code": "PERMISSION_DENIED", "scope": restricted_scope}
    ]
    assert "local@example.test" not in str(
        result
    ) and "must-not-be-reported" not in str(result)
    assert all(
        ("--no-cli-auto-prompt" if provider == "aws" else "--quiet") in call
        for call in cli.commands
    )
    assert any("describe-cluster" in cmd or "describe" in cmd for cmd in cli.commands)


def test_missing_cli_reports_empty_without_failing_discovery(monkeypatch):
    monkeypatch.setattr(
        "migration_connection.inventory.cloud.shutil.which", lambda _: None
    )
    assert discover_cloud("gcp") == {
        "discovered": [],
        "warnings": [{"code": "CLI_MISSING", "scope": ""}],
    }


def test_command_error_is_classified_without_disclosing_raw_output():
    with pytest.raises(InventoryError, match="^PERMISSION_DENIED$"):
        run_bounded(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('AccessDenied sample-token'); sys.exit(1)",
            ],
            classify_failure=classify_failure,
        )


def test_collect_refuses_a_changed_cluster_before_accessing_kubernetes(monkeypatch):
    cli = LocalCli("aws")
    monkeypatch.setattr(
        "migration_connection.inventory.cloud.shutil.which", lambda name: "/cli/" + name
    )
    monkeypatch.setattr(
        "migration_connection.inventory.cloud.Cloud",
        lambda name: Cloud(name, runner=cli),
    )
    with pytest.raises(InventoryError, match="SELECTED_TARGET_CHANGED_OR_MISSING"):
        collect_cloud(
            "aws", "123456789012/ap-northeast-2/allowed", "old-target-id", **BINDING
        )


@pytest.mark.parametrize(
    ("provider", "context", "plugin"),
    [
        ("aws", "123456789012/ap-northeast-2/allowed", "aws"),
        ("gcp", "available-project/asia-northeast3/allowed", "gke-gcloud-auth-plugin"),
    ],
)
def test_collection_uses_temporary_config_bound_to_discovered_cluster(
    monkeypatch, provider, context, plugin
):
    from pathlib import Path

    from migration_connection.inventory import Kubectl, validate_report
    from test_inventory import FakeKubectl

    cli = LocalCli(provider)
    monkeypatch.setattr(
        "migration_connection.inventory.cloud.shutil.which", lambda name: "/cli/" + name
    )
    monkeypatch.setattr(
        "migration_connection.inventory.cloud.Cloud",
        lambda name: Cloud(name, runner=cli),
    )
    candidate, endpoint, _ = Cloud(provider, runner=cli).describe(context)
    paths = []

    def temporary_client(*, kubeconfig):
        path = Path(kubeconfig)
        paths.append(path)
        assert path.stat().st_mode & 0o777 == 0o600
        config = json.loads(path.read_text())
        assert config["current-context"] == context
        auth = config["users"][0]["user"]["exec"]
        assert (
            auth["command"] == "/cli/" + plugin and auth["interactiveMode"] == "Never"
        )
        assert config["clusters"][0]["cluster"]["server"] == endpoint
        fake = FakeKubectl()
        fake.contexts = [context]
        fake.trust["server"] = endpoint

        def run(argv):
            return fake(
                [
                    "--context=prod" if item == "--context=" + context else item
                    for item in argv
                ]
            )

        return Kubectl(kubeconfig=kubeconfig, executable="/cli/kubectl", runner=run)

    monkeypatch.setattr(
        "migration_connection.inventory.cloud.Kubectl", temporary_client
    )
    report = collect_cloud(provider, context, candidate["id"], **BINDING)
    assert validate_report(report)["environment"]["kind"] == provider
    assert report["environment"]["identity"] == context
    assert paths and not paths[0].exists()
