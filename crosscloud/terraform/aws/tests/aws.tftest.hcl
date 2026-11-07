# 자격 증명 없이 plan 만으로 방향별 역할·층 선택·DB 설정·변수 검사를 확인한다.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["ap-northeast-2a", "ap-northeast-2c"]
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  name_prefix = "ccx-r1"
  app_image   = "public.ecr.aws/docker/library/nginx:stable"
}

run "aws_to_gcp_is_one_source_fixture" {
  command = plan

  variables {
    direction = "aws_to_gcp"
  }

  assert {
    condition     = output.roles.base == "source_fixture" && output.roles.app == "source_fixture"
    error_message = "aws_to_gcp 에서는 두 층 모두 원본 고정물이다."
  }

  assert {
    condition     = output.name_prefix == "ccx-r1"
    error_message = "이름 접두사를 그대로 내보낸다(잔여 검사 입력)."
  }

  assert {
    condition     = length(module.app) == 1
    error_message = "원본 고정물은 앱 층을 함께 만든다."
  }

  assert {
    condition     = var.app_health_path == "/readyz"
    error_message = "상태 확인 기본값은 DB 를 읽는 /readyz 다."
  }
}

run "gcp_to_aws_base_only_until_app_is_added" {
  command = plan

  variables {
    direction   = "gcp_to_aws"
    include_app = false
    app_image   = null
  }

  assert {
    condition     = output.roles.base == "target_base" && output.roles.app == null
    error_message = "gcp_to_aws 대상 기반만 깔 때 앱 역할은 없다."
  }

  assert {
    condition     = length(module.app) == 0 && output.app == null
    error_message = "include_app = false 면 앱 층을 만들지 않는다."
  }
}

run "gcp_to_aws_app_layer_is_target_app" {
  command = plan

  variables {
    direction = "gcp_to_aws"
  }

  assert {
    condition     = output.roles.app == "target_app"
    error_message = "gcp_to_aws 의 앱 층은 대상 앱이다."
  }
}

run "app_layer_requires_image" {
  command = plan

  variables {
    direction = "aws_to_gcp"
    app_image = null
  }

  expect_failures = [var.app_image]
}

run "secret_env_must_point_at_a_declared_secret" {
  command = plan

  variables {
    direction      = "aws_to_gcp"
    app_secret_env = { DATABASE_URL = "undeclared" }
  }

  expect_failures = [var.app_secret_env]
}

run "rejects_postgres_older_than_15" {
  command = plan

  variables {
    direction         = "aws_to_gcp"
    db_engine_version = "14.12"
  }

  expect_failures = [var.db_engine_version]
}

run "rejects_name_prefix_outside_run_pattern" {
  command = plan

  variables {
    direction   = "aws_to_gcp"
    name_prefix = "ccx-run-1"
  }

  expect_failures = [var.name_prefix]
}
