# ECS 클러스터·작업 실행 역할·로그와 트래픽 봇 작업 정의. 봇은 이 VPC 안에서
# 돌아야 비공개 호스팅 영역 이름을 푼다. 실행은 run-task 로 한다(공인 IP 켬).

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_ecs_cluster" "this" {
  name = var.name_prefix
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${var.name_prefix}"
  retention_in_days = 1
}

# IAM 은 전역이라 리전 태그 질의에 안 잡힌다. 경로를 접두사로 두어 잔여
# 검사가 경로 접두사 질의로 찾게 한다(crosscloud.provision.residue_aws).
resource "aws_iam_role" "task_execution" {
  name               = "${var.name_prefix}-exec"
  path               = "/${var.name_prefix}/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for secret in aws_secretsmanager_secret.this : secret.arn]
  }
}

resource "aws_iam_role_policy" "read_secrets" {
  count = length(var.secret_names) > 0 ? 1 : 0

  name   = "read-secrets"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_security_group" "bot" {
  name        = "${var.name_prefix}-bot"
  description = "Traffic bot egress only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_egress_rule" "bot_all" {
  security_group_id = aws_security_group.bot.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_ecs_task_definition" "bot" {
  count = var.bot_image == null ? 0 : 1

  family                   = "${var.name_prefix}-bot"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn

  container_definitions = jsonencode([{
    name        = "bot"
    image       = var.bot_image
    essential   = true
    environment = [for key, value in var.bot_env : { name = key, value = value }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.this.name
        awslogs-region        = data.aws_region.current.region
        awslogs-stream-prefix = "bot"
      }
    }
  }])
}

data "aws_region" "current" {}
