variable "name_prefix" {
  description = "모든 자원 이름 접두사(ccx-<run>)."
  type        = string
}

variable "vpc_cidr" {
  description = "전용 VPC 주소 대역."
  type        = string
}

variable "db_engine_version" {
  description = "RDS PostgreSQL 엔진 버전."
  type        = string
}

variable "db_instance_class" {
  description = "RDS 인스턴스 사양."
  type        = string
}

variable "db_name" {
  description = "처음 만들 데이터베이스 이름."
  type        = string
}

variable "db_username" {
  description = "RDS 마스터 사용자 이름."
  type        = string
}

variable "secret_names" {
  description = "값 없이 만들 Secret 그릇 이름."
  type        = list(string)
}
