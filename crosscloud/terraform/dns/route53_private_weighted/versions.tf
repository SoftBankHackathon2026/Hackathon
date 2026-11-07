terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }

  # state 위치는 실행기가 init 때 -backend-config 로 준다.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "crosscloud-run"       = var.run_id
      "crosscloud-direction" = var.direction
    }
  }
}
