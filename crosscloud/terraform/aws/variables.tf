variable "name_prefix" {
  description = "실행 접두사 ccx-<run>. 모든 자원 이름이 이것으로 시작하고 태그 crosscloud-run 값이 된다. run 은 영문 소문자·숫자 1–16자(crosscloud.provision.residue 와 같다)."
  type        = string

  validation {
    condition     = can(regex("^ccx-[a-z0-9]{1,16}$", var.name_prefix))
    error_message = "name_prefix 는 ccx-<영문 소문자·숫자 1–16자> 여야 한다."
  }
}

variable "direction" {
  description = "이전 방향. aws_to_gcp 면 이 구성이 원본 고정물, gcp_to_aws 면 대상이다."
  type        = string

  validation {
    condition     = contains(["aws_to_gcp", "gcp_to_aws"], var.direction)
    error_message = "direction 은 aws_to_gcp 또는 gcp_to_aws 여야 한다."
  }
}

variable "region" {
  description = "AWS 리전."
  type        = string
  default     = "ap-northeast-2"
}

variable "vpc_cidr" {
  description = "전용 VPC 주소 대역. 앞의 /24 두 개를 퍼블릭 서브넷으로 쓴다."
  type        = string
  default     = "10.80.0.0/16"
}

variable "db_engine_version" {
  description = "RDS PostgreSQL 엔진 버전(주 버전만 써도 된다). 15 이상."
  type        = string
  default     = "16"

  validation {
    condition     = tonumber(split(".", var.db_engine_version)[0]) >= 15
    error_message = "PostgreSQL 15 이상이어야 한다."
  }
}

variable "db_instance_class" {
  description = "RDS 인스턴스 사양."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_name" {
  description = "처음 만들 데이터베이스 이름."
  type        = string
  default     = "app"
}

variable "db_username" {
  description = "RDS 마스터 사용자 이름. 비밀번호는 RDS 가 Secrets Manager 에 만든다."
  type        = string
  default     = "ccxadmin"
}

variable "secret_names" {
  description = "값 없이 만들 Secrets Manager 그릇 이름(접두사 뒤). 값은 crosscloud 가 따로 넣는다."
  type        = list(string)
  default     = ["database-url"]
}

variable "include_app" {
  description = "앱 층(ECS 서비스·ALB)을 만들지. gcp_to_aws 에서 대상 기반만 먼저 깔 때 false."
  type        = bool
  default     = true
}

variable "app_image" {
  description = "앱 컨테이너 이미지(digest 고정 권장). include_app 이면 필요하다."
  type        = string
  default     = null

  validation {
    condition     = !var.include_app || var.app_image != null
    error_message = "include_app 이 true 면 app_image 가 필요하다."
  }
}

variable "app_port" {
  description = "앱 컨테이너가 듣는 포트."
  type        = number
  default     = 8080
}

variable "app_health_path" {
  description = "ALB 대상 그룹 상태 확인 경로. 기본은 DB 를 읽는 준비 확인이다."
  type        = string
  default     = "/readyz"
}

variable "app_db_ssl_ca" {
  description = "앱이 DB 인증서를 검증할 CA 번들 PEM(RDS 리전 번들). 비밀값이 아니라 평문 env DB_SSL_CA 로 넣는다. null 이면 넣지 않는다."
  type        = string
  default     = null
}

variable "app_env" {
  description = "앱에 평문으로 줄 env. 비밀값·DB 접속 대상은 넣지 않는다(app_secret_env 로)."
  type        = map(string)
  default     = {}
}

variable "app_secret_env" {
  description = "env 이름 → Secret 그릇 이름. 값은 Secret 참조로만 주입한다."
  type        = map(string)
  default     = { DATABASE_URL = "database-url" }

  validation {
    condition     = alltrue([for name in values(var.app_secret_env) : contains(var.secret_names, name)])
    error_message = "app_secret_env 의 값은 secret_names 중 하나여야 한다."
  }
}

variable "app_desired_count" {
  description = "앱 작업 수."
  type        = number
  default     = 1
}
