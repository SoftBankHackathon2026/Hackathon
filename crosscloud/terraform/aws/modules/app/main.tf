# 앱 층: ALB(HTTP 80) → ECS Fargate 서비스. 작업은 퍼블릭 서브넷에서 공인 IP 로
# 나간다(NAT 없음).
#
# 리스너 기본 동작과 서비스의 작업 정의는 Terraform 이 처음 한 번만 정한다.
# 그 뒤에는 제어 어댑터가 바꾼다(리스너 503 ↔ forward, 대상 프로필 Secret 참조로
# 바꾼 작업 정의). 재 apply 가 이것을 원본으로 되돌리면 대상 DB 가 쓰기를 받은
# 뒤 앱이 원본 DB 로 돌아가는 롤백이 된다 — 불변식 위반이라 무시한다.

data "aws_region" "current" {}

data "aws_partition" "current" {}

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "HTTP from anywhere"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = var.port
  to_port                      = var.port
}

resource "aws_security_group" "app" {
  name        = "${var.name_prefix}-app"
  description = "App tasks behind the ALB"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = var.port
  to_port                      = var.port
}

resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.subnet_ids
}

resource "aws_lb_target_group" "app" {
  name                 = "${var.name_prefix}-app"
  target_type          = "ip"
  protocol             = "HTTP"
  port                 = var.port
  vpc_id               = var.vpc_id
  deregistration_delay = 10

  health_check {
    path    = var.health_path
    matcher = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }

  lifecycle {
    ignore_changes = [default_action]
  }
}

# IAM 역할 경로를 접두사로 두는 이유는 base 모듈 ecs.tf 와 같다(잔여 검사).
resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-task"
  path               = "/${var.name_prefix}/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "rds_connect" {
  statement {
    actions = ["rds-db:connect"]
    resources = [
      for user in var.iam_db_users :
      "arn:${data.aws_partition.current.partition}:rds-db:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:dbuser:${var.db_resource_id}/${user}"
    ]
  }
}

resource "aws_iam_role_policy" "rds_connect" {
  count = length(var.iam_db_users) > 0 ? 1 : 0

  name   = "rds-connect"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.rds_connect.json
}

resource "aws_ecs_task_definition" "app" {
  family                   = "${var.name_prefix}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = var.task_execution_role_arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "app"
    image        = var.image
    essential    = true
    portMappings = [{ containerPort = var.port, protocol = "tcp" }]
    environment = [
      for key, value in merge(var.env, { PORT = tostring(var.port) }) :
      { name = key, value = value }
    ]
    secrets = [
      for key, arn in var.secret_env : { name = key, valueFrom = arn }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = var.log_group_name
        awslogs-region        = data.aws_region.current.region
        awslogs-stream-prefix = "app"
      }
    }
  }])
}

resource "aws_ecs_service" "app" {
  name            = "${var.name_prefix}-app"
  cluster         = var.cluster_arn
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = var.port
  }

  depends_on = [aws_lb_listener.http]

  lifecycle {
    ignore_changes = [task_definition]
  }
}
