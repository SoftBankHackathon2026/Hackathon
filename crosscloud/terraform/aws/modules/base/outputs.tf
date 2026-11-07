output "vpc_id" {
  description = "전용 VPC."
  value       = aws_vpc.this.id
}

output "public_subnet_ids" {
  description = "퍼블릭 서브넷 2개."
  value       = aws_subnet.public[*].id
}

output "db" {
  description = "RDS 접속 정보(비밀값 없음)."
  value = {
    identifier             = aws_db_instance.this.identifier
    address                = aws_db_instance.this.address
    port                   = aws_db_instance.this.port
    name                   = aws_db_instance.this.db_name
    username               = aws_db_instance.this.username
    resource_id            = aws_db_instance.this.resource_id
    master_user_secret_arn = aws_db_instance.this.master_user_secret[0].secret_arn
  }
}

output "db_resource_id" {
  description = "IAM DB 인증 정책(rds-db:connect)에 쓰는 자원 ID."
  value       = aws_db_instance.this.resource_id
}

output "secret_arns" {
  description = "Secret 그릇 이름 → ARN."
  value       = { for name, secret in aws_secretsmanager_secret.this : name => secret.arn }
}

output "ecr_repository_url" {
  description = "앱 이미지 저장소."
  value       = aws_ecr_repository.app.repository_url
}

output "cluster_arn" {
  description = "ECS 클러스터."
  value       = aws_ecs_cluster.this.arn
}

output "log_group_name" {
  description = "ECS 로그 그룹."
  value       = aws_cloudwatch_log_group.this.name
}

output "task_execution_role_arn" {
  description = "ECS 작업 실행 역할(이미지 받기·Secret 읽기·로그)."
  value       = aws_iam_role.task_execution.arn
}

output "bot" {
  description = "트래픽 봇 run-task 에 필요한 값. 봇 이미지가 없으면 null."
  value = var.bot_image == null ? null : {
    task_definition_arn = aws_ecs_task_definition.bot[0].arn
    security_group_id   = aws_security_group.bot.id
    subnet_ids          = aws_subnet.public[*].id
  }
}
