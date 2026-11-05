from dataclasses import dataclass

from oneport_aws.providers.oneport_ecs.connection import AwsClients
from oneport_aws.providers.oneport_ecs.gateways.cloudwatch import CloudWatchGateway
from oneport_aws.providers.oneport_ecs.gateways.ecs import EcsGateway
from oneport_aws.providers.oneport_ecs.gateways.elasticache import ElastiCacheGateway
from oneport_aws.providers.oneport_ecs.gateways.elb import ElbGateway
from oneport_aws.providers.oneport_ecs.gateways.iam import IamGateway
from oneport_aws.providers.oneport_ecs.gateways.logs import LogsGateway
from oneport_aws.providers.oneport_ecs.gateways.rds import RdsGateway
from oneport_aws.providers.oneport_ecs.gateways.secrets_manager import SecretsGateway
from oneport_aws.providers.oneport_ecs.gateways.xray import XrayGateway


@dataclass(frozen=True)
class AwsGateways:
    ecs: EcsGateway
    elb: ElbGateway
    iam: IamGateway
    cloudwatch: CloudWatchGateway
    secrets: SecretsGateway
    rds: RdsGateway
    elasticache: ElastiCacheGateway
    logs: LogsGateway
    xray: XrayGateway

    @classmethod
    def from_clients(cls, clients: AwsClients) -> "AwsGateways":
        return cls(
            ecs=EcsGateway(clients.ecs),
            elb=ElbGateway(clients.elbv2),
            iam=IamGateway(clients.iam),
            cloudwatch=CloudWatchGateway(clients.cloudwatch),
            secrets=SecretsGateway(clients.secrets),
            rds=RdsGateway(clients.rds),
            elasticache=ElastiCacheGateway(clients.elasticache),
            logs=LogsGateway(clients.logs),
            xray=XrayGateway(clients.xray),
        )
