# Cloud Run 진입을 plan 으로 확인한다. 운영자 PC 의 durable-ingress 가 서비스
# URL(run.app)로 직접 보내므로 방향과 관계없이 밖에서 닿아야 한다(설계 4.18).

mock_provider "google" {}

variables {
  project_id  = "p"
  name_prefix = "ccx-r1"
  image       = "asia-northeast3-docker.pkg.dev/p/r/app@sha256:abc"
}

run "target_is_reachable_from_operator_pc" {
  command = plan

  variables {
    direction = "aws_to_gcp"
  }

  assert {
    condition     = google_cloud_run_v2_service.app.ingress == "INGRESS_TRAFFIC_ALL"
    error_message = "대상 Cloud Run 은 운영자 PC 에서 닿아야 한다(ingress all)."
  }
}

run "source_fixture_is_reachable_from_operator_pc" {
  command = plan

  variables {
    direction = "gcp_to_aws"
  }

  assert {
    condition     = google_cloud_run_v2_service.app.ingress == "INGRESS_TRAFFIC_ALL"
    error_message = "원본 고정물 Cloud Run 도 운영자 PC 에서 닿아야 한다."
  }
}
