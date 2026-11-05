"""Small in-memory AWS fake enforcing the oneport-deployer rules we rely on.

Not an AWS emulator (same spirit as the repo's kubernetes_fakes.MemoryApi):
it keeps identity, tags, ECS native deployment state and the IAM conditions
from infra/aws/deployer.tf, and lets tests inject errors and advance ECS.
"""

import itertools
import json
from types import SimpleNamespace

from botocore.config import Config
from botocore.exceptions import ClientError

ACCOUNT = "111122223333"
REGION = "ap-northeast-2"
LISTENER = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/oneport-alb/abc/def"
ALB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/oneport-alb/abc"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/oneport-app-runtime-boundary"
POLICY = f"arn:aws:iam::{ACCOUNT}:policy/oneport-app-runtime"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"


def error(code: str, op: str = "Call") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "fake"}}, op)


class _Client:
    """Routes calls to the shared FakeAws; supports one-shot error injection."""

    def __init__(self, aws, prefix, retries=1):
        self._aws, self._prefix = aws, prefix
        self.meta = SimpleNamespace(config=Config(retries={"total_max_attempts": retries}))

    def __getattr__(self, method):
        target = getattr(self._aws, f"{self._prefix}_{method}")

        def call(**kwargs):
            self._aws.calls.append(f"{self._prefix}.{method}")
            injected = self._aws.fail.pop(f"{self._prefix}.{method}", None)
            if injected:
                raise injected
            return target(**kwargs)

        return call


