"""AWS 한 리전의 구성을 Inventory 로 읽는다.

읽는 것: ECS(Fargate) 서비스·작업 정의, RDS PostgreSQL, Secrets Manager 메타,
ECR 저장소, ALB, Route 53 레코드. 모두 Describe/List 호출이고 비밀값 조회
(GetSecretValue)는 하지 않는다. 지원하지 않는 형태는 Omission 으로 남기고,
권한이 없는 영역은 permission_denied Omission 으로 남긴다.

  reader = aws.AwsInventoryReader(region="ap-northeast-2")
  inventory = reader.read()
"""

import datetime
import re
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import botocore.exceptions

from crosscloud import discover, model
from crosscloud.discover import env

_ACCESS_DENIED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "UnauthorizedException",
    }
)
_ECR_TAGGED_IMAGE = re.compile(
    r"(?P<registry>\d{12})\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/"
    r"(?P<repository>[^:@]+):(?P<tag>[^:@/]+)"
)
_RECORD_TYPES = frozenset({"A", "AAAA", "CNAME"})
# describe_services 가 한 번에 받는 서비스 수 상한.
_DESCRIBE_SERVICES_LIMIT = 10


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class AwsInventoryReader:
    """AWS 한 리전의 구성을 읽는다(읽기 전용, 비밀값 미조회)."""

    def __init__(
        self,
        region: str,
        client_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime.datetime] = _utc_now,
    ) -> None:
        """읽을 리전과 클라이언트를 정한다.

        Args:
            region: 읽을 리전(예: ap-northeast-2).
            client_factory: 서비스 이름을 받아 boto3 클라이언트를 돌려준다.
                없으면 기본 자격 증명 체인의 boto3 세션을 쓴다.
            clock: 수집 시각을 돌려준다.
        """
        self._region = region
        self._client = (
            client_factory or boto3.session.Session(region_name=region).client
        )
        self._clock = clock

    def read(self) -> model.Inventory:
        """구성을 읽어 Inventory 로 돌려준다.

        Returns:
            읽은 Inventory. IAM 권한·제한 정책·결합은 비어 있다.

        Raises:
            botocore.exceptions.ClientError: 권한 부족이 아닌 API 오류.
        """
        collected_at = self._clock()
        omissions: list[model.Omission] = []
        workloads = tuple(self._guarded("ecs", self._workloads, omissions))
        resources = tuple(
            resource
            for service, read in (
                ("rds", self._databases),
                ("secretsmanager", self._secrets),
                ("ecr", self._registries),
                ("elbv2", self._load_balancers),
                ("route53", self._dns_records),
            )
            for resource in self._guarded(service, read, omissions)
        )
        return model.Inventory(
            cloud="aws",
            collected_at=collected_at,
            resources=resources,
            workloads=workloads,
            grants=(),
            restrictions=(),
            restriction_scans=(),
            couplings=(),
            omissions=tuple(omissions),
            attestation=discover.unattested(workloads),
        )

    def _guarded(
        self,
        service: str,
        read: Callable[[list[model.Omission]], list[Any]],
        omissions: list[model.Omission],
    ) -> list[Any]:
        try:
            return read(omissions)
        except botocore.exceptions.ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in _ACCESS_DENIED_CODES:
                raise
        omissions.append(
            model.Omission(f"{service}:*", "permission_denied", None)
        )
        return []

    def _paginate(
        self, service: str, operation: str, key: str, **params: Any
    ) -> Iterator[Any]:
        paginator = self._client(service).get_paginator(operation)
        for page in paginator.paginate(**params):
            yield from page.get(key, [])

    def _workloads(
        self, omissions: list[model.Omission]
    ) -> list[model.Workload]:
        ecs = self._client("ecs")
        workloads = []
        for cluster in self._paginate("ecs", "list_clusters", "clusterArns"):
            arns = list(
                self._paginate(
                    "ecs", "list_services", "serviceArns", cluster=cluster
                )
            )
            for start in range(0, len(arns), _DESCRIBE_SERVICES_LIMIT):
                batch = arns[start : start + _DESCRIBE_SERVICES_LIMIT]
                response = ecs.describe_services(
                    cluster=cluster, services=batch
                )
                for service in response["services"]:
                    workload = self._workload(service, omissions)
                    if workload is not None:
                        workloads.append(workload)
        return workloads

    def _workload(
        self, service: dict[str, Any], omissions: list[model.Omission]
    ) -> model.Workload | None:
        subject = f"ecs:{service['serviceArn']}"
        if not _is_fargate(service):
            omissions.append(model.Omission(subject, "unsupported_type", None))
            return None
        task = self._client("ecs").describe_task_definition(
            taskDefinition=service["taskDefinition"]
        )["taskDefinition"]
        # 사이드카가 있는 작업은 본선 범위 밖이다.
        if len(task["containerDefinitions"]) != 1:
            omissions.append(model.Omission(subject, "unsupported_type", None))
            return None
        (container,) = task["containerDefinitions"]
        name = service["serviceName"]
        secret_refs = tuple(
            model.SecretRef(
                cloud="aws", native_id=s["valueFrom"], version="AWSCURRENT"
            )
            for s in container.get("secrets", [])
        )
        return model.Workload(
            name=name,
            cloud="aws",
            platform="ecs",
            native_id=service["serviceArn"],
            identity_id=task.get("taskRoleArn", ""),
            image=self._image(name, container["image"], omissions),
            code_repo=None,
            entries=tuple(
                model.Entry(
                    kind="http",
                    source_id=lb["targetGroupArn"],
                    attributes=(
                        model.KeyValue("container_port", lb["containerPort"]),
                    ),
                )
                for lb in service.get("loadBalancers", [])
            ),
            env=_env_entries(container, secret_refs),
            secret_refs=secret_refs,
            db_user=None,
            depends_on=tuple(ref.native_id for ref in secret_refs),
        )

    def _image(
        self, name: str, image: str, omissions: list[model.Omission]
    ) -> model.ImageRef | None:
        if "@" in image:
            repository, digest = image.split("@", 1)
            return model.ImageRef(registry_repo=repository, digest=digest)
        match = _ECR_TAGGED_IMAGE.fullmatch(image)
        if match is None:
            omissions.append(
                model.Omission(f"image:{name}", "unsupported_type", None)
            )
            return None
        details = self._client("ecr").describe_images(
            registryId=match["registry"],
            repositoryName=match["repository"],
            imageIds=[{"imageTag": match["tag"]}],
        )["imageDetails"]
        return model.ImageRef(
            registry_repo=image.rsplit(":", 1)[0],
            digest=details[0]["imageDigest"],
        )

    def _databases(
        self, omissions: list[model.Omission]
    ) -> list[model.Resource]:
        databases = []
        for db in self._paginate("rds", "describe_db_instances", "DBInstances"):
            if db["Engine"] != "postgres":
                omissions.append(
                    model.Omission(
                        f"rds:{db['DBInstanceArn']}", "unsupported_type", None
                    )
                )
                continue
            endpoint = db.get("Endpoint", {})
            databases.append(
                self._resource(
                    "postgres_instance",
                    db["DBInstanceArn"],
                    engine="postgres",
                    engine_version=db.get("EngineVersion"),
                    instance_class=db.get("DBInstanceClass"),
                    iam_auth=db.get("IAMDatabaseAuthenticationEnabled"),
                    publicly_accessible=db.get("PubliclyAccessible"),
                    db_name=db.get("DBName"),
                    host=endpoint.get("Address"),
                    port=endpoint.get("Port"),
                )
            )
        return databases

    def _secrets(self, omissions: list[model.Omission]) -> list[model.Resource]:
        return [
            self._resource(
                "secret",
                secret["ARN"],
                name=secret["Name"],
                kms_key_id=secret.get("KmsKeyId"),
                rotation_enabled=secret.get("RotationEnabled", False),
            )
            for secret in self._paginate(
                "secretsmanager", "list_secrets", "SecretList"
            )
        ]

    def _registries(
        self, omissions: list[model.Omission]
    ) -> list[model.Resource]:
        return [
            self._resource(
                "container_registry",
                repo["repositoryArn"],
                name=repo["repositoryName"],
                repository_uri=repo["repositoryUri"],
            )
            for repo in self._paginate(
                "ecr", "describe_repositories", "repositories"
            )
        ]

    def _load_balancers(
        self, omissions: list[model.Omission]
    ) -> list[model.Resource]:
        balancers = []
        for lb in self._paginate(
            "elbv2", "describe_load_balancers", "LoadBalancers"
        ):
            if lb.get("Type") != "application":
                omissions.append(
                    model.Omission(
                        f"elbv2:{lb['LoadBalancerArn']}",
                        "unsupported_type",
                        None,
                    )
                )
                continue
            balancers.append(
                self._resource(
                    "load_balancer",
                    lb["LoadBalancerArn"],
                    lb_type="application",
                    scheme=lb.get("Scheme"),
                    dns_name=lb.get("DNSName"),
                )
            )
        return balancers

    def _dns_records(
        self, omissions: list[model.Omission]
    ) -> list[model.Resource]:
        records = []
        for zone in self._paginate(
            "route53", "list_hosted_zones", "HostedZones"
        ):
            zone_id = zone["Id"].removeprefix("/hostedzone/")
            for record in self._paginate(
                "route53",
                "list_resource_record_sets",
                "ResourceRecordSets",
                HostedZoneId=zone["Id"],
            ):
                if record["Type"] not in _RECORD_TYPES:
                    continue
                records.append(_dns_record(zone, zone_id, record))
        return records

    def _resource(
        self,
        kind: model.ResourceKind,
        native_id: str,
        **attributes: str | int | bool | None,
    ) -> model.Resource:
        return model.Resource(
            kind=kind,
            cloud="aws",
            native_id=native_id,
            region=self._region,
            attributes=tuple(
                model.KeyValue(key, value) for key, value in attributes.items()
            ),
            source_of_truth="api",
        )


