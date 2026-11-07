import os
import re
from datetime import UTC, datetime
from pathlib import Path

from controlplane.core.errors import InvalidRequest
from controlplane.schemas.traffic import ROUTE_NAME, TrafficRoute

NAME = re.compile(ROUTE_NAME)


class TrafficRoutes:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _markers(self, app: str) -> list[Path]:
        return sorted(self.root.glob(f"{app}@*"))

    def current(self, app: str) -> TrafficRoute:
        self._check(app)
        markers = self._markers(app)
        if not markers:
            return TrafficRoute(app=app, target=None)
        latest = max(markers, key=lambda path: path.stat().st_mtime)
        return TrafficRoute(
            app=app,
            target=latest.name.split("@", 1)[1],
            changed_at=datetime.fromtimestamp(latest.stat().st_mtime, UTC),
        )

    def switch(self, app: str, target: str) -> TrafficRoute:
        self._check(app)
        self._check(target)
        marker = self.root / f"{app}@{target}"
        temporary = self.root / f".{app}@{target}.tmp"
        temporary.write_text(datetime.now(UTC).isoformat() + "\n")
        os.replace(temporary, marker)
        for other in self._markers(app):
            if other != marker:
                other.unlink(missing_ok=True)
        return self.current(app)

    @staticmethod
    def _check(name: str) -> None:
        if not NAME.fullmatch(name):
            raise InvalidRequest("앱·경로 이름은 소문자·숫자·- 로 된 63자 이하여야 해요.")


def routes_from_environment() -> TrafficRoutes | None:
    configured = os.getenv("ONEPORT_TRAFFIC_DIR")
    if not configured:
        return None
    root = Path(configured)
    return TrafficRoutes(root) if root.is_dir() else None
