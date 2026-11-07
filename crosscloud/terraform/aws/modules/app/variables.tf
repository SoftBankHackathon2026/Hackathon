variable "name_prefix" {
  description = "모든 자원 이름 접두사(ccx-<run>)."
  type        = string
}

variable "vpc_id" {
  description = "앱을 둘 VPC."
  type        = string
}

variable "subnet_ids" {
  description = "ALB 와 앱 작업을 둘 퍼블릭 서브넷."
  type        = list(string)
}

variable "cluster_arn" {
  description = "ECS 클러스터."
  type        = string
}

variable "log_group_name" {
  description = "ECS 로그 그룹."
  type        = string
}

variable "task_execution_role_arn" {
  description = "ECS 작업 실행 역할."
  type        = string
}

variable "db_resource_id" {
  description = "IAM DB 인증 정책에 쓰는 RDS 자원 ID."
  type        = string
}

variable "image" {
  description = "앱 컨테이너 이미지."
  type        = string
}

variable "port" {
  description = "앱 컨테이너 포트."
  type        = number
}

variable "health_path" {
  description = "ALB 상태 확인 경로."
  type        = string
}

variable "env" {
  description = "평문 env."
  type        = map(string)
}

variable "secret_env" {
  description = "env 이름 → Secret ARN."
  type        = map(string)
}

variable "desired_count" {
  description = "앱 작업 수."
  type        = number
}

variable "iam_db_users" {
  description = "rds-db:connect 를 줄 DB 사용자."
  type        = list(string)
}
