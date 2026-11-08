import re

PASS, FAIL, ERROR = "pass", "fail", "error"
DENIED = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "UnauthorizedException",
}
CREDENTIALS = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "InvalidSignatureException",
    "SignatureDoesNotMatch",
}
THROTTLED = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
}
MAX_PAGES = 100


class AwsError(Exception):
    def __init__(self, code, missing=False):
        super().__init__(code)
        self.code = code
        self.missing = missing


def reason(code):
    if code in DENIED:
        return "AWS_ACCESS_DENIED"
    if code in CREDENTIALS:
        return "AWS_CREDENTIALS_INVALID"
    if code in THROTTLED:
        return "AWS_THROTTLED"
    return "AWS_REQUEST_FAILED"


def aws(method, missing=(), **params):
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        ConnectTimeoutError,
        EndpointConnectionError,
        NoCredentialsError,
        ReadTimeoutError,
    )

    try:
        return method(**params)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in missing:
            raise AwsError("NOT_FOUND", missing=True) from None
        raise AwsError(reason(code)) from None
    except NoCredentialsError:
        raise AwsError("AWS_CREDENTIALS_UNAVAILABLE") from None
    except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError):
        raise AwsError("AWS_UNREACHABLE") from None
    except BotoCoreError:
        raise AwsError("AWS_REQUEST_FAILED") from None


def pages(method, field, **params):
    seen = set()
    for _ in range(MAX_PAGES):
        reply = aws(method, **params)
        yield from reply.get(field) or []
        token = reply.get("NextToken")
        if not token:
            return
        if token in seen:
            raise AwsError("AWS_PAGINATION_REPEATED")
        seen.add(token)
        params["NextToken"] = token
    raise AwsError("AWS_PAGINATION_LIMIT")


def entry(name, status, code, **detail):
    result = {"name": name, "status": status, "code": code}
    if detail:
        result["detail"] = detail
    return result


def wildcard(pattern, name):
    return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), name) is not None


def scanned_by_rule(scanning, name):
    for rule in scanning.get("rules") or []:
        if rule.get("scanFrequency") not in ("SCAN_ON_PUSH", "CONTINUOUS_SCAN"):
            continue
        for item in rule.get("repositoryFilters") or []:
            if item.get("filterType") == "WILDCARD" and wildcard(
                item.get("filter", ""), name
            ):
                return True
    return False


def scan_on_push(repository, scanning, name):
    flag = (repository.get("imageScanningConfiguration") or {}).get("scanOnPush")
    if isinstance(scanning, AwsError):
        if flag is True:
            return entry("ecr.scan_on_push", PASS, "ECR_SCAN_ON_PUSH", repository=name)
        return entry("ecr.scan_on_push", ERROR, scanning.code, repository=name)
    enhanced = scanning.get("scanType") == "ENHANCED"
    if scanned_by_rule(scanning, name) or (flag is True and not enhanced):
        return entry("ecr.scan_on_push", PASS, "ECR_SCAN_ON_PUSH", repository=name)
    return entry("ecr.scan_on_push", FAIL, "ECR_SCAN_ON_PUSH_DISABLED", repository=name)


def registry(clients, section, config):
    ecr = clients("ecr")
    account = config["account_id"]
    items = []
    try:
        reply = aws(ecr.describe_registry)
    except AwsError as error:
        items.append(entry("ecr.registry", ERROR, error.code))
    else:
        if reply.get("registryId") == account:
            items.append(entry("ecr.registry", PASS, "ECR_REGISTRY_REACHABLE"))
        else:
            items.append(entry("ecr.registry", FAIL, "ECR_REGISTRY_ACCOUNT_MISMATCH"))
    try:
        data = aws(ecr.get_authorization_token).get("authorizationData") or []
    except AwsError as error:
        items.append(entry("ecr.authorization_token", ERROR, error.code))
    else:
        issued = bool(data) and all(
            isinstance(item.get("authorizationToken"), str)
            and item["authorizationToken"]
            for item in data
        )
        items.append(
            entry("ecr.authorization_token", PASS, "ECR_AUTH_TOKEN_ISSUED")
            if issued
            else entry("ecr.authorization_token", FAIL, "ECR_AUTH_TOKEN_EMPTY")
        )
    scanning = None
    if section["scan_on_push"]:
        try:
            scanning = (
                aws(ecr.get_registry_scanning_configuration).get(
                    "scanningConfiguration"
                )
                or {}
            )
        except AwsError as error:
            scanning = error
    for name in section["repositories"]:
        try:
            found = (
                aws(
                    ecr.describe_repositories,
                    missing={"RepositoryNotFoundException"},
                    registryId=account,
                    repositoryNames=[name],
                ).get("repositories")
                or []
            )
        except AwsError as error:
            if error.missing:
                items.append(
                    entry(
                        "ecr.repository",
                        FAIL,
                        "ECR_REPOSITORY_MISSING",
                        repository=name,
                    )
                )
            else:
                items.append(
                    entry("ecr.repository", ERROR, error.code, repository=name)
                )
            continue
        repository = next((r for r in found if r.get("repositoryName") == name), None)
        if repository is None:
            items.append(
                entry("ecr.repository", FAIL, "ECR_REPOSITORY_MISSING", repository=name)
            )
            continue
        items.append(
            entry("ecr.repository", PASS, "ECR_REPOSITORY_PRESENT", repository=name)
        )
        if section["scan_on_push"]:
            items.append(scan_on_push(repository, scanning, name))
        if "encryption" in section:
            actual = (repository.get("encryptionConfiguration") or {}).get(
                "encryptionType"
            )
            if actual == section["encryption"]:
                items.append(
                    entry(
                        "ecr.encryption", PASS, "ECR_ENCRYPTION_MATCH", repository=name
                    )
                )
            else:
                items.append(
                    entry(
                        "ecr.encryption",
                        FAIL,
                        "ECR_ENCRYPTION_MISMATCH",
                        repository=name,
                        expected=section["encryption"],
                        actual=actual if isinstance(actual, str) else None,
                    )
                )
    return items


