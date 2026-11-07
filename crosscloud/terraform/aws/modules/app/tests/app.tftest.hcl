# 앱 층의 진입(ALB HTTP 80)·공인 IP·Secret 주입을 plan 으로 확인한다.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  name_prefix             = "ccx-r1"
  vpc_id                  = "vpc-0123"
  subnet_ids              = ["subnet-a", "subnet-b"]
  cluster_arn             = "arn:aws:ecs:ap-northeast-2:111122223333:cluster/ccx-r1"
  log_group_name          = "/ecs/ccx-r1"
  task_execution_role_arn = "arn:aws:iam::111122223333:role/ccx-r1-exec"
  image                   = "kty1021/sample-app:abc123"
  port                    = 8080
  health_path             = "/healthz"
  env                     = { LOG_LEVEL = "info" }
  secret_env              = { DATABASE_URL = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ccx-r1-database-url" }
  desired_count           = 1
}

run "http_80_forwards_to_public_ip_fargate_tasks" {
  command = plan

  assert {
    condition     = aws_lb_listener.http.port == 80 && aws_lb_listener.http.default_action[0].type == "forward"
    error_message = "ALB 는 HTTP 80 에서 앱으로 보낸다."
  }

  assert {
    condition     = !aws_lb.this.internal
    error_message = "ALB 는 인터넷 연결형이다."
  }

  assert {
    condition     = one(aws_ecs_service.app.network_configuration).assign_public_ip
    error_message = "NAT 가 없으므로 작업에 공인 IP 를 준다."
  }
}

run "secrets_injected_by_reference_only" {
  command = plan

  assert {
    condition = (
      jsondecode(aws_ecs_task_definition.app.container_definitions)[0].secrets[0].valueFrom
      == "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:ccx-r1-database-url"
    )
    error_message = "DATABASE_URL 은 Secret 참조로 주입한다."
  }

  assert {
    condition = !contains(
      [for e in jsondecode(aws_ecs_task_definition.app.container_definitions)[0].environment : e.name],
      "DATABASE_URL",
    )
    error_message = "DB 접속 대상은 평문 env 로 렌더링하지 않는다."
  }
}
