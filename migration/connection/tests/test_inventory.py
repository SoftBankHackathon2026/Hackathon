"""Inventory boundary tests; real Kubernetes is opt-in and read-only."""

from __future__ import annotations

import base64
import copy
import json
import os
import sys
import unittest
from unittest.mock import patch

from migration_connection.inventory import (
    InventoryError,
    Kubectl,
    collect,
    discover,
    inventory_digest,
    validate_report,
)
from migration_connection.inventory.collector import decode_json, run_bounded
from migration_connection.inventory.schema import ReportError, canonical_json

SECRET = "DO-NOT-RETURN-customer-credential"
CA = base64.b64encode(
    b"-----BEGIN CERTIFICATE-----\npublic-fixture\n-----END CERTIFICATE-----\n"
).decode()
BINDING = {"request_id": "request-1", "device_id": "device-1", "epoch": 2}


class FakeKubectl:
    def __init__(self):
        self.commands = []
        self.contexts = ["prod", "other"]
        self.trust = {
            "server": "https://kube.example:6443",
            "certificate-authority-data": CA,
        }
        self.denied = set()
        self.change_after_reads = False

    def __call__(self, command):
        self.commands.append(command)
        if "config" in command:
            if "--output=jsonpath={.contexts}" in command:
                return json.dumps(
                    [
                        {
                            "name": name,
                            "context": {"cluster": name, "user": "local-user"},
                        }
                        for name in self.contexts
                    ]
                ).encode()
            trust = dict(self.trust)
            if "--context=other" in command:
                trust["server"] = "https://other.example:6443"
            if self.change_after_reads and any("get" in c for c in self.commands):
                trust["server"] = "https://changed.example:6443"
            return json.dumps(trust).encode()
        if "--context=prod" not in command:
            raise AssertionError("An unselected context was contacted")
        if "version" in command:
            return json.dumps(
                {
                    "serverVersion": {"gitVersion": "v1.31.0", "extra": SECRET},
                    "clientVersion": {"credential": SECRET},
                }
            ).encode()
        resource = command[command.index("get") + 1]
        if resource in self.denied:
            raise InventoryError("KUBECTL_REQUEST_FAILED")
        if resource == "namespace":
            result = {
                "metadata": {"uid": "uid-cluster", "annotations": {"sensitive": SECRET}}
            }
        elif resource == "nodes":
            result = {
                "items": [
                    {
                        "metadata": {
                            "name": "node-1",
                            "annotations": {"sensitive": SECRET},
                        }
                    }
                ]
            }
        elif resource == "namespaces":
            result = {
                "items": [
                    {"metadata": {"name": "default"}},
                    {"metadata": {"name": "kube-system"}},
                ]
            }
        elif resource == "storageclasses.storage.k8s.io":
            result = {
                "items": [
                    {
                        "metadata": {
                            "name": "fast",
                            "annotations": {
                                "storageclass.kubernetes.io/is-default-class": "true",
                                "sensitive": SECRET,
                            },
                        },
                        "provisioner": "csi.example",
                        "parameters": {"secret": SECRET},
                    }
                ]
            }
        else:
            raise AssertionError("Unexpected resource: " + resource)
        return json.dumps(result).encode()


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeKubectl()
        self.client = Kubectl(runner=self.runner, executable="/usr/local/bin/kubectl")

    def selected(self):
        return next(
            x["id"]
            for x in discover(self.client, **BINDING)["discovered"]
            if x["name"] == "prod"
        )

    def report(self):
        return collect(
            self.client,
            selected_target_id=self.selected(),
            clock=lambda: 1700000000,
            **BINDING,
        )

    def test_discovery_only_reads_public_local_config(self):
        result = discover(self.client, **BINDING)
        self.assertEqual([x["name"] for x in result["discovered"]], ["other", "prod"])
        self.assertTrue(
            all("config" in c and "get" not in c for c in self.runner.commands)
        )
        self.assertTrue(
            all(
                "--output=jsonpath={.contexts}" in c
                or "--output=jsonpath={.clusters[0].cluster}" in c
                for c in self.runner.commands
            )
        )

    def test_selection_and_output_allowlist(self):
        report = self.report()
        output = json.dumps(report)
        self.assertNotIn(SECRET, output)
        self.assertNotIn("annotations", output)
        self.assertNotIn("parameters", output)
        self.assertEqual(report["environment"]["identity"], "kubernetes:uid-cluster")
        self.assertEqual(report["resources"]["node_count"], 1)
        self.assertEqual(report["resources"]["namespace_count"], 2)
        remote = [c for c in self.runner.commands if "get" in c or "version" in c]
        self.assertTrue(remote)
        self.assertTrue(all("--context=prod" in c for c in remote))
        self.assertFalse(
            any(
                any(
                    x in c
                    for x in [
                        "apply",
                        "create",
                        "delete",
                        "patch",
                        "secrets",
                        "configmaps",
                    ]
                )
                for c in remote
            )
        )

    def test_missing_target_does_not_contact_cluster(self):
        with self.assertRaisesRegex(
            InventoryError, "SELECTED_TARGET_CHANGED_OR_MISSING"
        ):
            collect(self.client, selected_target_id="k8s-" + "0" * 48, **BINDING)
        self.assertTrue(all("config" in c for c in self.runner.commands))

    def test_trust_change_requires_new_selection(self):
        selected = self.selected()
        self.runner.trust["server"] = "https://replacement.example:6443"
        with self.assertRaisesRegex(
            InventoryError, "SELECTED_TARGET_CHANGED_OR_MISSING"
        ):
            collect(self.client, selected_target_id=selected, **BINDING)
        self.assertTrue(all("config" in c for c in self.runner.commands))

    def test_trust_change_during_collection_rejects_result(self):
        selected = self.selected()
        self.runner.change_after_reads = True
        with self.assertRaisesRegex(
            InventoryError, "SELECTED_TARGET_CHANGED_OR_MISSING"
        ):
            collect(self.client, selected_target_id=selected, **BINDING)

    def test_insecure_proxy_and_credential_urls_rejected(self):
        for replacement in (
            {"insecure-skip-tls-verify": True},
            {"proxy-url": "https://user:secret@proxy.example"},
            {"server": "http://kube.example"},
            {"server": "https://user:secret@kube.example"},
        ):
            with self.subTest(replacement=replacement):
                original = self.runner.trust.copy()
                self.runner.trust.update(replacement)
                with self.assertRaisesRegex(
                    InventoryError, "UNSUPPORTED_CONTEXT_CONFIGURATION"
                ):
                    discover(self.client, **BINDING)
                self.runner.trust = original

    def test_partial_permissions_are_unknown_not_zero(self):
        self.runner.denied = {"nodes", "storageclasses.storage.k8s.io"}
        report = self.report()
        self.assertIsNone(report["resources"]["node_count"])
        self.assertIsNone(report["resources"]["storage_classes"])
        self.assertEqual(report["coverage"]["nodes"], "unavailable")
        self.assertEqual(report["environment"]["facts"][1]["value"], "미확인")
        self.assertEqual(report["coverage"]["namespaces"], "complete")

    def test_required_identity_access_failure_blocks(self):
        self.runner.denied = {"namespace"}
        with self.assertRaises(InventoryError):
            self.report()

    def test_server_report_binding_and_payload_tampering(self):
        report = self.report()
        validate_report(
            report,
            expected_request_id="request-1",
            expected_device_id="device-1",
            expected_epoch=2,
            expected_target_id=report["target_id"],
        )
        for binding in (
            {"expected_request_id": "other"},
            {"expected_device_id": "other"},
            {"expected_epoch": 3},
            {"expected_target_id": "other"},
        ):
            with self.subTest(binding=binding), self.assertRaises(ReportError):
                validate_report(report, **binding)
        changed = copy.deepcopy(report)
        changed["environment"]["facts"].append({"label": "token", "value": SECRET})
        with self.assertRaises(ReportError):
            validate_report(changed)
        changed = copy.deepcopy(report)
        changed["cluster"]["server"] = "https://replacement.example"
        with self.assertRaisesRegex(ReportError, "REPORT_TARGET_CHANGED"):
            validate_report(changed)
        changed = copy.deepcopy(report)
        changed["cluster"]["secret"] = SECRET
        with self.assertRaises(ReportError):
            validate_report(changed)

    def test_digest_order_independence_and_integer_time(self):
        report = self.report()
        self.assertEqual(
            inventory_digest(report),
            inventory_digest(dict(reversed(list(report.items())))),
        )
        changed = copy.deepcopy(report)
        changed["epoch"] = 3
        self.assertNotEqual(inventory_digest(report), inventory_digest(changed))
        report["collected_at_ms"] = 1.5
        with self.assertRaises(ReportError):
            validate_report(report)

    def test_json_and_canonical_limits(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b"not-json"):
            with self.subTest(raw=raw), self.assertRaises(InventoryError):
                decode_json(raw)
        for value in ({"x": 1.5}, {"x": 2**60}, {"x": "\ud800"}, {"한글키": 1}):
            with self.subTest(value=value), self.assertRaises(ReportError):
                canonical_json(value)

    def test_process_timeout_is_bounded(self):
        with self.assertRaisesRegex(InventoryError, "KUBECTL_TIMEOUT"):
            run_bounded(
                [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1
            )

    def test_process_output_limit_and_stderr_redaction(self):
        with (
            patch("migration_connection.inventory.collector.MAX_OUTPUT", 32),
            self.assertRaisesRegex(InventoryError, "KUBECTL_RESPONSE_TOO_LARGE"),
        ):
            run_bounded([sys.executable, "-c", "print('x'*1024)"])
        with self.assertRaises(InventoryError) as failure:
            run_bounded(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('" + SECRET + "', file=sys.stderr); sys.exit(1)",
                ]
            )
        self.assertNotIn(SECRET, str(failure.exception))


@unittest.skipUnless(
    os.environ.get("CONNECTION_INVENTORY_LIVE_CONTEXT")
    and os.environ.get("CONNECTION_INVENTORY_LIVE_KUBECONFIG"),
    "explicit read-only test cluster configuration required",
)
class LiveInventoryTests(unittest.TestCase):
    def test_selected_context_read_only(self):
        client = Kubectl(kubeconfig=os.environ["CONNECTION_INVENTORY_LIVE_KUBECONFIG"])
        candidates = discover(client, **BINDING)["discovered"]
        self.assertEqual(
            len(candidates),
            1,
            "use an isolated test kubeconfig with exactly the approved context",
        )
        selected = candidates[0]
        self.assertEqual(
            selected["context"], os.environ["CONNECTION_INVENTORY_LIVE_CONTEXT"]
        )
        report = collect(client, selected_target_id=selected["id"], **BINDING)
        validate_report(report, expected_target_id=selected["id"])
        self.assertTrue(report["cluster"]["namespace_uid"])


if __name__ == "__main__":
    unittest.main()
