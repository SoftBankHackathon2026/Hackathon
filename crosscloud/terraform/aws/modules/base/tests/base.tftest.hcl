# 시험용 DB 의 고정 설정(사용자 결정 2026-11-07)과 해체·잔여 검사 전제를 plan
# 으로 확인한다.

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
  name_prefix       = "ccx-r1"
  vpc_cidr          = "10.80.0.0/16"
  db_engine_version = "16"
  db_instance_class = "db.t4g.micro"
  db_name           = "app"
  db_username       = "ccxadmin"
  secret_names      = ["database-url"]
  bot_image         = null
  bot_env           = {}
}

run "db_is_public_single_az_with_logical_replication_and_iam_auth" {
  command = plan

  assert {
    condition = (
      aws_db_instance.this.publicly_accessible
      && !aws_db_instance.this.multi_az
      && aws_db_instance.this.iam_database_authentication_enabled
    )
    error_message = "DB 는 공개·단일 AZ·IAM 인증 켬이어야 한다."
  }

  assert {
    condition = (
      aws_db_instance.this.backup_retention_period == 0
      && !aws_db_instance.this.deletion_protection
      && aws_db_instance.this.skip_final_snapshot
    )
    error_message = "시험 DB 는 백업·삭제 보호·최종 스냅샷을 끈다."
  }

  assert {
    condition     = aws_db_instance.this.manage_master_user_password
    error_message = "마스터 비밀번호는 RDS 가 Secrets Manager 에 만든다(state 에 평문 금지)."
  }

  assert {
    condition = anytrue([
      for p in aws_db_parameter_group.this.parameter :
      p.name == "rds.logical_replication" && p.value == "1"
    ])
    error_message = "파라미터 그룹에 rds.logical_replication=1 이 있어야 한다."
  }

  assert {
    condition     = aws_db_parameter_group.this.family == "postgres16"
    error_message = "파라미터 그룹 계열은 엔진 주 버전을 따른다."
  }

  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.db_postgres.cidr_ipv4 == "0.0.0.0/0"
      && aws_vpc_security_group_ingress_rule.db_postgres.from_port == 5432
    )
    error_message = "DB SG 는 5432 를 0.0.0.0/0 에 연다(사용자 결정)."
  }
}

run "secrets_are_empty_containers_deleted_without_recovery_window" {
  command = plan

  assert {
    condition     = aws_secretsmanager_secret.this["database-url"].name == "ccx-r1/database-url"
    error_message = "Secret 이름은 접두사 아래에 둔다."
  }

  assert {
    condition     = aws_secretsmanager_secret.this["database-url"].recovery_window_in_days == 0
    error_message = "복구 대기가 있으면 해체 뒤 잔여로 남는다."
  }

  assert {
    condition     = aws_ecr_repository.app.force_delete
    error_message = "이미지가 남아도 ECR 저장소를 지워야 한다."
  }
}

run "iam_role_lives_under_prefix_path_for_residue_scan" {
  command = plan

  assert {
    condition     = aws_iam_role.task_execution.path == "/ccx-r1/"
    error_message = "IAM 역할 경로가 접두사여야 잔여 검사가 찾는다."
  }

  assert {
    condition     = length(aws_ecs_task_definition.bot) == 0
    error_message = "봇 이미지가 없으면 봇 작업 정의를 만들지 않는다."
  }
}

run "bot_task_definition_when_image_given" {
  command = plan

  variables {
    bot_image = "kty1021/traffic-bot:latest"
    bot_env   = { TARGET_URL = "http://app.ccx-r1.internal" }
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.bot[0].container_definitions)[0].image == "kty1021/traffic-bot:latest"
    error_message = "봇 작업 정의는 주어진 이미지를 쓴다."
  }
}
