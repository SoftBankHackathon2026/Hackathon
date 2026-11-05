from dataclasses import dataclass
from typing import Any

from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway, Payload

TARGET_GROUP_NOT_FOUND = frozenset({"TargetGroupNotFound"})


def target_group_dimension(target_group_arn: str) -> str:
    return target_group_arn[target_group_arn.index("targetgroup/") :]


@dataclass(frozen=True)
class TargetGroup:
    arn: str
    vpc_id: str
    target_type: str
    health_check_path: str | None


@dataclass(frozen=True)
class ForwardTarget:
    target_group_arn: str
    weight: int

    @property
    def name(self) -> str:
        return self.target_group_arn.split("/")[-2]


@dataclass(frozen=True)
class ListenerRule:
    arn: str
    priority: str
    hosts: frozenset[str]
    forwards: tuple[ForwardTarget, ...]

    @classmethod
    def from_api(cls, raw: Payload) -> "ListenerRule":
        hosts = frozenset(
            host
            for condition in raw.get("Conditions", [])
            if condition.get("Field") == "host-header"
            for host in condition.get("HostHeaderConfig", {}).get("Values", [])
        )
        forwards = tuple(
            ForwardTarget(group["TargetGroupArn"], group.get("Weight", 0))
            for action in raw.get("Actions", [])
            if action["Type"] == "forward"
            for group in action.get("ForwardConfig", {}).get("TargetGroups", [])
        )
        return cls(raw["RuleArn"], raw["Priority"], hosts, forwards)

    @property
    def target_group_arns(self) -> set[str]:
        return {forward.target_group_arn for forward in self.forwards}


class ElbGateway(AwsGateway):
    def find_target_group(self, name: str) -> TargetGroup | None:
        found = self._read_optional("describe_target_groups", TARGET_GROUP_NOT_FOUND, Names=[name])
        if found is None:
            return None
        group = found["TargetGroups"][0]
        return TargetGroup(
            arn=group["TargetGroupArn"],
            vpc_id=group["VpcId"],
            target_type=group["TargetType"],
            health_check_path=group.get("HealthCheckPath"),
        )

    def create_target_group(
        self, *, name: str, port: int, vpc_id: str, health_path: str, tags: list[Payload]
    ) -> str:
        created = self._write(
            "create_target_group",
            Name=name,
            Protocol="HTTP",
            ProtocolVersion="HTTP1",
            Port=port,
            VpcId=vpc_id,
            TargetType="ip",
            HealthCheckEnabled=True,
            HealthCheckProtocol="HTTP",
            HealthCheckPort="traffic-port",
            HealthCheckPath=health_path,
            HealthCheckIntervalSeconds=10,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=2,
            Matcher={"HttpCode": "200"},
            Tags=tags,
        )
        arn = created["TargetGroups"][0]["TargetGroupArn"]
        self._write(
            "modify_target_group_attributes",
            TargetGroupArn=arn,
            Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}],
        )
        return arn

    def set_health_check_path(self, target_group_arn: str, path: str) -> None:
        self._write("modify_target_group", TargetGroupArn=target_group_arn, HealthCheckPath=path)

    def delete_target_group(self, target_group_arn: str) -> None:
        self._write("delete_target_group", TargetGroupArn=target_group_arn)

    def list_rules(self, listener_arn: str) -> list[ListenerRule]:
        rules: list[ListenerRule] = []
        kwargs: dict[str, Any] = {"ListenerArn": listener_arn}
        while True:
            page = self._read("describe_rules", **kwargs)
            rules += [ListenerRule.from_api(raw) for raw in page["Rules"]]
            if not page.get("NextMarker"):
                return rules
            kwargs["Marker"] = page["NextMarker"]

    def create_host_rule(
        self, *, listener_arn: str, priority: int, host: str, blue: str, green: str, tags: list
    ) -> str:
        created = self._write(
            "create_rule",
            ListenerArn=listener_arn,
            Priority=priority,
            Conditions=[{"Field": "host-header", "HostHeaderConfig": {"Values": [host]}}],
            Actions=[
                {
                    "Type": "forward",
                    "ForwardConfig": {
                        "TargetGroups": [
                            {"TargetGroupArn": blue, "Weight": 100},
                            {"TargetGroupArn": green, "Weight": 0},
                        ]
                    },
                }
            ],
            Tags=tags,
        )
        return created["Rules"][0]["RuleArn"]

    def delete_rule(self, rule_arn: str) -> None:
        self._write("delete_rule", RuleArn=rule_arn)

    def list_target_states(self, target_group_arn: str) -> list[str]:
        health = self._read("describe_target_health", TargetGroupArn=target_group_arn)
        return [item["TargetHealth"]["State"] for item in health["TargetHealthDescriptions"]]
