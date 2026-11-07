terraform {
  # 변수 검증에서 다른 변수를 참조하려면 1.9 이상이 필요하다.
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }

  # state 경로는 실행기(crosscloud.provision.terraform)가 init 때
  # -backend-config=path=... 로 준다.
  backend "local" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "crosscloud-run"       = var.name_prefix
      "crosscloud-direction" = var.direction
    }
  }
}
