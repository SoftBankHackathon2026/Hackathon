"""Register the reviewed demo environments on the existing Oneport server.

No app deployment or new cloud resources; preserve the existing database and AWS settings.
"""

import copy
import grp
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from controlplane.domain.models import ImageSource
from controlplane.execution.gcp.settings import GcpSettings
from controlplane.execution.onprem.settings import ArgoSettings
from controlplane.providers.loader import PlatformConfig
from controlplane.providers.onprem.helm.journal import retained_bindings
from controlplane.providers.onprem.helm.settings import HelmSettings
from controlplane.repositories.releases import ReleaseStore

BASE = Path("/etc/oneport")
SECRET_DIR = BASE / "secrets"
SERVICES = [
    "oneport-api",
    "oneport-aws-worker",
    "oneport-gcp-worker",
    "oneport-onprem-worker",
    "oneport-helm-worker",
    "oneport-helm-canary-worker",
]


def prepare(bundle, aws, manifest):
    """Pure validation: no writes and no cloud API calls."""
    original = PlatformConfig.model_validate(bundle)
    if set(manifest) != {"aws_demo_image", "gcp", "onprem", "helm", "helm_canary"}:
        raise ValueError("unexpected manifest fields")
    ImageSource(image=manifest["aws_demo_image"])
    if not manifest["aws_demo_image"].startswith("docker.io/"):
        raise ValueError("public Docker Hub demo image required")
    dsn = original.database_url.get_secret_value()
    configs = {}
    for name, model in [
        ("gcp", GcpSettings),
        ("onprem", ArgoSettings),
        ("helm", HelmSettings),
        ("helm_canary", HelmSettings),
    ]:
        item = dict(manifest[name])
        if any(k in item for k in ["database_url", "database_url_file", "kubeconfig"]):
            raise ValueError(
                "credentials may not be supplied in the deployment manifest"
            )
        item["database_url"] = dsn  # validate now; persist a file reference below
        if name in {"onprem", "helm", "helm_canary"}:
            item["kubeconfig"] = str(SECRET_DIR / "oneport-platform.kubeconfig")
        model.model_validate(item)
        del item["database_url"]
        item["database_url_file"] = str(SECRET_DIR / "database-url")
        configs[name] = item
    updated_aws = dict(aws)
    updated_aws.setdefault("catalog_target_id", "aws-main")
    updated_aws["allowed_images"] = list(
        dict.fromkeys([manifest["aws_demo_image"], *aws.get("allowed_images", [])])
    )
    entries = []
    for entry in original.providers:
        if entry.runtime == "aws_ecs":
            if str(entry.config_file) not in {
                "aws-runtime.json",
                str(BASE / "aws-runtime.json"),
            }:
                raise ValueError(
                    "unexpected AWS runtime path; preserve it through an explicit migration"
                )
            entries.append(entry.model_dump(mode="json"))
        elif str(entry.config_file) not in {
            "gcp-runtime.json",
            "onprem-runtime.json",
            "helm-runtime.json",
            "helm-canary-runtime.json",
            str(BASE / "gcp-runtime.json"),
            str(BASE / "onprem-runtime.json"),
            str(BASE / "helm-runtime.json"),
            str(BASE / "helm-canary-runtime.json"),
        }:
            raise ValueError(
                "unrecognized existing provider; refusing to overwrite registration"
            )
    if len(entries) != 1:
        raise ValueError("exactly one existing AWS environment expected")
    entries += [
        {"runtime": "gcp_cloud_run", "config_file": "gcp-runtime.json"},
        {
            "runtime": "onprem_argo",
            "config_file": "onprem-runtime.json",
            "listed": False,
        },
        {"runtime": "onprem_helm", "config_file": "helm-runtime.json"},
        # Retain the old verification binding/worker for history and in-flight recovery.
        # Canary is a strategy of the on-prem target, not a separate environment.
        {
            "runtime": "onprem_helm",
            "config_file": "helm-canary-runtime.json",
            "listed": False,
        },
    ]
    updated_bundle = {**bundle, "providers": entries}
    return updated_bundle, updated_aws, configs, dsn


def preflight_gcp(config):
    from google.cloud import run_v2
    from google.oauth2 import service_account

    # Only the service-account JSON installed by the separate credentials step is used.
    creds = service_account.Credentials.from_service_account_file(
        SECRET_DIR / "gcp-credentials.json"
    )
    gcp = config["environment"]
    name = f"projects/{gcp['project_id']}/locations/{gcp['region']}/services/{gcp['service']}"
    client = run_v2.ServicesClient(credentials=creds)
    try:
        service = client.get_service(name=name, retry=None, timeout=10)
        # Resource name + UID identify the service. Its public service_url may
        # route through our load balancer instead of the default run.app URI.
        if service.name != name or service.uid != gcp["service_uid"]:
            raise ValueError("registered GCP service identity changed")
    finally:
        client.transport.close()