def _is_fargate(service: dict[str, Any]) -> bool:
    if service.get("launchType") == "FARGATE":
        return True
    return any(
        strategy["capacityProvider"].startswith("FARGATE")
        for strategy in service.get("capacityProviderStrategy", [])
    )


def _env_entries(
    container: dict[str, Any], secret_refs: tuple[model.SecretRef, ...]
) -> tuple[model.EnvEntry, ...]:
    plain = tuple(
        env.classify_env(variable["name"], variable.get("value", ""))
        for variable in container.get("environment", [])
    )
    referenced = tuple(
        env.classify_env(secret["name"], ref)
        for secret, ref in zip(
            container.get("secrets", []), secret_refs, strict=True
        )
    )
    return plain + referenced


def _dns_record(
    zone: dict[str, Any], zone_id: str, record: dict[str, Any]
) -> model.Resource:
    name = record["Name"].rstrip(".")
    native_id = f"{zone_id}/{name}/{record['Type']}"
    if "SetIdentifier" in record:
        native_id += f"/{record['SetIdentifier']}"
    return model.Resource(
        kind="dns_record",
        cloud="aws",
        native_id=native_id,
        region="global",
        attributes=(
            model.KeyValue("zone_name", zone["Name"].rstrip(".")),
            model.KeyValue("private_zone", zone["Config"]["PrivateZone"]),
            model.KeyValue("record_type", record["Type"]),
            model.KeyValue("set_identifier", record.get("SetIdentifier")),
            model.KeyValue("weight", record.get("Weight")),
        ),
        source_of_truth="api",
    )
