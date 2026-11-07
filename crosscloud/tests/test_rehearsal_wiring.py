import pathlib

import pytest
import rehearsal_outputs as tf

from crosscloud import handoff
from crosscloud.rehearsal import config, wiring

CFG = config.RehearsalConfig(run=tf.RUN, source_image=tf.IMAGE)


def test_config_rejects_run_too_long_for_return_prefix():
    # 귀환 스택 접두사 ccx-<run>c 도 16자 규칙(residue)에 들어가야 한다.
    with pytest.raises(ValueError, match="run"):
        config.RehearsalConfig(run="a" * 16, source_image=tf.IMAGE)


def test_config_rejects_image_without_digest():
    with pytest.raises(ValueError, match="digest"):
        config.RehearsalConfig(
            run=tf.RUN, source_image="docker.io/kty1021/sample-app:363eeb9"
        )


def test_config_round_trips_through_state_file(tmp_path):
    path = tmp_path / "config.json"

    config.save(CFG, path)

    assert config.load(path) == CFG


def test_forward_handoff_maps_source_alb_and_target_cloud_run():
    result = wiring.forward_handoff(
        CFG, aws=tf.AWS, gcp_base=tf.GCP_BASE, gcp_app=tf.GCP_APP
    )

    assert result.source_app_url == (
        "http://ccx-r1107a-alb-1234567890.ap-northeast-2.elb.amazonaws.com"
    )
    assert result.target_app_url == tf.GCP_APP["service_uri"]
    assert result.target_database == "shop_b"


def test_forward_handoff_tf_documents_feed_db_from_terraform():
    # render-handoff 가 --source-tf·--target-tf 로 읽는 계약 그대로다.
    result = wiring.forward_handoff(
        CFG, aws=tf.AWS, gcp_base=tf.GCP_BASE, gcp_app=tf.GCP_APP
    )

    source = handoff.db_from_terraform("aws", result.source_tf)
    target = handoff.db_from_terraform(
        "gcp", result.target_tf, database=result.target_database
    )

    assert source.instance_id == "ccx-r1107a-db"
    assert source.endpoint.host == tf.AWS["db"]["address"]
    assert source.endpoint.database == "shop"
    assert target.endpoint.host == "34.64.10.20"
    assert target.endpoint.database == "shop_b"


def test_forward_deploy_spec_points_cloud_run_at_profile_b():
    result = wiring.forward_handoff(
        CFG, aws=tf.AWS, gcp_base=tf.GCP_BASE, gcp_app=tf.GCP_APP
    )

    assert result.deploy(profile_version="3") == {
        "platform": "cloud_run",
        "service": "projects/ghost-project-513812/locations/"
        "asia-northeast3/services/ccx-r1107a-app",
        "secret_env": {
            "DATABASE_URL": {
                "secret": "ccx-r1107a-database-url-b",
                "version": "3",
            }
        },
    }


def test_return_handoff_swaps_sides_and_targets_new_rds():
    result = wiring.return_handoff(
        CFG,
        aws=tf.AWS,
        aws_return=tf.AWS_RETURN,
        gcp_base=tf.GCP_BASE,
        gcp_app=tf.GCP_APP,
    )

    source = handoff.db_from_terraform("gcp", result.source_tf)
    target = handoff.db_from_terraform(
        "aws", result.target_tf, database=result.target_database
    )
    assert source.endpoint.database == "shop_b"
    assert target.instance_id == "ccx-r1107ac-db"
    assert target.endpoint.database == "shop_c"
    assert result.source_app_url == tf.GCP_APP["service_uri"]
    assert result.target_app_url.startswith("http://ccx-r1107a-alb-")


def test_return_deploy_spec_redeploys_original_ecs_with_profile_c():
    result = wiring.return_handoff(
        CFG,
        aws=tf.AWS,
        aws_return=tf.AWS_RETURN,
        gcp_base=tf.GCP_BASE,
        gcp_app=tf.GCP_APP,
    )

    assert result.deploy(profile_version=None) == {
        "platform": "ecs",
        "service": "ccx-r1107a-app",
        "cluster": tf.AWS["cluster_arn"],
        "region": "ap-northeast-2",
        "secret_env": {
            "DATABASE_URL": {"secret": tf.AWS["secret_arns"]["database-url-c"]}
        },
    }