def preflight(configs, dsn):
    from controlplane.execution.onprem.doctor import diagnose
    from controlplane.providers.onprem.models import ArgoDeploymentRequest
    from controlplane.providers.public_images import verify_public_image

    verify_public_image(configs["onprem"]["allowed_images"][0])
    preflight_gcp(configs["gcp"])
    settings = {**configs["onprem"]}
    settings.pop("database_url_file")
    settings["database_url"] = dsn
    settings = ArgoSettings.model_validate(settings)
    request = ArgoDeploymentRequest(
        application=settings.environment.application,
        environment_id=settings.environment.environment_id,
        source={"kind": "image", "image": settings.allowed_images[0]},
        cpu="0.5",
        memory_mib=512,
        replicas=1,
    )
    result = diagnose(settings, request)
    if not result["checks_passed"]:
        failed = [check["name"] for check in result["checks"] if not check["passed"]]
        print("On-prem checks failed: " + ", ".join(failed))
        raise ValueError("on-prem prerequisites not met")


def preflight_helm(config, dsn):
    from controlplane.providers.onprem.helm.runtime import HelmRuntime

    item = dict(config)
    item.pop("database_url_file")
    item["database_url"] = dsn
    settings = HelmSettings.model_validate(item)
    settings.validate_request(settings.demo_request())
    runtime = HelmRuntime(settings)
    try:
        runtime.connect()
        runtime.verify_hostname()
    finally:
        runtime.close()


def reviewed_helm_upgrade(config, previous, dsn):
    """Approve a chart/telemetry-only transition without discarding the verified journal.

    The previous configuration is read from the operator-owned server file, never
    an HTTP request. Application identity, cluster, data chart and resource policy
    must stay identical. Old packages stay in the release bundle for rollback.
    """
    result = copy.deepcopy(config)

    def model(value):
        value = dict(value)
        value.pop("database_url_file", None)
        value["database_url"] = dsn
        return HelmSettings.model_validate(value)

    old = model(previous)
    result["previous_bindings"] = [
        b.model_dump(mode="json") for b in old.previous_bindings
    ]
    new = model(result)
    if new.fingerprint() == old.fingerprint():
        return result, False
    changed = {"app_chart", "chart_source_commit", "observability", "previous_bindings"}
    if old.model_dump(exclude=changed) != new.model_dump(exclude=changed):
        raise ValueError(
            "Helm upgrade changed cluster, app, data or policy; explicit migration required"
        )
    if (
        old.app_chart.version not in {"0.1.1", "0.2.0", "0.3.0"}
        or new.app_chart.version not in {"0.2.0", "0.3.0"}
    ):
        raise ValueError("Unreviewed Helm chart transition")
    # Activation holds the intake/worker barrier: use the same journal roots as
    # the worker, retaining rollback and data reuse without accumulating history.
    with ReleaseStore(dsn).connect() as conn:
        result["previous_bindings"] = [
            b.model_dump(mode="json") for b in retained_bindings(conn, old)
        ]
    validated = model(result)
    for predecessor in validated.previous_bindings:
        from controlplane.providers.onprem.helm.client import HelmClient

        HelmClient(
            binary=new.binary,
            kubeconfig=new.kubeconfig,
            context=new.context,
            expected_server=new.server,
            artifact=predecessor.app_chart,
        ).load_package()
    return result, True


def upgrade_catalog_bindings(base, dsn):
    from psycopg.types.json import Jsonb

    originals = []
    with ReleaseStore(dsn).connect() as conn:
        rows = conn.execute(
            "SELECT e.id,e.binding FROM app_environments e JOIN deployment_targets t ON t.id=e.target_id WHERE t.provider_binding_id=%s AND e.binding IS NOT NULL FOR UPDATE OF e",
            (base["environment"]["environment_id"],),
        ).fetchall()
        for row in rows:
            previous = copy.deepcopy(row["binding"])
            previous.update(kubeconfig=base["kubeconfig"], binary=base["binary"])
            updated = copy.deepcopy(previous)
            for key in ("app_chart", "chart_source_commit", "observability"):
                updated[key] = base[key]
            upgraded, changed = reviewed_helm_upgrade(updated, previous, dsn)
            if changed:
                originals.append((row["id"], row["binding"]))
                require_idle_helm(upgraded, dsn)
                for key in ("database_url", "database_url_file", "kubeconfig", "binary"):
                    upgraded.pop(key, None)
                conn.execute("UPDATE app_environments SET binding=%s WHERE id=%s", (Jsonb(upgraded), row["id"]))

    return originals


