variable "run_id" {
  description = "실행 식별자. 영역 이름 ccx-<run_id>.internal 과 태그 crosscloud-run 값이 된다."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{1,12}$", var.run_id))
    error_message = "run_id 는 소문자·숫자 1~12자여야 한다."
  }
}

variable "direction" {
  description = "이전 방향. 원본 쪽 레코드가 가중치 100 으로 시작한다."
  type        = string

  validation {
    condition     = contains(["aws_to_gcp", "gcp_to_aws"], var.direction)
    error_message = "direction 은 aws_to_gcp 또는 gcp_to_aws 여야 한다."
  }
}

variable "region" {
  description = "VPC 가 있는 AWS 리전."
  type        = string
  default     = "ap-northeast-2"
}

variable "vpc_id" {
  description = "영역을 붙일 VPC(트래픽 봇이 도는 곳)."
  type        = string
}

variable "record_name" {
  description = "영역 안의 레코드 이름(앞부분)."
  type        = string
  default     = "app"
}

variable "ttl" {
  description = "GCP 쪽 A 레코드 TTL(초). 짧을수록 가중치 변경이 빨리 반영된다."
  type        = number
  default     = 10
}

variable "aws_alb_dns_name" {
  description = "AWS 앱 ALB 의 DNS 이름(별칭 대상)."
  type        = string
}

variable "aws_alb_zone_id" {
  description = "AWS 앱 ALB 의 호스팅 영역 ID."
  type        = string
}

variable "gcp_lb_ip" {
  description = "GCP 앱 외부 LB 의 IPv4 주소."
  type        = string

  validation {
    condition     = can(cidrhost("${var.gcp_lb_ip}/32", 0))
    error_message = "gcp_lb_ip 는 IPv4 주소여야 한다."
  }
}
