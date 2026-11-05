"""Publish the registry credential from the dedicated GitHub secret, without printing it."""

import hashlib
import json
import os
import sys

import boto3


def main():
    value = json.loads(os.environ["ONEPORT_REGISTRY_CREDENTIALS_JSON"])
    if set(value) != {"username", "password"} or not all(
        isinstance(v, str) and v.strip() for v in value.values()
    ):
        raise ValueError("Invalid registry credential")
    data = json.dumps(value, sort_keys=True, separators=(",", ":"))
    client = boto3.client("secretsmanager", region_name="ap-northeast-2")
    version = hashlib.sha256(data.encode()).hexdigest()
    client.put_secret_value(
        SecretId="oneport-platform/build/dockerhub",
        ClientRequestToken=version,
        SecretString=data,
    )
    metadata = client.describe_secret(SecretId="oneport-platform/build/dockerhub")
    stages = metadata.get("VersionIdsToStages", {})
    if "AWSCURRENT" not in stages.get(version, []):
        current = next(
            (key for key, names in stages.items() if "AWSCURRENT" in names), None
        )
        client.update_secret_version_stage(
            SecretId="oneport-platform/build/dockerhub",
            VersionStage="AWSCURRENT",
            MoveToVersionId=version,
            **({"RemoveFromVersionId": current} if current else {}),
        )
    print("Registry credential published")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- never expose secret-bearing SDK error payloads
        sys.exit("Registry credential publication failed; no secret values logged")
