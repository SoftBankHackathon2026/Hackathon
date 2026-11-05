from dataclasses import dataclass

from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway, Payload

NOT_FOUND = frozenset({"ResourceNotFoundException"})


@dataclass(frozen=True)
class SecretInfo:
    arn: str
    scheduled_for_deletion: bool


@dataclass(frozen=True)
class SecretVersion:
    arn: str
    version_id: str

    @property
    def pinned_ref(self) -> str:
        return f"{self.arn}:::{self.version_id}"


class SecretsGateway(AwsGateway):
    def find_secret(self, name: str) -> SecretInfo | None:
        found = self._read_optional("describe_secret", NOT_FOUND, SecretId=name)
        if found is None:
            return None
        return SecretInfo(
            arn=found["ARN"], scheduled_for_deletion=found.get("DeletedDate") is not None
        )

    def create_secret(self, name: str, value: str, tags: list[Payload]) -> SecretVersion:
        result = self._write("create_secret", Name=name, SecretString=value, Tags=tags)
        return SecretVersion(arn=result["ARN"], version_id=result["VersionId"])

    def put_value(self, arn: str, value: str) -> SecretVersion:
        result = self._write("put_secret_value", SecretId=arn, SecretString=value)
        return SecretVersion(arn=arn, version_id=result["VersionId"])