def test_render_handoff_argv_uses_job_files_and_app_urls():
    result = wiring.forward_handoff(
        CFG, aws=tf.AWS, gcp_base=tf.GCP_BASE, gcp_app=tf.GCP_APP
    )
    files = config.StatePaths(pathlib.Path("/s")).job("forward")

    argv = wiring.render_handoff_argv(CFG, result, files)

    assert argv[0] == "render-handoff"
    pairs = dict(zip(argv[1::2], argv[2::2], strict=False))
    assert pairs["--source-tf"] == "/s/forward/source-tf.json"
    assert pairs["--target-database"] == "shop_b"
    assert pairs["--table"] == "public.migration_demo=request_id"
    assert pairs["--source-app-user"] == "shop_app"
    assert pairs["--target-app-url"] == tf.GCP_APP["service_uri"]
    assert pairs["--control-url"] == "http://127.0.0.1:18080"
    assert pairs["--out-dir"] == "/s/forward/out"


def test_forward_plans_use_handoff_login_names_and_stack_secrets():
    source, target = wiring.forward_plans(CFG, aws=tf.AWS, gcp_base=tf.GCP_BASE)

    assert source.cloud == "aws"
    assert target.cloud == "gcp"
    assert source.endpoint.host == tf.AWS["db"]["address"]
    assert target.endpoint.database == "shop_b"
    assert source.login("cdc").name == "migration_cdc"
    assert source.login("cdc").secret_id == "ccx-r1107a-aws-cdc-password"
    assert target.login("app").name == "shop_app"
    assert target.login("sink").secret_id == "ccx-r1107a-gcp-sink-password"
    assert target.app_profile_secret_id == "ccx-r1107a-database-url-b"


def test_return_plans_target_new_rds_and_write_profile_into_first_stack():
    source, target = wiring.return_plans(
        CFG, gcp_base=tf.GCP_BASE, aws_return=tf.AWS_RETURN
    )

    assert source.cloud == "gcp"
    assert source.login("admin").secret_id == "ccx-r1107a-gcp-admin-password"
    assert target.endpoint.host == tf.AWS_RETURN["db"]["address"]
    assert target.endpoint.database == "shop_c"
    assert target.login("admin").secret_id == ("ccx-r1107ac-aws-admin-password")
    # ECS 실행 역할은 첫 스택의 Secret 만 읽는다(terraform/aws README).
    assert target.app_profile_secret_id == "ccx-r1107a-database-url-c"


def test_plan_secret_names_are_declared_by_terraform_vars():
    # datapath 가 쓰는 Secret 그릇은 Terraform 이 미리 만들어야 한다.
    declared = {
        "aws": set(wiring.aws_source_tfvars(CFG)["secret_names"]),
        "aws_return": set(wiring.aws_return_tfvars(CFG)["secret_names"]),
        "gcp": set(wiring.target_base_tfvars(CFG)["secret_names"]),
    }
    forward = wiring.forward_plans(CFG, aws=tf.AWS, gcp_base=tf.GCP_BASE)
    back = wiring.return_plans(
        CFG, gcp_base=tf.GCP_BASE, aws_return=tf.AWS_RETURN
    )
    for plan in (*forward, *back):
        ids = [x.secret_id for x in plan.logins]
        ids += (
            [plan.app_profile_secret_id] if plan.app_profile_secret_id else []
        )
        for secret_id in ids:
            if secret_id.startswith(tf.RETURN_PREFIX + "-"):
                key = secret_id.removeprefix(tf.RETURN_PREFIX + "-")
                assert key in declared["aws_return"], secret_id
            else:
                key = secret_id.removeprefix(tf.PREFIX + "-")
                assert key in declared[plan.cloud], secret_id


def test_aws_source_tfvars_add_app_only_with_image():
    base = wiring.aws_source_tfvars(CFG)
    full = wiring.aws_source_tfvars(
        CFG, app_image="repo@sha256:ab", db_ssl_ca="PEM"
    )

    assert base["include_app"] is False
    assert "app_image" not in base
    assert base["db_name"] == "shop"
    assert base["direction"] == "aws_to_gcp"
    assert full["include_app"] is True
    assert full["app_image"] == "repo@sha256:ab"
    assert full["app_db_ssl_ca"] == "PEM"
    assert full["app_env"] == {"PGSSLMODE": "verify-full"}


