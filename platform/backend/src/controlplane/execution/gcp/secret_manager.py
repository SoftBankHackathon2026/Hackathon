import base64
from urllib.parse import urlencode

from controlplane.execution.gcp.api import GcpApiClient, JsonObject

SECRETS_API = "https://secretmanager.googleapis.com/v1/"


class SecretManager:
    def __init__(self, api: GcpApiClient, project_id: str, region: str):
        self.api = api
        self.project_id = project_id
        self.region = region

    def get(self, secret_url: str) -> JsonObject | None:
        return self.api.call_unless_missing("GET", secret_url)

    def create(self, secret_name: str, labels: dict[str, str]) -> None:
        query = urlencode({"secretId": secret_name.rsplit("/", 1)[1]})
        self.api.call_unless_conflict(
            "POST",
            f"{SECRETS_API}projects/{self.project_id}/secrets?{query}",
            body={
                "labels": labels,
                "replication": {"userManaged": {"replicas": [{"location": self.region}]}},
            },
        )

    def add_version(self, secret_url: str, value: str) -> JsonObject:
        return self.api.call(
            "POST",
            secret_url + ":addVersion",
            body={"payload": {"data": base64.b64encode(value.encode()).decode()}},
        )

    def version_enabled(self, secret_url: str, version: str) -> bool:
        return self.api.call("GET", f"{secret_url}/versions/{version}").get("state") == "ENABLED"


def labels_match(actual: dict[str, str], expected: dict[str, str]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())
