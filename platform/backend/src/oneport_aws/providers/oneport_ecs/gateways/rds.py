from dataclasses import dataclass

from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway, Payload

NOT_FOUND = frozenset({"DBInstanceNotFound"})


@dataclass(frozen=True)
class Endpoint:
    address: str
    port: int


@dataclass(frozen=True)
class DbInstance:
    status: str
    endpoint: Endpoint | None
    master_username: str | None
    db_name: str | None

    @classmethod
    def from_api(cls, raw: Payload) -> "DbInstance":
        endpoint = raw.get("Endpoint")
        return cls(
            status=raw["DBInstanceStatus"],
            endpoint=Endpoint(endpoint["Address"], endpoint["Port"]) if endpoint else None,
            master_username=raw.get("MasterUsername"),
            db_name=raw.get("DBName"),
        )


class RdsGateway(AwsGateway):
    def find_instance(self, identifier: str) -> DbInstance | None:
        found = self._read_optional(
            "describe_db_instances", NOT_FOUND, DBInstanceIdentifier=identifier
        )
        return DbInstance.from_api(found["DBInstances"][0]) if found else None

    def create_instance(
        self,
        *,
        identifier: str,
        instance_class: str,
        storage_gib: int,
        db_name: str,
        username: str,
        password: str,
        subnet_group: str,
        security_group: str,
        tags: list[Payload],
    ) -> None:
        self._write(
            "create_db_instance",
            DBInstanceIdentifier=identifier,
            Engine="postgres",
            DBInstanceClass=instance_class,
            AllocatedStorage=storage_gib,
            StorageType="gp3",
            DBName=db_name,
            MasterUsername=username,
            MasterUserPassword=password,
            DBSubnetGroupName=subnet_group,
            VpcSecurityGroupIds=[security_group],
            PubliclyAccessible=False,
            MultiAZ=False,
            StorageEncrypted=True,
            CopyTagsToSnapshot=True,
            Tags=tags,
        )

    def set_master_password(self, identifier: str, password: str) -> None:
        self._write(
            "modify_db_instance",
            DBInstanceIdentifier=identifier,
            MasterUserPassword=password,
            ApplyImmediately=True,
        )

    def delete_instance(self, identifier: str, final_snapshot: str | None) -> None:
        if final_snapshot is None:
            self._write(
                "delete_db_instance", DBInstanceIdentifier=identifier, SkipFinalSnapshot=True
            )
            return
        self._write(
            "delete_db_instance",
            DBInstanceIdentifier=identifier,
            FinalDBSnapshotIdentifier=final_snapshot,
            SkipFinalSnapshot=False,
        )
