# ECS 클러스터·작업 실행 역할·로그. 대상 앱(gcp_to_aws)을 나중에 더해도 같은
# 클러스터·역할을 쓰도록 기반 층에 둔다.

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

# IAM 은 전역이라 리전 태그 질의에 안 잡힌다. 잔여 검사는 역할 이름 접두사로
# 찾는다(crosscloud.provision.residue_aws).
resource "aws_iam_role" "task_execution" {
  name               = "${var.name_prefix}-exec"
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
