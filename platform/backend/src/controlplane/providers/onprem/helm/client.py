"""Pinned Helm 3 subprocess boundary. Caller journals intent and holds an app lock.

Writes are submitted once. A timeout/nonzero exit can follow a partial apply and
is always uncertain. Reconcile Helm history and Kubernetes state before retrying.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderRejected,
    ProviderUnavailable,
    SubmissionUncertain,
)
from controlplane.providers.onprem.connection import connect
from controlplane.providers.onprem.helm.models import (
    ChartArtifact,
    HelmPlan,
    HelmReceipt,
    HelmRelease,
    is_deployed_at,
)
from controlplane.providers.onprem.helm.package import load_chart_package
from controlplane.providers.onprem.helm.plan import validate_plan

HELM_NAMESPACE = "oneport-apps"
HELM_VERSION = "v3.22.0"
RELEASE_NAME_PATTERN = re.compile(r"op-[a-z0-9-]*")
MAX_RELEASE_NAME = 40
HISTORY_MAX = "10"
DATA_CREDENTIAL_KEYS = (
    "postgres-admin-password",
    "postgres-app-password",
    "redis-password",
    "DATABASE_URL",
    "REDIS_URL",
)


class HelmClient:
    def __init__(
        self,
        *,
        binary: Path,
        kubeconfig: Path,
        context: str,
        expected_server: str,
        artifact: ChartArtifact,
        runner=subprocess.run,
    ):
        if not binary.is_absolute() or not kubeconfig.is_absolute() or not context:
            raise ValueError("Explicit absolute Helm/kubeconfig paths and context are required")
        self.binary, self.kubeconfig, self.context = binary, kubeconfig, context
        self.artifact, self.runner = artifact, runner
        self.expected_server = expected_server

    def _run(self, args: list[str], *, write: bool = False, timeout: int = 40) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("HELM_")}
        command = [
            str(self.binary),
            "--kubeconfig",
            str(self.kubeconfig),
            "--kube-context",
            self.context,
            "--namespace",
            HELM_NAMESPACE,
            *args,
        ]
        error = SubmissionUncertain if write else ProviderUnavailable
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=timeout, env=env)
        except (subprocess.TimeoutExpired, OSError):
            raise error("Helm result unavailable; reconcile before retrying a write") from None
        if result.returncode:
            raise error("Helm operation did not complete; reconcile release state before retrying")
        return result.stdout

    def verify_version(self) -> None:
        if self._run(["version", "--template", "{{.Version}}"]).strip() != HELM_VERSION:
            raise ProviderRejected("The reviewed charts require Helm v3.22.0")

    def load_package(self) -> bytes:
        return load_chart_package(self.artifact)

    def current(self, release: str) -> HelmRelease | None:
        if len(release) > MAX_RELEASE_NAME or not RELEASE_NAME_PATTERN.fullmatch(release):
            raise ValueError("Invalid Helm release name")
        try:
            rows = json.loads(
                self._run(["list", "--all", "--filter", "^" + release + "$", "--output", "json"])
            )
            if not rows:
                return None
            row = rows[0]
            if len(rows) != 1 or row["name"] != release or row["namespace"] != HELM_NAMESPACE:
                raise ValueError()
            return HelmRelease(revision=int(row["revision"]), status=row["status"])
        except (ValueError, KeyError, TypeError):
            raise ProviderUnavailable("Invalid Helm release observation") from None

    def uninstall(self, release: str) -> None:
        if self.current(release) is None:
            return
        self._run(["uninstall", release, "--timeout", "120s"], write=True, timeout=150)

    def values(self, release: str, revision: int):
        self.current(release)
        if revision < 1:
            raise ValueError("Invalid Helm revision")
        try:
            return json.loads(
                self._run(
                    ["get", "values", release, "--revision", str(revision), "--output", "json"]
                )
            )
        except ValueError:
            raise ProviderUnavailable("Invalid Helm values observation") from None

    def _secret_refs(self, plan: HelmPlan) -> list[dict]:
        if self.artifact.name != "oneport-data":
            return plan.values.get("secretBindings", [])
        return [
            {
                "name": plan.values["credentialsSecret"],
                "uid": plan.values["credentialsSecretUid"],
                "key": key,
            }
            for key in DATA_CREDENTIAL_KEYS
        ]

    @staticmethod
    def _verify_secret(clients, ref: dict, plan: HelmPlan) -> None:
        try:
            secret = clients.core.read_namespaced_secret(
                ref["name"], HELM_NAMESPACE, _request_timeout=(3, 15)
            )
        except Exception:
            raise ProviderRejected("Registered Secret cannot be verified") from None
        labels = secret.metadata.labels or {}
        if (
            secret.metadata.uid != ref["uid"]
            or secret.immutable is not True
            or ref["key"] not in (secret.data or {})
            or any(
                labels.get("launchpad.io/" + k) != plan.values[k]
                for k in ("application", "environment")
            )
        ):
            raise ProviderConflict(
                "Secret UID, key or ownership differs from the registered binding"
            )

    def _check_cluster(self, plan):
        clients = connect(
            kubeconfig=self.kubeconfig, context=self.context, expected_server=self.expected_server
        )
        try:
            for ref in self._secret_refs(plan):
                self._verify_secret(clients, ref, plan)
        finally:
            clients.close()

    def _verify_existing_release(self, plan: HelmPlan, expected_revision: int | None) -> bool:
        before = self.values(plan.release, expected_revision)
        for key in ("application", "environment"):
            if before.get(key) != plan.values[key]:
                raise ProviderConflict(
                    "Helm release belongs to a different application/environment"
                )
        if self.artifact.name == "oneport-data":
            if before != plan.values:
                raise ProviderConflict("Data configuration changed; explicit migration required")
            return True
        if before.get("strategy", {}).get("type") != plan.strategy_type:
            if self.artifact.version != "0.3.0" or any(
                before.get(k) != plan.values.get(k) for k in ("image", "releaseId")
            ):
                raise ProviderConflict(
                    "Strategy migration must retain the verified application version"
                )
        if before.get("route", {}).get("hostname") != plan.values["route"]["hostname"]:
            raise ProviderConflict("Public hostname changed after registration")
        return False

    def _upgrade(self, plan: HelmPlan, payload: bytes) -> None:
        with tempfile.TemporaryDirectory(prefix="oneport-helm-") as directory:
            chart = Path(directory) / "chart.tgz"
            values = Path(directory) / "values.json"
            chart.write_bytes(payload)
            values.write_text(json.dumps(plan.values))
            chart.chmod(0o600)
            values.chmod(0o600)
            self._run(
                [
                    "upgrade",
                    "--install",
                    plan.release,
                    str(chart),
                    "--values",
                    str(values),
                    "--reset-values",
                    "--history-max",
                    HISTORY_MAX,
                    "--timeout",
                    "180s",
                ],
                write=True,
                timeout=210,
            )

    def apply(self, plan: HelmPlan, *, expected_revision: int | None) -> HelmReceipt:
        validate_plan(plan)
        if plan.chart_sha256 != self.artifact.sha256:
            raise ProviderConflict("Plan uses a different chart artifact")
        payload = self.load_package()
        self._check_cluster(plan)
        self.verify_version()
        current = self.current(plan.release)
        if current is None:
            if expected_revision is not None or plan.values.get("route", {}).get("enabled", False):
                raise ProviderConflict("First install must keep the public route disabled")
        else:
            if not current.deployed_at(expected_revision):
                raise ProviderConflict("Helm release changed or has an unresolved operation")
            if self._verify_existing_release(plan, expected_revision):
                return self.observe_receipt(plan, expected_revision)
        self._upgrade(plan, payload)
        return self.observe_receipt(plan, (expected_revision or 0) + 1)

    def observe_receipt(self, plan: HelmPlan, revision: int) -> HelmReceipt:
        try:
            matches = (
                is_deployed_at(self.current(plan.release), revision)
                and self.values(plan.release, revision) == plan.values
            )
        except ProviderUnavailable:
            raise SubmissionUncertain(
                "Helm write returned but its result could not be observed"
            ) from None
        if not matches:
            raise SubmissionUncertain("Helm post-write state differs from saved intent")
        return HelmReceipt(
            release=plan.release,
            revision=revision,
            chart_sha256=plan.chart_sha256,
            values_fingerprint=plan.fingerprint,
        )

    def test(self, receipt: HelmReceipt):
        if not is_deployed_at(self.current(receipt.release), receipt.revision):
            raise ProviderConflict("Helm revision changed before candidate test")
        # Creates test Jobs; this is a write. Never add --logs (Helm 3 Job hook bug).
        self._run(["test", receipt.release, "--timeout", "120s"], write=True, timeout=150)

    def rollback(
        self, target: HelmPlan, receipt: HelmReceipt, *, expected_revision: int
    ) -> HelmReceipt:
        if self.artifact.name != "oneport-app":
            raise ProviderRejected("Data releases must not follow application rollback")
        validate_plan(target)
        if (receipt.release, receipt.chart_sha256, receipt.values_fingerprint) != (
            target.release,
            target.chart_sha256,
            target.fingerprint,
        ) or receipt.revision >= expected_revision:
            raise ProviderConflict(
                "Rollback target does not match the recorded application revision"
            )
        if target.chart_sha256 != self.artifact.sha256:
            raise ProviderConflict("Rollback chart differs from the reviewed artifact")
        self.load_package()
        self._check_cluster(target)
        self.verify_version()
        if not is_deployed_at(self.current(target.release), expected_revision):
            raise ProviderConflict("Helm release changed or has an unresolved operation")
        before = self.values(target.release, expected_revision)
        if any(before.get(k) != target.values[k] for k in ("application", "environment")):
            raise ProviderConflict("Cannot roll back a foreign application")
        if self.values(target.release, receipt.revision) != target.values:
            raise ProviderConflict(
                "Historical Helm values differ from the recorded rollback target"
            )
        self._run(
            [
                "rollback",
                target.release,
                str(receipt.revision),
                "--history-max",
                HISTORY_MAX,
                "--timeout",
                "180s",
            ],
            write=True,
            timeout=210,
        )
        return self.observe_receipt(target, expected_revision + 1)
