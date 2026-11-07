"""Route 53 가중 레코드 두 개(라벨 aws·gcp)의 가중치를 읽고 바꾼다.

같은 이름·형식에 SetIdentifier 가 클라우드 라벨(aws·gcp)인 가중 레코드 둘을
Terraform 이 만들어 둔다(원본 100 / 대상 0). 여기서는 가중치만 바꾼다. 두 값의
합은 늘 100 이고, 변경은 GetChange 가 INSYNC 일 때만 반영으로 본다.

  route = route53.WeightedRoute(client, zone_id, "app.ccx.internal", "A")
  change_id = route.set_weights({"aws": 0, "gcp": 100})
  route.wait_insync(change_id, timeout_seconds=120, interval=5)
"""

import collections.abc
import dataclasses
import json
import os
import pathlib
import time
from typing import Any

from crosscloud import model

_LABELS: tuple[model.Cloud, ...] = ("aws", "gcp")


class RoutingError(RuntimeError):
    """가중 레코드를 읽거나 바꿀 수 없다."""


@dataclasses.dataclass(frozen=True)
class RouteSnapshot:
    """지금 가중치. ttl_seconds 는 alias 가 아닌 레코드의 TTL(없으면 None)."""

    weights: dict[str, int]
    ttl_seconds: int | None


class WeightedRoute:
    """한 이름의 가중 레코드 쌍. client 는 boto3 route53 클라이언트다."""

    def __init__(
        self, client: Any, zone_id: str, record_name: str, record_type: str
    ) -> None:
        """레코드 쌍을 가리킨다. 이름 끝 점은 있어도 없어도 된다."""
        self._client = client
        self.zone_id = zone_id
        self.record_name = record_name.rstrip(".").lower() + "."
        self._record_type = record_type

    def read(self) -> RouteSnapshot:
        """두 레코드의 가중치를 읽는다.

        Raises:
            RoutingError: 라벨 레코드 중 하나라도 없을 때.
        """
        records = self._records()
        ttls = [r["TTL"] for r in records.values() if "TTL" in r]
        return RouteSnapshot(
            weights={label: int(r["Weight"]) for label, r in records.items()},
            ttl_seconds=min(ttls) if ttls else None,
        )

    def set_weights(self, weights: dict[str, int]) -> str:
        """두 레코드를 한 변경 묶음으로 UPSERT 한다. 대상은 그대로 둔다.

        Args:
            weights: 라벨별 가중치. 두 라벨 모두, 합 100.

        Returns:
            Route 53 변경 id. 반영 확인은 wait_insync 로 한다.

        Raises:
            ValueError: 라벨이 맞지 않거나 합이 100 이 아닐 때.
            RoutingError: 라벨 레코드가 없을 때.
        """
        if set(weights) != set(_LABELS) or sum(weights.values()) != 100:
            raise ValueError(f"가중치는 aws·gcp 합 100 이어야 한다: {weights}")
        records = self._records()
        changes = [
            {
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    **records[label],
                    "Weight": weights[label],
                },
            }
            for label in _LABELS
        ]
        comment = " ".join(f"{label}={weights[label]}" for label in _LABELS)
        response = self._client.change_resource_record_sets(
            HostedZoneId=self.zone_id,
            ChangeBatch={
                "Comment": f"crosscloud {comment}",
                "Changes": changes,
            },
        )
        return response["ChangeInfo"]["Id"]

    def change_insync(self, change_id: str) -> bool:
        """변경이 모든 권한 서버에 퍼졌는지(INSYNC)."""
        response = self._client.get_change(Id=change_id)
        return response["ChangeInfo"]["Status"] == "INSYNC"

    def wait_insync(
        self,
        change_id: str,
        *,
        timeout_seconds: float,
        interval: float,
        clock: collections.abc.Callable[[], float] = time.monotonic,
        sleep: collections.abc.Callable[[float], None] = time.sleep,
    ) -> bool:
        """INSYNC 가 될 때까지 기다린다. 기한을 넘기면 False(불명)."""
        deadline = clock() + timeout_seconds
        while True:
            if self.change_insync(change_id):
                return True
            if clock() + interval > deadline:
                return False
            sleep(interval)

    def _records(self) -> dict[str, dict[str, Any]]:
        response = self._client.list_resource_record_sets(
            HostedZoneId=self.zone_id,
            StartRecordName=self.record_name,
            StartRecordType=self._record_type,
            MaxItems="10",
        )
        records = {
            record["SetIdentifier"]: record
            for record in response["ResourceRecordSets"]
            if record["Name"].lower() == self.record_name
            and record["Type"] == self._record_type
            and record.get("SetIdentifier") in _LABELS
        }
        missing = [label for label in _LABELS if label not in records]
        if missing:
            raise RoutingError(
                f"{self.record_name} 가중 레코드가 없다: {', '.join(missing)}"
            )
        return records


class ChangeRecord:
    """마지막 가중치 변경 id 를 담는 작은 파일. 원장이 아니라 확인용이다."""

    def __init__(self, path: pathlib.Path) -> None:
        """기록 파일 경로를 받는다. 파일은 처음 save 때 만든다."""
        self._path = path

    def load(self) -> str | None:
        """기록된 변경 id. 없으면 None.

        Raises:
            RoutingError: 파일이 깨졌을 때.
        """
        if not self._path.exists():
            return None
        try:
            return json.loads(self._path.read_text())["last_change_id"]
        except (ValueError, KeyError, TypeError) as error:
            raise RoutingError(
                f"변경 기록을 읽지 못했다: {self._path}"
            ) from error

    def save(self, change_id: str) -> None:
        """변경 id 를 원자적으로 기록한다."""
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"last_change_id": change_id}))
        os.replace(temporary, self._path)


def fully_on(route: WeightedRoute, record: ChangeRecord, label: str) -> bool:
    """라벨 쪽이 100 이고 마지막 변경이 INSYNC 인지. 모르면 False."""
    if route.read().weights.get(label) != 100:
        return False
    change_id = record.load()
    return change_id is not None and route.change_insync(change_id)
