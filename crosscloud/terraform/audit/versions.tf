# 이 모듈은 루트 모듈이다. state 경로는 init -backend-config=path=... 로
# 넘기므로 local 백엔드를 비워 선언한다(R2g 규약과 같다).
terraform {
  required_version = ">= 1.6"

  backend "local" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.labels
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  # 라벨을 받는 모든 자원에 붙는다. 잔여 검사는 이름 접두사로 한다.
  default_labels = local.labels
}
