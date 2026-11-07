# 이 모듈은 루트 모듈이다. crosscloud.provision.terraform 이 state 경로를
# init -backend-config=path=... 로 넘기므로 local 백엔드를 비워 선언한다.
terraform {
  required_version = ">= 1.6"

  backend "local" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  # 라벨을 받는 모든 자원에 붙는다. 잔여 검사는 이름 접두사로 하고, 라벨은
  # 사람이 콘솔에서 이 실행의 자원을 알아보는 용도다.
  default_labels = local.labels
}
