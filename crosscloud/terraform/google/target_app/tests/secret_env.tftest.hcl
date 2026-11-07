# secret_env 는 버전 번호로만 Secret 을 참조한다. 이관을 되돌리면(파기) latest
# 가 파기된 버전을 가리켜 Cloud Run 이 값을 읽지 못한다.

mock_provider "google" {}

variables {
  project_id  = "ccx-test-project"
  name_prefix = "ccx-r1"
  direction   = "aws_to_gcp"
  image       = "asia-northeast3-docker.pkg.dev/p/r/app@sha256:0000"
}

run "accepts_numbered_secret_version" {
  command = plan

  variables {
    secret_env = {
      DATABASE_URL = { secret = "ccx-r1-database-url", version = "3" }
    }
  }
}

run "rejects_latest_secret_version" {
  command = plan

  variables {
    secret_env = {
      DATABASE_URL = { secret = "ccx-r1-database-url", version = "latest" }
    }
  }

  expect_failures = [var.secret_env]
}

run "rejects_non_numeric_secret_version" {
  command = plan

  variables {
    secret_env = {
      DATABASE_URL = { secret = "ccx-r1-database-url", version = "v1" }
    }
  }

  expect_failures = [var.secret_env]
}