def test_aws_return_tfvars_is_base_only_target_with_new_prefix():
    tfvars = wiring.aws_return_tfvars(CFG)

    assert tfvars["name_prefix"] == "ccx-r1107ac"
    assert tfvars["direction"] == "gcp_to_aws"
    assert tfvars["include_app"] is False
    assert tfvars["db_name"] == "shop_c"
    assert tfvars["app_secret_env"] == {}


def test_target_app_tfvars_pin_transferred_profile_a_version():
    tfvars = wiring.target_app_tfvars(
        CFG, image="ar/app@sha256:ab", db_ssl_ca="PEM", database_url_version="2"
    )

    assert tfvars["image"] == "ar/app@sha256:ab"
    assert tfvars["env"] == {"PGSSLMODE": "verify-full", "DB_SSL_CA": "PEM"}
    assert tfvars["secret_env"] == {
        "DATABASE_URL": {"secret": "ccx-r1107a-database-url", "version": "2"}
    }
    assert tfvars["project_id"] == "ghost-project-513812"


def test_copied_image_keeps_source_digest():
    tag, pinned = wiring.copied_image(tf.REGISTRY["repository_url"], tf.IMAGE)

    digest = tf.IMAGE.split("@")[1]
    assert pinned == f"{tf.REGISTRY['repository_url']}/sample-app@{digest}"
    assert tag == (
        f"{tf.REGISTRY['repository_url']}/sample-app:src-{digest[7:19]}"
    )


def test_ecr_copy_uses_repository_itself_as_image_name():
    tag, pinned = wiring.copied_image(tf.AWS["ecr_repository_url"], tf.IMAGE)

    assert pinned.startswith(tf.AWS["ecr_repository_url"] + "@sha256:")
    assert tag.startswith(tf.AWS["ecr_repository_url"] + ":src-")


def test_registry_host():
    assert wiring.registry_host(tf.AWS["ecr_repository_url"]) == (
        "187478112557.dkr.ecr.ap-northeast-2.amazonaws.com"
    )


def test_password_exports_read_secrets_at_eval_time_not_values():
    lines = wiring.password_exports(CFG, "forward")

    assert lines == (
        'export AWS_ADMIN_PASSWORD="$(aws secretsmanager get-secret-value'
        " --region ap-northeast-2 --secret-id ccx-r1107a-aws-admin-password"
        ' --query SecretString --output text)"',
        'export AWS_CDC_PASSWORD="$(aws secretsmanager get-secret-value'
        " --region ap-northeast-2 --secret-id ccx-r1107a-aws-cdc-password"
        ' --query SecretString --output text)"',
        'export AWS_VERIFY_PASSWORD="$(aws secretsmanager get-secret-value'
        " --region ap-northeast-2 --secret-id ccx-r1107a-aws-verify-password"
        ' --query SecretString --output text)"',
        'export GCP_ADMIN_PASSWORD="$(gcloud secrets versions access latest'
        " --project ghost-project-513812"
        ' --secret ccx-r1107a-gcp-admin-password)"',
        'export GCP_SINK_PASSWORD="$(gcloud secrets versions access latest'
        " --project ghost-project-513812"
        ' --secret ccx-r1107a-gcp-sink-password)"',
        'export GCP_VERIFY_PASSWORD="$(gcloud secrets versions access latest'
        " --project ghost-project-513812"
        ' --secret ccx-r1107a-gcp-verify-password)"',
    )


def test_return_password_exports_use_return_stack_for_aws_target():
    lines = wiring.password_exports(CFG, "return")

    assert any("ccx-r1107a-gcp-cdc-password" in x for x in lines)
    assert any("ccx-r1107ac-aws-sink-password" in x for x in lines)
    assert not any("AWS_CDC_PASSWORD" in x for x in lines)


def test_aws_stacks_pin_allowed_account():
    for tfvars in (
        wiring.aws_source_tfvars(CFG),
        wiring.aws_source_tfvars(CFG, app_image="repo@sha256:ab"),
        wiring.aws_return_tfvars(CFG),
        wiring.audit_tfvars(CFG),
    ):
        assert tfvars["allowed_account_ids"] == ["187478112557"]


def test_target_app_tfvars_grant_profile_b_read_for_warmup():
    tfvars = wiring.target_app_tfvars(
        CFG, image="ar/app@sha256:ab", db_ssl_ca="PEM", database_url_version="2"
    )

    assert tfvars["readable_secrets"] == ["ccx-r1107a-database-url-b"]