def require_idle_helm(config, dsn):
    import psycopg

    env = config["environment"]
    with psycopg.connect(dsn) as conn:
        active = conn.execute(
            "SELECT 1 FROM releases WHERE runtime='onprem_helm' AND application=%s "
            "AND environment_id=%s AND state NOT IN ('succeeded','failed') LIMIT 1",
            (env["application"], env["environment_id"]),
        ).fetchone()
    if active:
        raise ValueError(
            "Wait for the active Helm deployment before upgrading its binding"
        )


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".oneport-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o640)
            os.fchown(stream.fileno(), 0, grp.getgrnam("oneport").gr_gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def command(*args):
    subprocess.run(
        args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def wait_for_api():
    for _ in range(30):
        try:
            with urlopen("http://127.0.0.1:8320/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("API did not recover")


def activate():
    # Serialize configuration changes with both API intake and complete worker
    # ticks, including rollback/restarts. New API processes can become healthy
    # under this lock, but cannot accept deployments until the switch completes.
    bundle = PlatformConfig.model_validate_json((BASE / "platform.json").read_text())
    dsn = bundle.database_url.get_secret_value()
    with ReleaseStore(dsn).configuration_lock():
        _activate(dsn)


def _activate(locked_dsn):
    bundle = json.loads((BASE / "platform.json").read_text())
    aws = json.loads((BASE / "aws-runtime.json").read_text())
    manifest = json.loads(
        (
            Path(__file__).parents[1] / "deployment/provider-environments.json"
        ).read_text()
    )
    bundle, aws, configs, dsn = prepare(bundle, aws, manifest)
    if dsn != locked_dsn:
        raise ValueError("Platform database changed during provider activation")
    helm_changed = False
    for name, filename in (("helm", "helm-runtime.json"), ("helm_canary", "helm-canary-runtime.json")):
        previous_helm = BASE / filename
        if previous_helm.exists():
            configs[name], changed = reviewed_helm_upgrade(
                configs[name], json.loads(previous_helm.read_text()), dsn
            )
            helm_changed = helm_changed or changed
    preflight(configs, dsn)
    preflight_helm(configs["helm"], dsn)
    preflight_helm(configs["helm_canary"], dsn)
    files = {
        BASE / "platform.json": json.dumps(bundle).encode(),
        BASE / "aws-runtime.json": json.dumps(aws).encode(),
        BASE / "gcp-runtime.json": json.dumps(configs["gcp"]).encode(),
        BASE / "onprem-runtime.json": json.dumps(configs["onprem"]).encode(),
        BASE / "helm-runtime.json": json.dumps(configs["helm"]).encode(),
        BASE / "helm-canary-runtime.json": json.dumps(configs["helm_canary"]).encode(),
        SECRET_DIR / "database-url": dsn.encode(),
    }
    before = {path: path.read_bytes() if path.exists() else None for path in files}
    previous_active = {
        name: subprocess.run(
            ["systemctl", "is-active", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
        for name in SERVICES[2:]
    }
    previous_enabled = {
        name: subprocess.run(
            ["systemctl", "is-enabled", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
        for name in SERVICES[2:]
    }
    catalog_before = []
    try:
        if helm_changed:
            # The running release may predate the DB switch lock. Drain/stop it
            # before the idle check; restart only with the final configuration.
            command(
                "systemctl",
                "stop",
                "oneport-api",
                *[
                    name
                    for name in SERVICES
                    if "helm" in name and previous_active.get(name)
                ],
            )
            require_idle_helm(configs["helm"], dsn)
            require_idle_helm(configs["helm_canary"], dsn)
        command(
            "install",
            "-m",
            "644",
            str(Path(__file__).parent / "systemd/oneport-helm-worker.service"),
            "/etc/systemd/system/oneport-helm-worker.service",
        )
        command(
            "install",
            "-m",
            "644",
            str(Path(__file__).parent / "systemd/oneport-helm-canary-worker.service"),
            "/etc/systemd/system/oneport-helm-canary-worker.service",
        )
        command("systemctl", "daemon-reload")
        for path, data in files.items():
            atomic_write(path, data)
        catalog_before = upgrade_catalog_bindings(configs["helm"], dsn)
        command("systemctl", "restart", "oneport-api", "oneport-aws-worker")
        wait_for_api()
        command(
            "systemctl",
            "enable",
            "oneport-gcp-worker",
            "oneport-onprem-worker",
            "oneport-helm-worker",
            "oneport-helm-canary-worker",
        )
        # enable --now leaves already-running workers on their old runtime config.
        command("systemctl", "restart", *SERVICES[2:])
        time.sleep(3)
        command("systemctl", "is-active", *SERVICES)
    except Exception:
        subprocess.run(
            ["systemctl", "stop", *SERVICES[2:]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        for path, data in before.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
        if catalog_before:
            from psycopg.types.json import Jsonb
            with ReleaseStore(dsn).connect() as conn:
                for identity, binding in catalog_before:
                    conn.execute("UPDATE app_environments SET binding=%s WHERE id=%s", (Jsonb(binding), identity))
        command("systemctl", "restart", "oneport-api", "oneport-aws-worker")
        for name in SERVICES[2:]:
            if not previous_enabled[name]:
                command("systemctl", "disable", name)
            if previous_active[name]:
                command("systemctl", "start", name)
        raise


if __name__ == "__main__":
    try:
        activate()
        print(
            "Provider configuration registered and workers active; app deployment still requires a request."
        )
    except Exception:  # noqa: BLE001 -- SDK errors can include credential material
        print("Provider activation failed; credential details omitted.")
        raise SystemExit(1) from None
