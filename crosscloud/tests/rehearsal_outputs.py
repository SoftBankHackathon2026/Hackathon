"""리허설 시험용 Terraform 출력 — `terraform output -json` 의 value 들.

crosscloud.provision.terraform.Terraform.outputs() 가 돌려주는 모양(이름 →
value)이다. 값은 실제 자원 이름 규칙(ccx-<run>-…)을 따른 합성값이다.
"""

RUN = "r1107a"
PREFIX = f"ccx-{RUN}"
RETURN_PREFIX = f"ccx-{RUN}c"
ACCOUNT = "187478112557"
PROJECT = "ghost-project-513812"


def _aws(prefix, *, db_name, secret_names, with_app):
    arn = f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret"
    outputs = {
        "name_prefix": prefix,
        "region": "ap-northeast-2",
        "roles": {
            "base": "source_fixture",
            "app": "source_fixture" if with_app else None,
        },
        "vpc_id": "vpc-0a1b2c3d4e5f60718",
        "public_subnet_ids": ["subnet-0aa11", "subnet-0bb22"],
        "db": {
            "identifier": f"{prefix}-db",
            "address": f"{prefix}-db.c9xyzabc1234.ap-northeast-2"
            ".rds.amazonaws.com",
            "port": 5432,
            "name": db_name,
            "username": "ccxadmin",
            "resource_id": "db-ABCDEFGHIJKLMNOP",
            "master_user_secret_arn": f"{arn}:rds!db-0f1e2d3c-AbCdEf",
        },
        "secret_arns": {
            name: f"{arn}:{prefix}-{name}-Xy12Zq" for name in secret_names
        },
        "ecr_repository_url": f"{ACCOUNT}.dkr.ecr.ap-northeast-2"
        f".amazonaws.com/{prefix}-app",
        "cluster_arn": f"arn:aws:ecs:ap-northeast-2:{ACCOUNT}:cluster/"
        f"{prefix}-cluster",
        "app": None,
    }
    if with_app:
        outputs["app"] = {
            "service_name": f"{prefix}-app",
            "task_definition_arn": f"arn:aws:ecs:ap-northeast-2:{ACCOUNT}:"
            f"task-definition/{prefix}-app:1",
            "alb_arn": "arn:aws:elasticloadbalancing:...:loadbalancer/app/x",
            "alb_dns_name": f"{prefix}-alb-1234567890.ap-northeast-2"
            ".elb.amazonaws.com",
            "listener_arn": "arn:aws:elasticloadbalancing:...:listener/x",
            "target_group_arn": "arn:aws:elasticloadbalancing:...:tg/x",
            "security_group_id": "sg-0123456789abcdef0",
        }
    return outputs


AWS = _aws(
    PREFIX,
    db_name="shop",
    secret_names=(
        "database-url",
        "database-url-c",
        "aws-admin-password",
        "aws-cdc-password",
        "aws-verify-password",
    ),
    with_app=True,
)
AWS_RETURN = _aws(
    RETURN_PREFIX,
    db_name="shop_c",
    secret_names=(
        "aws-admin-password",
        "aws-app-password",
        "aws-sink-password",
        "aws-verify-password",
    ),
    with_app=False,
)
REGISTRY = {
    "repository_id": f"{PREFIX}-images",
    "repository_url": f"asia-northeast3-docker.pkg.dev/{PROJECT}/"
    f"{PREFIX}-images",
}
GCP_BASE = {
    "db_instance_name": f"{PREFIX}-db",
    "db_connection_name": f"{PROJECT}:asia-northeast3:{PREFIX}-db",
    "db_public_ip": "34.64.10.20",
    "database_name": "shop_b",
    "secret_ids": {
        name: f"{PREFIX}-{name}"
        for name in (
            "database-url",
            "database-url-b",
            "postgres-password",
            "gcp-admin-password",
            "gcp-app-password",
            "gcp-cdc-password",
            "gcp-sink-password",
            "gcp-verify-password",
        )
    },
}
GCP_APP = {
    "service_name": f"{PREFIX}-app",
    "service_uri": f"https://{PREFIX}-app-abcdefghij-du.a.run.app",
    "lb_ip_address": "34.120.1.2",
    "runtime_service_account": f"{PREFIX}-run@{PROJECT}"
    ".iam.gserviceaccount.com",
}
IMAGE = (
    "docker.io/kty1021/sample-app@sha256:"
    "4e219077ff61e90dab00762e81e1ab97bc4344b06a9dc9af2f9ec19aade0f41b"
)