def secret_store(clients, section, config):
    items = []
    try:
        aws(clients("secretsmanager").list_secrets, MaxResults=1)
    except AwsError as error:
        items.append(entry("secretsmanager.list", ERROR, error.code))
    else:
        items.append(entry("secretsmanager.list", PASS, "SECRETS_MANAGER_REACHABLE"))
    try:
        meta = (
            aws(
                clients("kms").describe_key,
                missing={"NotFoundException"},
                KeyId=section["kms_key"],
            ).get("KeyMetadata")
            or {}
        )
    except AwsError as error:
        if error.missing:
            items.append(entry("kms.key", FAIL, "KMS_KEY_MISSING"))
        else:
            items.append(entry("kms.key", ERROR, error.code))
        return items
    state = meta.get("KeyState")
    spec = meta.get("KeySpec") or meta.get("CustomerMasterKeySpec")
    if meta.get("Enabled") is not True or state != "Enabled":
        items.append(
            entry(
                "kms.key",
                FAIL,
                "KMS_KEY_NOT_ENABLED",
                key_state=state if isinstance(state, str) else None,
            )
        )
    elif meta.get("KeyUsage") != "ENCRYPT_DECRYPT" or spec != "SYMMETRIC_DEFAULT":
        items.append(entry("kms.key", FAIL, "KMS_KEY_NOT_SYMMETRIC"))
    else:
        items.append(
            entry(
                "kms.key", PASS, "KMS_KEY_ENABLED", key_manager=meta.get("KeyManager")
            )
        )
    return items


def identity(clients, section, config):
    sso = clients("sso-admin", section["region"])
    try:
        instances = list(pages(sso.list_instances, "Instances"))
    except AwsError as error:
        return [entry("identity.instance", ERROR, error.code)]
    if "instance_arn" in section:
        instances = [
            i for i in instances if i.get("InstanceArn") == section["instance_arn"]
        ]
        if not instances:
            return [
                entry("identity.instance", FAIL, "IDENTITY_CENTER_INSTANCE_MISSING")
            ]
    if not instances:
        return [entry("identity.instance", FAIL, "IDENTITY_CENTER_MISSING")]
    if len(instances) > 1:
        return [entry("identity.instance", FAIL, "IDENTITY_CENTER_INSTANCE_AMBIGUOUS")]
    instance = instances[0]
    if instance.get("Status", "ACTIVE") != "ACTIVE":
        return [entry("identity.instance", FAIL, "IDENTITY_CENTER_NOT_ACTIVE")]
    items = [entry("identity.instance", PASS, "IDENTITY_CENTER_PRESENT")]
    expected = section["permission_sets"]
    if not expected:
        return items
    arn = instance["InstanceArn"]
    found = set()
    try:
        for permission_set in pages(
            sso.list_permission_sets, "PermissionSets", InstanceArn=arn
        ):
            reply = aws(
                sso.describe_permission_set,
                InstanceArn=arn,
                PermissionSetArn=permission_set,
            )
            found.add((reply.get("PermissionSet") or {}).get("Name"))
            if found.issuperset(expected):
                break
    except AwsError as error:
        return items + [entry("identity.permission_sets", ERROR, error.code)]
    for name in expected:
        if name in found:
            items.append(
                entry(
                    "identity.permission_set",
                    PASS,
                    "IDENTITY_PERMISSION_SET_PRESENT",
                    permission_set=name,
                )
            )
        else:
            items.append(
                entry(
                    "identity.permission_set",
                    FAIL,
                    "IDENTITY_PERMISSION_SET_MISSING",
                    permission_set=name,
                )
            )
    return items


