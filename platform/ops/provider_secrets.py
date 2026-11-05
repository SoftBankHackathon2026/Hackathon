"""Fixed-name runtime secrets transport. Never print values or underlying exceptions."""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import boto3
import yaml
from google.oauth2 import service_account

SPECS = {
    "ONEPORT_GCP_CREDENTIALS_JSON": (
        "oneport-platform/runtime/gcp-credentials",
        "gcp-credentials.json",
    ),
    "ONEPORT_ONPREM_KUBECONFIG": (
        "oneport-platform/runtime/onprem-kubeconfig",
        "oneport-platform.kubeconfig",
    ),
}


def validate(values):
    """Validate the whole set before any remote or local write."""
    for name in SPECS:
        if not values.get(name) or len(values[name].encode()) > 48000:
            raise ValueError("missing or oversized secret")
    gcp = json.loads(values["ONEPORT_GCP_CREDENTIALS_JSON"])
    if gcp.get("type") != "service_account":
        raise ValueError("service account JSON required")
    service_account.Credentials.from_service_account_info(gcp)
    kube = yaml.safe_load(values["ONEPORT_ONPREM_KUBECONFIG"])
    if kube.get("current-context") != "oneport-onprem":
        raise ValueError("unexpected context")
    contexts = kube.get("contexts", [])
    if len(contexts) != 1 or contexts[0].get("name") != "oneport-onprem":
        raise ValueError("one dedicated context required")
    context = contexts[0]["context"]
    clusters = kube.get("clusters", [])
    users = kube.get("users", [])
    if len(clusters) != 1 or len(users) != 1:
        raise ValueError("one dedicated cluster and user required")
    if context["cluster"] != clusters[0]["name"] or context["user"] != users[0]["name"]:
        raise ValueError("unbound context")
    cluster = clusters[0]["cluster"]
    if cluster.get("server") != "https://kube-api.yubin.website":
        raise ValueError("unexpected API server")
    if cluster.get("insecure-skip-tls-verify") or cluster.get("proxy-url"):
        raise ValueError("unsafe cluster transport")
    if set(cluster) - {
        "server",
        "certificate-authority-data",
        "tls-server-name",
        "insecure-skip-tls-verify",
    }:
        raise ValueError("external cluster configuration rejected")
    user = users[0]["user"]
    if (
        set(user) != {"token"}
        or not isinstance(user["token"], str)
        or not user["token"]
    ):
        raise ValueError(
            "dedicated bearer token required; exec and admin credentials rejected"
        )


def publish(client, values):
    validate(values)
    for name, (secret_id, _) in SPECS.items():
        value = values[name]
        # Exact same bytes reuse a version; different rotation gets a new version.
        version = hashlib.sha256(value.encode()).hexdigest()
        client.put_secret_value(
            SecretId=secret_id, SecretString=value, ClientRequestToken=version
        )
        stages = client.describe_secret(SecretId=secret_id).get(
            "VersionIdsToStages", {}
        )
        current = next(
            (v for v, labels in stages.items() if "AWSCURRENT" in labels), None
        )
        if current != version:
            arguments = {
                "SecretId": secret_id,
                "VersionStage": "AWSCURRENT",
                "MoveToVersionId": version,
            }
            if current:
                arguments["RemoveFromVersionId"] = current
            client.update_secret_version_stage(**arguments)


def install(client, destination, *, uid, gid):
    values = {
        name: client.get_secret_value(SecretId=secret_id)["SecretString"]
        for name, (secret_id, _) in SPECS.items()
    }
    validate(values)
    destination.mkdir(mode=0o750, parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("secret directory cannot be a symlink")
    os.chown(destination, uid, gid)
    os.chmod(destination, 0o750)
    # Stage both files first. Readers are restarted only after successful installation.
    staged = []
    try:
        for name, (_, filename) in SPECS.items():
            fd, temporary = tempfile.mkstemp(prefix=".secret-", dir=destination)
            staged.append((Path(temporary), destination / filename))
            with os.fdopen(fd, "w") as stream:
                os.fchown(stream.fileno(), uid, gid)
                os.fchmod(stream.fileno(), 0o640)
                stream.write(values[name])
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish", "install"])
    args = parser.parse_args()
    try:
        client = boto3.client("secretsmanager", region_name="ap-northeast-2")
        if args.command == "publish":
            publish(client, {name: os.environ.get(name, "") for name in SPECS})
        else:
            import grp

            install(
                client,
                Path("/etc/oneport/secrets"),
                uid=0,
                gid=grp.getgrnam("oneport").gr_gid,
            )
        print("Provider credentials " + args.command + " completed; values omitted.")
        return 0
    except Exception:  # noqa: BLE001 -- credential errors must never leak values
        print(
            "Provider credentials operation failed; check configuration and scoped permissions."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
