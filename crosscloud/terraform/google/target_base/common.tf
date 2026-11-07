# 세 GCP 모듈(registry·target_base·target_app)이 같은 모양으로 받는 입력.

variable "project_id" {
  description = "자원을 만들 GCP 프로젝트 ID."
  type        = string
}

variable "region" {
  description = "자원을 만들 GCP 리전."
  type        = string
  default     = "asia-northeast3"
}

variable "name_prefix" {
  description = <<-EOT
    실행 접두사 ccx-<run>. 모든 자원 이름이 이것으로 시작하고, 잔여 검사가
    이것으로 질의한다. run 은 영문 소문자·숫자 1–16자(crosscloud.provision.
    residue.NAME_PREFIX_PATTERN 과 같다). Cloud SQL 인스턴스 이름은 지운 뒤
    한동안 다시 못 쓰므로 실행마다 새 run 을 쓴다.
  EOT
  type        = string

  validation {
    condition     = can(regex("^ccx-[a-z0-9]{1,16}$", var.name_prefix))
    error_message = "name_prefix 는 ccx-<영문 소문자·숫자 1–16자> 여야 한다."
  }
}

variable "direction" {
  description = <<-EOT
    이전 방향. aws_to_gcp 면 이 모듈은 이전 대상이고, gcp_to_aws 면 GCP→AWS
    시험의 원본 고정물이다.
  EOT
  type        = string

  validation {
    condition     = contains(["aws_to_gcp", "gcp_to_aws"], var.direction)
    error_message = "direction 은 aws_to_gcp 또는 gcp_to_aws 여야 한다."
  }
}

locals {
  labels = {
    "crosscloud-run"       = var.name_prefix
    "crosscloud-direction" = var.direction
  }
}
