from typing import Any

from controlplane.builds.models import BuildSettings

MAX_LOG_EVENTS = 100
MAX_LINE_LENGTH = 4096


class CodeBuildLogReader:
    def __init__(self, settings: BuildSettings) -> None:
        self._settings = settings

    def _client(self, service: str) -> Any:
        import boto3
        from botocore.config import Config

        config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
        return boto3.client(service, region_name=self._settings.region, config=config)

    def _log_location(self, handle: str) -> dict[str, str]:
        builds = self._client("codebuild").batch_get_builds(ids=[handle])["builds"]
        return builds[0].get("logs", {})

    def read(self, handle: str) -> list[str]:
        location = self._log_location(handle)
        expected_group = "/aws/codebuild/" + self._settings.project
        if location.get("groupName") != expected_group or not location.get("streamName"):
            return []
        result = self._client("logs").get_log_events(
            logGroupName=location["groupName"],
            logStreamName=location["streamName"],
            limit=MAX_LOG_EVENTS,
            startFromHead=False,
        )
        return [event["message"][:MAX_LINE_LENGTH] for event in result.get("events", [])]
