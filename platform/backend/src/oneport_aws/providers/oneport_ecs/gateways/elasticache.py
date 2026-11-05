from dataclasses import dataclass

from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway, Payload

CACHE_NOT_FOUND = frozenset({"ServerlessCacheNotFoundFault"})
USER_NOT_FOUND = frozenset({"UserNotFound"})
GROUP_NOT_FOUND = frozenset({"UserGroupNotFound"})
DEFAULT_PORT = 6379


@dataclass(frozen=True)
class CacheEndpoint:
    address: str
    port: int


@dataclass(frozen=True)
class ServerlessCache:
    status: str
    endpoint: CacheEndpoint | None

    @classmethod
    def from_api(cls, raw: Payload) -> "ServerlessCache":
        endpoint = raw.get("Endpoint")
        return cls(
            status=raw["Status"],
            endpoint=CacheEndpoint(endpoint["Address"], endpoint.get("Port", DEFAULT_PORT))
            if endpoint
            else None,
        )


@dataclass(frozen=True)
class CacheUser:
    status: str | None


@dataclass(frozen=True)
class CacheUserGroup:
    status: str | None


class ElastiCacheGateway(AwsGateway):
    def find_cache(self, name: str) -> ServerlessCache | None:
        found = self._read_optional(
            "describe_serverless_caches", CACHE_NOT_FOUND, ServerlessCacheName=name
        )
        caches = found.get("ServerlessCaches", []) if found else []
        return ServerlessCache.from_api(caches[0]) if caches else None

    def find_user(self, user_id: str) -> CacheUser | None:
        found = self._read_optional("describe_users", USER_NOT_FOUND, UserId=user_id)
        users = found.get("Users", []) if found else []
        return CacheUser(users[0].get("Status")) if users else None

    def find_user_group(self, group_id: str) -> CacheUserGroup | None:
        found = self._read_optional("describe_user_groups", GROUP_NOT_FOUND, UserGroupId=group_id)
        groups = found.get("UserGroups", []) if found else []
        return CacheUserGroup(groups[0].get("Status")) if groups else None

    def create_user(
        self, *, user_id: str, user_name: str, access: str, password: str, tags: list[Payload]
    ) -> None:
        self._write(
            "create_user",
            UserId=user_id,
            UserName=user_name,
            Engine="valkey",
            AccessString=access,
            AuthenticationMode={"Type": "password", "Passwords": [password]},
            Tags=tags,
        )

    def create_user_group(self, group_id: str, user_ids: list[str], tags: list[Payload]) -> None:
        self._write(
            "create_user_group", UserGroupId=group_id, Engine="valkey", UserIds=user_ids, Tags=tags
        )

    def create_cache(
        self,
        *,
        name: str,
        subnet_ids: list[str],
        security_group: str,
        max_storage_gb: int,
        max_ecpu: int,
        tags: list[Payload],
    ) -> None:
        self._write(
            "create_serverless_cache",
            ServerlessCacheName=name,
            Engine="valkey",
            SubnetIds=subnet_ids,
            SecurityGroupIds=[security_group],
            UserGroupId=name,
            CacheUsageLimits={
                "DataStorage": {"Maximum": max_storage_gb, "Unit": "GB"},
                "ECPUPerSecond": {"Maximum": max_ecpu},
            },
            Tags=tags,
        )

    def set_user_password(self, user_id: str, password: str) -> None:
        self._write(
            "modify_user",
            UserId=user_id,
            AuthenticationMode={"Type": "password", "Passwords": [password]},
        )

    def delete_cache(self, name: str) -> None:
        self._write("delete_serverless_cache", ServerlessCacheName=name)

    def delete_user_group(self, group_id: str) -> None:
        self._write("delete_user_group", UserGroupId=group_id)

    def delete_user(self, user_id: str) -> None:
        self._write("delete_user", UserId=user_id)
