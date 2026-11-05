from collections.abc import Callable
from typing import Any

from controlplane.core.errors import OperationNotFound, RequestConflict
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.accounts import AccountStore
from controlplane.schemas.accounts import Identity

APP_NOT_OWNED = "이 팀이 배포한 앱이 없습니다."
CLAIMED_BY_OTHER_TEAM = "다른 팀이 사용 중인 환경입니다."


class TeamAccess:
    def __init__(self, registry: ProviderRegistry, accounts: AccountStore | None) -> None:
        self._registry = registry
        self._accounts = accounts

    def _catalog_owned(self, environment_id: str, identity: Identity | None) -> bool:
        catalog = self._registry.catalog
        return bool(catalog and catalog.require_owner(environment_id, identity))

    def _claim_owner(self, environment_id: str) -> str | None:
        return self._accounts.owners().get(environment_id)

    def check_plannable(self, environment_id: str, identity: Identity | None) -> None:
        if self._catalog_owned(environment_id, identity) or identity is None:
            return
        if self._claim_owner(environment_id) not in (None, identity.team):
            raise RequestConflict(CLAIMED_BY_OTHER_TEAM)

    def require_owner(self, environment_id: str, identity: Identity | None) -> None:
        if self._catalog_owned(environment_id, identity):
            return
        if identity and self._claim_owner(environment_id) != identity.team:
            raise OperationNotFound(APP_NOT_OWNED)

    def as_owner[T](
        self, environment_id: str, identity: Identity | None, action: Callable[[], T]
    ) -> T:
        if self._catalog_owned(environment_id, identity) or identity is None:
            return action()
        created = self._accounts.claim(environment_id, identity)
        try:
            return action()
        except Exception:
            if created:
                self._accounts.release_claim(environment_id, identity)
            raise

    def visible(
        self, items: list[dict[str, Any]], identity: Identity | None
    ) -> list[dict[str, Any]]:
        if identity is None:
            return items
        owners = self._accounts.owners()
        return [
            shown
            for item in items
            if (shown := self._visible_item(item, identity, owners)) is not None
        ]

    def _visible_item(
        self, item: dict[str, Any], identity: Identity, owners: dict[str, str]
    ) -> dict[str, Any] | None:
        catalog = self._registry.catalog
        binding = catalog.binding(item["id"]) if catalog else None
        if binding and binding["binding"] is not None:
            return {**item, "team": binding["team"]} if binding["team"] == identity.team else None
        owner = owners.get(item["id"])
        if owner in (None, identity.team) or not item.get("executable"):
            return {**item, "team": owner}
        return {
            "id": item["id"],
            "name": "다른 팀이 사용 중인 환경",
            "runtime": item["runtime"],
            "state": "claimed",
            "executable": False,
            "connection_verified": False,
        }
