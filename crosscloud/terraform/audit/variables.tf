variable "name_prefix" {
  description = <<-EOT
    실행 접두사 ccx-<run>. 버킷 이름이 이것으로 시작하고, 잔여 검사가 이것으로
    질의한다. run 은 영문 소문자·숫자 1–16자다. 이관 객체도 버킷 안
    <name_prefix>/ 아래에 둔다(crosscloud.audit_copy.archive).
  EOT
  type        = string

  validation {
    condition     = can(regex("^ccx-[a-z0-9]{1,16}$", var.name_prefix))
    error_message = "name_prefix 는 ccx-<영문 소문자·숫자 1–16자> 여야 한다."
  }
}

variable "direction" {
  description = "이 실행의 이전 방향. 라벨로만 쓴다(버킷은 두 방향 모두 만든다)."
  type        = string

  validation {
    condition     = contains(["aws_to_gcp", "gcp_to_aws"], var.direction)
    error_message = "direction 은 aws_to_gcp 또는 gcp_to_aws 여야 한다."
  }
}

variable "project_id" {
  description = "GCS 보존 버킷(AWS→GCP 대상)을 만들 GCP 프로젝트 ID."
  type        = string
}

variable "region" {
  description = "GCS 보존 버킷 위치(GCP 리전)."
  type        = string
  default     = "asia-northeast3"
}

variable "aws_region" {
  description = "S3 보존 버킷(GCP→AWS 대상) 리전."
  type        = string
  default     = "ap-northeast-2"
}

variable "gcs_retention_seconds" {
  description = <<-EOT
    GCS 보존 정책 기간(초). 정책은 잠그지 않는다(평가 20 D7 — 시연·샌드박스는
    잠그지 않음). 객체는 이 기간이 지나야 지워지므로 해체가 막히지 않게 짧게 둔다.
  EOT
  type        = number
  default     = 60
}

locals {
  labels = {
    "crosscloud-run"       = var.name_prefix
    "crosscloud-direction" = var.direction
  }
}
