variable "database_version" {
  description = "Cloud SQL PostgreSQL 버전(POSTGRES_15 이상)."
  type        = string
  default     = "POSTGRES_16"

  validation {
    condition     = can(regex("^POSTGRES_(1[5-9]|[2-9][0-9])$", var.database_version))
    error_message = "database_version 은 POSTGRES_15 이상이어야 한다."
  }
}

variable "db_tier" {
  description = "Cloud SQL 머신 등급. 시험용 최소 사양이 기본값이다."
  type        = string
  default     = "db-f1-micro"
}

variable "database_name" {
  description = "앱이 쓸 데이터베이스 이름."
  type        = string
  default     = "app"
}

variable "secret_names" {
  description = <<-EOT
    만들 Secret Manager 비밀의 짧은 이름 목록. 비밀 ID 는 <name_prefix>-<이름>
    이다. 그릇만 만들고 값(버전)은 넣지 않는다 — 값은 Terraform 밖에서 넣어
    state 에 남지 않게 한다.
  EOT
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for name in var.secret_names : can(regex("^[a-z0-9][a-z0-9-]{0,39}$", name))])
    error_message = "secret_names 의 각 이름은 영문 소문자·숫자·하이픈 1–40자여야 한다."
  }
}
