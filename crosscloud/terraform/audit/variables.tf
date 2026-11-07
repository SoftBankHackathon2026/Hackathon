variable "run_id" {
  description = "이관 실행 식별자. 버킷 이름 접두사 ccx-<run_id> 와 라벨에 들어간다."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,19}$", var.run_id))
    error_message = "run_id 는 소문자·숫자·하이픈 20자 이하이고 하이픈으로 시작하지 않는다."
  }
}

variable "aws_region" {
  description = "S3 보존 버킷(GCP→AWS 대상) 리전."
  type        = string
  default     = "ap-northeast-2"
}

variable "gcp_project" {
  description = "GCS 보존 버킷(AWS→GCP 대상) 프로젝트."
  type        = string
}

variable "gcp_location" {
  description = "GCS 보존 버킷 위치."
  type        = string
  default     = "ASIA-NORTHEAST3"
}

variable "gcs_retention_seconds" {
  description = <<-EOT
    GCS 보존 정책 기간(초). 정책은 잠그지 않는다(평가 20 D7 — 시연·샌드박스는
    잠그지 않음). 객체는 이 기간이 지나야 지워지므로 해체가 막히지 않게 짧게 둔다.
  EOT
  type        = number
  default     = 60
}