class FakeAws:
    def __init__(self):
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.ids = itertools.count(1)
        self.roles, self.target_groups, self.alarms = {}, {}, {}
        self.rules = [
            {
                "RuleArn": f"{LISTENER}/default",
                "Priority": "default",
                "Conditions": [],
                "Actions": [],
            }
        ]
        self.task_definitions, self.services, self.deployments = {}, {}, {}
        self.images: dict[str, dict] = {}
        self.ecs = _Client(self, "ecs")
        self.elbv2 = _Client(self, "elbv2")
        self.iam = _Client(self, "iam")
        self.ecr = _Client(self, "ecr")
        self.cloudwatch = _Client(self, "cloudwatch")
        self.logs = _Client(self, "logs")
        self.secrets = _Client(self, "secrets")
        self.rds = _Client(self, "rds")
        self.elasticache = _Client(self, "elasticache")
        self.tasks: dict[str, dict] = {}
        self.migration_exit = 0  # exit code the next one-off task ends with
        self.cache_users: dict[str, dict] = {}
        self.cache_groups: dict[str, dict] = {}
        self.caches: dict[str, dict] = {}
        self.secret_store: dict[str, dict] = {}
        self.log_events: list[dict] = []
        self.log_page_size = 10_000
        self.target_health: dict[str, list[str]] = {}
        self.sts = _Client(self, "sts")
        self.xray = _Client(self, "xray")
        # (metric name, TargetGroup dimension) -> [(timestamp, value)]
        self.metric_points: dict[tuple[str, str], list] = {}
        self.trace_summaries: list[dict] = []
        self.traces: list[dict] = []
        self.last_trace_filter: str | None = None

    # ---- helpers for tests
    def push(self, repository: str, archs=("amd64",), tag: str | None = None) -> str:
        digest = "sha256:" + f"{next(self.ids):064x}"
        self.images[digest] = {
            "repository": repository,
            "tag": tag,
            "manifest": {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": [{"platform": {"architecture": a, "os": "linux"}} for a in archs],
            },
        }
        return digest

    def advance(self, service: str, status: str, stage: str | None = None):
        dep = self.deployments[service][-1]
        dep.update(status=status, lifecycleStage=stage)
        if status in {"ROLLBACK_SUCCESSFUL"} and dep["source"]:
            self.services[service]["taskDefinition"] = dep["source"]

    # ---- IAM (deployer.tf conditions)
    def iam_get_role(self, RoleName):
        if RoleName not in self.roles:
            raise error("NoSuchEntity")
        return {"Role": self.roles[RoleName]}

    def iam_create_role(
        self, RoleName, AssumeRolePolicyDocument, PermissionsBoundary=None, Tags=(), **_
    ):
        if PermissionsBoundary != BOUNDARY or not any(t["Key"] == "OneportApp" for t in Tags):
            raise error("AccessDenied", "CreateRole")
        if not RoleName.startswith("oneport-rt-"):
            raise error("AccessDenied", "CreateRole")
        self.roles[RoleName] = {
            "Arn": f"arn:aws:iam::{ACCOUNT}:role/{RoleName}",
            "Tags": list(Tags),
            "PermissionsBoundary": {"PermissionsBoundaryArn": PermissionsBoundary},
            "AssumeRolePolicyDocument": json.loads(AssumeRolePolicyDocument),
            "policies": set(),
        }
        return {"Role": self.roles[RoleName]}

    def iam_list_attached_role_policies(self, RoleName):
        return {"AttachedPolicies": [{"PolicyArn": p} for p in self.roles[RoleName]["policies"]]}

    def iam_attach_role_policy(self, RoleName, PolicyArn):
        if PolicyArn != POLICY:
            raise error("AccessDenied", "AttachRolePolicy")
        self.roles[RoleName]["policies"].add(PolicyArn)

    def iam_detach_role_policy(self, RoleName, PolicyArn):
        self.roles[RoleName]["policies"].discard(PolicyArn)

    def iam_delete_role(self, RoleName):
        self.roles.pop(RoleName)

    # ---- ELBv2
    def elbv2_describe_target_groups(self, Names):
        if Names[0] not in self.target_groups:
            raise error("TargetGroupNotFound")
        return {"TargetGroups": [self.target_groups[Names[0]]]}

    def elbv2_create_target_group(
        self, Name, VpcId, TargetType, HealthCheckPath, Tags=(), **kwargs
    ):
        if not Name.startswith("oneport-") or len(Name) > 32:
            raise error("AccessDenied" if not Name.startswith("oneport-") else "ValidationError")
        group = {
            "TargetGroupArn": f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{Name}/{next(self.ids)}",
            "TargetGroupName": Name,
            "VpcId": VpcId,
            "TargetType": TargetType,
            "HealthCheckPath": HealthCheckPath,
            "Tags": list(Tags),
            "Attributes": {},
            **kwargs,
        }
        self.target_groups[Name] = group
        return {"TargetGroups": [group]}

    def _group(self, arn):
        return next(g for g in self.target_groups.values() if g["TargetGroupArn"] == arn)

    def elbv2_modify_target_group(self, TargetGroupArn, HealthCheckPath):
        self._group(TargetGroupArn)["HealthCheckPath"] = HealthCheckPath

    def elbv2_modify_target_group_attributes(self, TargetGroupArn, Attributes):
        self._group(TargetGroupArn)["Attributes"].update({a["Key"]: a["Value"] for a in Attributes})

    def elbv2_delete_target_group(self, TargetGroupArn):
        if any(TargetGroupArn in json.dumps(r["Actions"]) for r in self.rules):
            raise error("ResourceInUse")
        group = self._group(TargetGroupArn)
        del self.target_groups[group["TargetGroupName"]]

    def elbv2_describe_rules(self, ListenerArn, Marker=None):
        return {"Rules": [dict(r) for r in self.rules]}

    def elbv2_create_rule(self, ListenerArn, Priority, Conditions, Actions, Tags=()):
        if any(r["Priority"] == str(Priority) for r in self.rules):
            raise error("PriorityInUse")
        rule = {
            "RuleArn": f"{LISTENER}/rule-{next(self.ids)}",
            "Priority": str(Priority),
            "Conditions": Conditions,
            "Actions": Actions,
            "Tags": list(Tags),
        }
        self.rules.append(rule)
        return {"Rules": [rule]}

    def elbv2_delete_rule(self, RuleArn):
        self.rules = [r for r in self.rules if r["RuleArn"] != RuleArn]

    # ---- CloudWatch
    def cloudwatch_put_metric_alarm(self, AlarmName, **kwargs):
        if not AlarmName.startswith("oneport-"):
            raise error("AccessDenied")
        self.alarms[AlarmName] = kwargs

    def cloudwatch_delete_alarms(self, AlarmNames):
        for name in AlarmNames:
            self.alarms.pop(name, None)

    # ---- ECR
    def ecr_batch_get_image(self, repositoryName, imageIds, acceptedMediaTypes):
        image = self.images.get(imageIds[0]["imageDigest"])
        if image is None or image["repository"] != repositoryName:
            return {"images": [], "failures": [{"failureCode": "ImageNotFound"}]}
        return {
            "images": [
                {
                    "imageManifest": json.dumps(image["manifest"]),
                    "imageManifestMediaType": OCI_INDEX,
                }
            ]
        }

    # ---- ECS task definitions
    def ecs_register_task_definition(self, family, tags=(), **payload):
        revision = 1 + sum(1 for t in self.task_definitions.values() if t["family"] == family)
        arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{family}:{revision}"
        self.task_definitions[arn] = {
            "family": family,
            "taskDefinitionArn": arn,
            "tags": list(tags),
            "status": "ACTIVE",
            **payload,
        }
        return {"taskDefinition": self.task_definitions[arn]}

    def ecs_describe_task_definition(self, taskDefinition, include=()):
        td = self.task_definitions[taskDefinition]
        return {"taskDefinition": td, "tags": td["tags"]}

    def ecs_list_task_definitions(self, familyPrefix, status="ACTIVE", **_):
        arns = [
            a
            for a, t in self.task_definitions.items()
            if t["family"].startswith(familyPrefix) and t["status"] == status
        ]
        return {
            "taskDefinitionArns": sorted(arns, key=lambda a: int(a.rsplit(":", 1)[1]), reverse=True)
        }

    def ecs_deregister_task_definition(self, taskDefinition):
        self.task_definitions[taskDefinition]["status"] = "INACTIVE"

    # ---- ECS services + native deployments
    def _deploy(self, name, td, source):
        self.deployments.setdefault(name, []).append(
            {
                "serviceDeploymentArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service-deployment/oneport/{name}/{next(self.ids)}",
                "status": "IN_PROGRESS",
                "lifecycleStage": "SCALE_UP",
                "createdAt": next(self.ids),
                "targetServiceRevision": {"arn": f"rev:{td}"},
                "source": source,
                "sourceServiceRevisions": [{"arn": f"rev:{source}"}] if source else [],
            }
        )

    def ecs_create_service(self, cluster, serviceName, taskDefinition, clientToken, **kwargs):
        if (
            serviceName in self.services
            and self.services[serviceName]["clientToken"] == clientToken
        ):
            return {"service": self.services[serviceName]}  # idempotent retry
        self.services[serviceName] = {
            "serviceName": serviceName,
            "status": "ACTIVE",
            "clientToken": clientToken,
            "serviceArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/{cluster}/{serviceName}",
            "taskDefinition": taskDefinition,
            **kwargs,
        }
        self._deploy(serviceName, taskDefinition, None)
        return {"service": self.services[serviceName]}

    def ecs_update_service(
        self, cluster, service, taskDefinition, desiredCount, deploymentConfiguration
    ):
        svc = self.services[service]
        source = svc["taskDefinition"]
        svc.update(
            taskDefinition=taskDefinition,
            desiredCount=desiredCount,
            deploymentConfiguration=deploymentConfiguration,
        )
        self._deploy(service, taskDefinition, source)
        return {"service": svc}

    def ecs_describe_services(self, cluster, services):
        return {"services": [self.services[s] for s in services if s in self.services]}

    def ecs_delete_service(self, cluster, service, force):
        self.services[service]["status"] = "INACTIVE"

    def ecs_list_service_deployments(self, cluster, service, maxResults):
        if service not in self.services:
            raise error("ServiceNotFoundException")
        return {
            "serviceDeployments": [
                {"serviceDeploymentArn": d["serviceDeploymentArn"], "createdAt": d["createdAt"]}
                for d in self.deployments.get(service, [])
            ]
        }

    def ecs_describe_service_deployments(self, serviceDeploymentArns):
        found = [
            d
            for deps in self.deployments.values()
            for d in deps
            if d["serviceDeploymentArn"] in serviceDeploymentArns
        ]
        return {"serviceDeployments": found}

    def ecs_describe_service_revisions(self, serviceRevisionArns):
        return {
            "serviceRevisions": [{"taskDefinition": serviceRevisionArns[0].removeprefix("rev:")}]
        }

    def ecs_stop_service_deployment(self, serviceDeploymentArn, stopType):
        for name, deps in self.deployments.items():
            for dep in deps:
                if dep["serviceDeploymentArn"] == serviceDeploymentArn:
                    dep["status"] = "ROLLBACK_IN_PROGRESS"
                    dep["rollback_service"] = name
                    return {}
        raise error("InvalidParameterException")

    # ---- ECR registry side
    def ecr_get_authorization_token(self):
        import base64

        token = base64.b64encode(b"AWS:fake-registry-password").decode()
        return {
            "authorizationData": [
                {"authorizationToken": token, "proxyEndpoint": "https://registry"}
            ]
        }

    def ecr_describe_images(self, repositoryName, imageIds):
        tag = imageIds[0]["imageTag"]
        if not any(i["repository"] == repositoryName for i in self.images.values()):
            raise error("RepositoryNotFoundException")
        for digest, image in self.images.items():
            if image["repository"] == repositoryName and image["tag"] == tag:
                return {"imageDetails": [{"imageDigest": digest}]}
        raise error("ImageNotFoundException")

    # ---- Secrets Manager (deployer: create/put/describe, never get)
    def secrets_describe_secret(self, SecretId):
        if SecretId not in self.secret_store:
            raise error("ResourceNotFoundException")
        return {"ARN": self.secret_store[SecretId]["ARN"], "Name": SecretId}

    def _version(self, name, value):
        version = f"{next(self.ids):08d}-0000-0000-0000-000000000000"
        self.secret_store[name]["versions"][version] = value
        return version

    def secrets_create_secret(self, Name, SecretString, Tags=()):
        if not Name.startswith("oneport/"):
            raise error("AccessDeniedException")
        arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{Name}-AbCdEf"
        self.secret_store[Name] = {"ARN": arn, "versions": {}, "Tags": list(Tags)}
        return {"ARN": arn, "VersionId": self._version(Name, SecretString)}

    def secrets_put_secret_value(self, SecretId, SecretString):
        name = next(n for n, v in self.secret_store.items() if v["ARN"] == SecretId)
        return {"ARN": SecretId, "VersionId": self._version(name, SecretString)}

    def secrets_get_secret_value(self, **_):
        raise error("AccessDeniedException")  # deployer cannot read values

    # ---- CloudWatch Logs
    def logs_filter_log_events(
        self,
        logGroupName,
        logStreamNamePrefix,
        startTime,
        endTime=None,
        filterPattern=None,
        nextToken=None,
    ):
        # Oldest first and paged, like CloudWatch. A quoted pattern is a plain substring.
        found = [
            e
            for e in self.log_events
            if e["logStreamName"].startswith(logStreamNamePrefix)
            and (filterPattern is None or filterPattern.strip('"') in e["message"])
        ]
        start = int(nextToken or 0)
        page = found[start : start + self.log_page_size]
        more = start + self.log_page_size < len(found)
        return {"events": page, **({"nextToken": str(start + len(page))} if more else {})}

    def logs_get_log_events(self, logGroupName, logStreamName, startFromHead, limit):
        found = [e for e in self.log_events if e["logStreamName"] == logStreamName]
        if not found:
            raise error("ResourceNotFoundException")
        return {"events": found}

    def logs_start_query(self, logGroupName, startTime, endTime, queryString):
        self.last_query = queryString
        return {"queryId": "q-1"}

    def logs_get_query_results(self, queryId):
        return {"status": "Complete", "results": [[{"field": "@message", "value": "boom"}]]}

    # ---- CloudWatch metrics
    def cloudwatch_get_metric_data(self, MetricDataQueries, StartTime, EndTime, ScanBy):
        results = []
        for query in MetricDataQueries:
            metric = query["MetricStat"]["Metric"]
            dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
            scope = dims.get("TargetGroup", dims.get("ServiceName"))
            points = self.metric_points.get((metric["MetricName"], scope), [])
            points = [(at, value) for at, value in points if StartTime <= at < EndTime]
            results.append(
                {
                    "Id": query["Id"],
                    "Timestamps": [at for at, _ in points],
                    "Values": [value for _, value in points],
                }
            )
        return {"MetricDataResults": results}

    # ---- X-Ray
    def xray_get_trace_summaries(self, StartTime, EndTime, Sampling, FilterExpression):
        self.last_trace_filter = FilterExpression
        return {"TraceSummaries": list(self.trace_summaries)}

    def xray_batch_get_traces(self, TraceIds):
        return {"Traces": [t for t in self.traces if t["Id"] in TraceIds]}

    # ---- target health
    def elbv2_describe_target_health(self, TargetGroupArn):
        states = self.target_health.get(TargetGroupArn, [])
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": st}} for st in states]}

    # ---- RDS (oneport-db-* only); becomes available on the 2nd describe
    def rds_create_db_instance(
        self, DBInstanceIdentifier, MasterUserPassword, PubliclyAccessible, **kwargs
    ):
        if not DBInstanceIdentifier.startswith("oneport-db-") or PubliclyAccessible:
            raise error("AccessDenied")
        self.db = {
            "DBInstanceIdentifier": DBInstanceIdentifier,
            "DBInstanceStatus": "creating",
            "MasterUsername": kwargs["MasterUsername"],
            "DBName": kwargs["DBName"],
            "password": MasterUserPassword,
            "describes": 0,
            "kwargs": kwargs,
        }

    def rds_describe_db_instances(self, DBInstanceIdentifier):
        db = getattr(self, "db", None)
        if not db or db["DBInstanceIdentifier"] != DBInstanceIdentifier:
            raise error("DBInstanceNotFound")
        db["describes"] += 1
        if db["DBInstanceStatus"] == "deleting" and db.get("deleting_seen"):
            self.deleted_db, self.db = db, None  # gone on the describe after "deleting"
            raise error("DBInstanceNotFound")
        if db["DBInstanceStatus"] == "deleting":
            db["deleting_seen"] = True
        if db["describes"] >= 2 and db["DBInstanceStatus"] in {
            "creating",
            "resetting-master-credentials",
        }:
            db["DBInstanceStatus"] = "available"
            db["Endpoint"] = {
                "Address": f"{DBInstanceIdentifier}.c1.{REGION}.rds.amazonaws.com",
                "Port": 5432,
            }
        return {"DBInstances": [{k: v for k, v in db.items() if k not in {"password", "kwargs"}}]}

    def rds_modify_db_instance(self, DBInstanceIdentifier, MasterUserPassword, ApplyImmediately):
        self.db.update(
            password=MasterUserPassword,
            DBInstanceStatus="resetting-master-credentials",
            describes=0,
        )

    def rds_delete_db_instance(
        self, DBInstanceIdentifier, SkipFinalSnapshot, FinalDBSnapshotIdentifier=None
    ):
        self.db["DBInstanceStatus"] = "deleting"
        self.db["final_snapshot"] = FinalDBSnapshotIdentifier

    # ---- ElastiCache Serverless (infra data-access.md §4 + deployer_cache policy).
    # Groups and caches become ready on their 2nd describe; deletes finish on the next one.
    def _cache_tagged(self, Tags, name):
        if not name.startswith("oneport-cache-") or len(name) > 40:
            raise error("AccessDenied")
        if not any(t["Key"] == "OneportApp" for t in Tags):
            raise error("AccessDenied")

    def elasticache_create_user(
        self, UserId, UserName, Engine, AccessString, AuthenticationMode, Tags=()
    ):
        self._cache_tagged(Tags, UserId)
        if Engine != "valkey" or AuthenticationMode.get("Type") != "password":
            raise error("AccessDenied")
        if UserId in self.cache_users:
            raise error("UserAlreadyExists")
        self.cache_users[UserId] = {
            "UserId": UserId,
            "UserName": UserName,
            "AccessString": AccessString,
            "Status": "active",
            "password": AuthenticationMode["Passwords"][0],
        }

    def elasticache_describe_users(self, UserId):
        user = self.cache_users.get(UserId)
        if user is None:
            raise error("UserNotFound")
        if user["Status"] == "modifying":
            user["Status"] = "active"
            return {
                "Users": [
                    {**{k: v for k, v in user.items() if k != "password"}, "Status": "modifying"}
                ]
            }
        return {"Users": [{k: v for k, v in user.items() if k != "password"}]}

    def elasticache_modify_user(self, UserId, AuthenticationMode):
        if AuthenticationMode.get("Type") != "password":
            raise error("AccessDenied")
        self.cache_users[UserId].update(
            password=AuthenticationMode["Passwords"][0], Status="modifying"
        )

    def elasticache_delete_user(self, UserId):
        if any(UserId in g["UserIds"] for g in self.cache_groups.values()):
            raise error("InvalidUserState")
        self.cache_users.pop(UserId)

    def elasticache_create_user_group(self, UserGroupId, Engine, UserIds, Tags=()):
        self._cache_tagged(Tags, UserGroupId)
        users = [self.cache_users[u] for u in UserIds if u in self.cache_users]
        if len(users) != len(UserIds) or not any(u["UserName"] == "default" for u in users):
            raise error("DefaultUserRequired")
        self.cache_groups[UserGroupId] = {
            "UserGroupId": UserGroupId,
            "UserIds": list(UserIds),
            "Status": "creating",
            "describes": 0,
        }

    def elasticache_describe_user_groups(self, UserGroupId):
        group = self.cache_groups.get(UserGroupId)
        if group is None:
            raise error("UserGroupNotFound")
        group["describes"] += 1
        if group["Status"] == "deleting" and group["describes"] > 1:
            self.cache_groups.pop(UserGroupId)
            raise error("UserGroupNotFound")
        if group["Status"] == "creating" and group["describes"] >= 2:
            group["Status"] = "active"
        return {"UserGroups": [{k: v for k, v in group.items() if k != "describes"}]}

    def elasticache_delete_user_group(self, UserGroupId):
        if any(c["UserGroupId"] == UserGroupId for c in self.caches.values()):
            raise error("InvalidUserGroupState")
        self.cache_groups[UserGroupId].update(Status="deleting", describes=0)

    def elasticache_create_serverless_cache(
        self,
        ServerlessCacheName,
        Engine,
        SubnetIds,
        SecurityGroupIds,
        UserGroupId,
        CacheUsageLimits=None,
        Tags=(),
    ):
        self._cache_tagged(Tags, ServerlessCacheName)
        limits = CacheUsageLimits or {}
        if (
            Engine != "valkey"
            or limits.get("DataStorage", {}).get("Maximum", 10**9) > 1
            or limits.get("ECPUPerSecond", {}).get("Maximum", 10**9) > 5000
        ):
            raise error("AccessDenied")
        if self.cache_groups.get(UserGroupId, {}).get("Status") != "active":
            raise error("InvalidUserGroupState")
        self.caches[ServerlessCacheName] = {
            "ServerlessCacheName": ServerlessCacheName,
            "Status": "creating",
            "UserGroupId": UserGroupId,
            "SubnetIds": SubnetIds,
            "SecurityGroupIds": SecurityGroupIds,
            "describes": 0,
        }

    def elasticache_describe_serverless_caches(self, ServerlessCacheName):
        cache = self.caches.get(ServerlessCacheName)
        if cache is None:
            raise error("ServerlessCacheNotFoundFault")
        cache["describes"] += 1
        if cache["Status"] == "deleting" and cache["describes"] > 1:
            self.caches.pop(ServerlessCacheName)
            raise error("ServerlessCacheNotFoundFault")
        if cache["Status"] == "creating" and cache["describes"] >= 2:
            cache["Status"] = "available"
            cache["Endpoint"] = {
                "Address": f"{ServerlessCacheName}-x1y2.serverless.apn2.cache.amazonaws.com",
                "Port": 6379,
            }
        return {"ServerlessCaches": [{k: v for k, v in cache.items() if k != "describes"}]}

    def elasticache_delete_serverless_cache(self, ServerlessCacheName):
        self.caches[ServerlessCacheName].update(Status="deleting", describes=0)

    # ---- ECS one-off tasks (deployer: RunTask only for `oneport-*-migration`).
    # A task stops on its 2nd describe with `migration_exit`.
    def ecs_run_task(
        self, cluster, taskDefinition, launchType, count, startedBy, networkConfiguration, **_
    ):
        family = taskDefinition.rsplit("/", 1)[-1].rsplit(":", 1)[0]
        if cluster != "oneport" or not family.endswith("-migration"):
            raise error("AccessDeniedException", "RunTask")
        if taskDefinition not in self.task_definitions:
            raise error("ClientException", "RunTask")
        arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/oneport/{next(self.ids):032x}"
        self.tasks[arn] = {
            "taskArn": arn,
            "taskDefinitionArn": taskDefinition,
            "startedBy": startedBy,
            "lastStatus": "PROVISIONING",
            "describes": 0,
            "exit": self.migration_exit,
            "network": networkConfiguration,
        }
        return {"tasks": [{"taskArn": arn, "lastStatus": "PROVISIONING"}], "failures": []}

    def ecs_list_tasks(self, cluster, startedBy, desiredStatus):
        running = {"PROVISIONING", "PENDING", "RUNNING"}
        return {
            "taskArns": [
                a
                for a, t in self.tasks.items()
                if t["startedBy"] == startedBy
                and (t["lastStatus"] in running) == (desiredStatus == "RUNNING")
            ]
        }

    def ecs_describe_tasks(self, cluster, tasks):
        out = []
        for arn in tasks:
            t = self.tasks[arn]
            t["describes"] += 1
            if t["describes"] >= 2:
                t["lastStatus"] = "STOPPED"
            container = {"name": "migration", "lastStatus": t["lastStatus"]}
            if t["lastStatus"] == "STOPPED":
                container["exitCode"] = t["exit"]
            out.append(
                {
                    "taskArn": arn,
                    "lastStatus": t["lastStatus"],
                    "stoppedReason": "Essential container in task exited"
                    if t["lastStatus"] == "STOPPED"
                    else None,
                    "containers": [container],
                }
            )
        return {"tasks": out, "failures": []}