def audit_log(clients, section, config):
    trail_client = clients("cloudtrail")
    try:
        trails = (
            aws(trail_client.describe_trails, includeShadowTrails=True).get("trailList")
            or []
        )
    except AwsError as error:
        return [entry("cloudtrail.trail", ERROR, error.code)]
    if not trails:
        return [entry("cloudtrail.trail", FAIL, "CLOUDTRAIL_TRAIL_MISSING")]
    logging, qualifying, errors = 0, 0, []
    for trail in trails:
        try:
            status = aws(
                trail_client.get_trail_status,
                Name=trail.get("TrailARN") or trail.get("Name"),
            )
        except AwsError as error:
            errors.append(error.code)
            continue
        if status.get("IsLogging") is not True:
            continue
        logging += 1
        if trail.get("IsMultiRegionTrail") is True or not section["multi_region"]:
            qualifying += 1
    detail = {"trails": len(trails), "logging": logging}
    if qualifying:
        return [entry("cloudtrail.trail", PASS, "CLOUDTRAIL_LOGGING", **detail)]
    if errors:
        return [entry("cloudtrail.trail", ERROR, errors[0], **detail)]
    if logging:
        return [
            entry("cloudtrail.trail", FAIL, "CLOUDTRAIL_NOT_MULTI_REGION", **detail)
        ]
    return [entry("cloudtrail.trail", FAIL, "CLOUDTRAIL_NOT_LOGGING", **detail)]


def rds_region(instance, fallback):
    parts = str(instance.get("DBInstanceArn") or "").split(":")
    return parts[3] if len(parts) > 3 and parts[3] else fallback


def data_target(clients, section, config):
    items = []
    if "rds_instance" in section:
        items.extend(rds(clients, section, config))
    if "eks_cluster" in section:
        items.append(eks(clients, section))
    return items


def rds(clients, section, config):
    name = section["rds_instance"]
    try:
        found = (
            aws(
                clients("rds").describe_db_instances,
                missing={"DBInstanceNotFound", "DBInstanceNotFoundFault"},
                DBInstanceIdentifier=name,
            ).get("DBInstances")
            or []
        )
    except AwsError as error:
        if error.missing:
            return [entry("rds.instance", FAIL, "RDS_INSTANCE_MISSING")]
        return [entry("rds.instance", ERROR, error.code)]
    if len(found) != 1:
        return [entry("rds.instance", FAIL, "RDS_INSTANCE_MISSING")]
    instance = found[0]
    state = instance.get("DBInstanceStatus")
    items = [
        entry("rds.instance", PASS, "RDS_INSTANCE_AVAILABLE")
        if state == "available"
        else entry(
            "rds.instance",
            FAIL,
            "RDS_INSTANCE_NOT_AVAILABLE",
            db_status=state if isinstance(state, str) else None,
        )
    ]
    if instance.get("PubliclyAccessible") is False:
        items.append(entry("rds.public_access", PASS, "RDS_NOT_PUBLIC"))
    else:
        items.append(entry("rds.public_access", FAIL, "RDS_PUBLICLY_ACCESSIBLE"))
    if "approved_regions" in section:
        region = rds_region(instance, config["region"])
        if region in section["approved_regions"]:
            items.append(
                entry("rds.region", PASS, "RDS_REGION_APPROVED", region=region)
            )
        else:
            items.append(
                entry("rds.region", FAIL, "RDS_REGION_NOT_APPROVED", region=region)
            )
    return items


def eks(clients, section):
    try:
        cluster = (
            aws(
                clients("eks").describe_cluster,
                missing={"ResourceNotFoundException"},
                name=section["eks_cluster"],
            ).get("cluster")
            or {}
        )
    except AwsError as error:
        if error.missing:
            return entry("eks.cluster", FAIL, "EKS_CLUSTER_MISSING")
        return entry("eks.cluster", ERROR, error.code)
    state = cluster.get("status")
    if state == "ACTIVE":
        return entry("eks.cluster", PASS, "EKS_CLUSTER_ACTIVE")
    return entry(
        "eks.cluster",
        FAIL,
        "EKS_CLUSTER_NOT_ACTIVE",
        cluster_status=state if isinstance(state, str) else None,
    )


CHECKS = {
    "registry": registry,
    "secret_store": secret_store,
    "identity": identity,
    "audit_log": audit_log,
    "data_target": data_target,
}


def actions(name, section):
    if name == "registry":
        result = ["ecr:DescribeRegistry", "ecr:GetAuthorizationToken"]
        if section["repositories"]:
            result.append("ecr:DescribeRepositories")
        if section["scan_on_push"]:
            result.append("ecr:GetRegistryScanningConfiguration")
        return result
    if name == "secret_store":
        return ["secretsmanager:ListSecrets", "kms:DescribeKey"]
    if name == "identity":
        result = ["sso:ListInstances"]
        if section["permission_sets"]:
            result += ["sso:ListPermissionSets", "sso:DescribePermissionSet"]
        return result
    if name == "audit_log":
        return ["cloudtrail:DescribeTrails", "cloudtrail:GetTrailStatus"]
    result = []
    if "rds_instance" in section:
        result.append("rds:DescribeDBInstances")
    if "eks_cluster" in section:
        result.append("eks:DescribeCluster")
    return result
