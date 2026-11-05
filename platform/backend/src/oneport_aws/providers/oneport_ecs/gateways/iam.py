from dataclasses import dataclass

from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway

NO_SUCH_ENTITY = frozenset({"NoSuchEntity"})


@dataclass(frozen=True)
class IamRole:
    name: str
    tags: dict[str, str]


class IamGateway(AwsGateway):
    def find_role(self, name: str) -> IamRole | None:
        found = self._read_optional("get_role", NO_SUCH_ENTITY, RoleName=name)
        if found is None:
            return None
        tags = {tag["Key"]: tag["Value"] for tag in found["Role"].get("Tags", [])}
        return IamRole(name=name, tags=tags)

    def list_attached_policy_arns(self, role_name: str) -> set[str]:
        found = self._read("list_attached_role_policies", RoleName=role_name)
        return {policy["PolicyArn"] for policy in found["AttachedPolicies"]}

    def create_role(
        self,
        *,
        name: str,
        trust_policy: str,
        boundary_arn: str,
        description: str,
        tags: list[dict[str, str]],
    ) -> None:
        self._write(
            "create_role",
            RoleName=name,
            AssumeRolePolicyDocument=trust_policy,
            PermissionsBoundary=boundary_arn,
            Description=description,
            Tags=tags,
        )

    def attach_policy(self, role_name: str, policy_arn: str) -> None:
        self._write("attach_role_policy", RoleName=role_name, PolicyArn=policy_arn)

    def detach_policy(self, role_name: str, policy_arn: str) -> None:
        self._write("detach_role_policy", RoleName=role_name, PolicyArn=policy_arn)

    def delete_role(self, name: str) -> None:
        self._write("delete_role", RoleName=name)
