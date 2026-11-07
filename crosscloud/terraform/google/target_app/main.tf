# 이전 대상(또는 GCP→AWS 원본 고정물) 앱: Cloud Run 서비스 + 외부 Application
# Load Balancer(서버리스 NEG).
#
# 시험 환경 결정(2026-11-07)에 따라 VPC egress·Cloud NAT 는 두지 않는다. 앱은
# 기본 출구로 나가 DB 공개 엔드포인트에 붙는다.
#
# 프런트엔드는 HTTP(80)다. 시연은 도메인 없이 Route 53 비공개 영역을 쓰기로
# 해서(2026-11-07) 인증서를 받을 이름이 없고, 자체 서명 키를 Terraform 으로
# 만들면 개인 키가 state 에 남는다.

locals {
  is_target = var.direction == "aws_to_gcp"
  # 대상은 전환 전까지 진입을 닫아 둔다 — 제어 어댑터가 LB 진입을 연다.
  # 원본 고정물은 처음부터 LB 로만 받는다.
  ingress    = local.is_target ? "INGRESS_TRAFFIC_INTERNAL_ONLY" : "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  secret_ids = toset([for ref in values(var.secret_env) : ref.secret])
}

resource "google_service_account" "run" {
  account_id   = "${var.name_prefix}-run"
  display_name = "crosscloud ${var.name_prefix} Cloud Run 실행 신원"
}

resource "google_secret_manager_secret_iam_member" "run" {
  for_each  = local.secret_ids
  secret_id = each.key
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.run.member
}

resource "google_cloud_run_v2_service" "app" {
  name                = "${var.name_prefix}-app"
  location            = var.region
  ingress             = local.ingress
  deletion_protection = false
  # LB 를 거친 요청을 IAM 인증 없이 받는다. 진입은 ingress 가 막는다.
  invoker_iam_disabled = true

  template {
    service_account = google_service_account.run.email

    containers {
      image = var.image

      ports {
        container_port = var.container_port
      }

      dynamic "env" {
        for_each = var.env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = env.value.version
            }
          }
        }
      }
    }
  }

  # 비밀 읽기 권한이 먼저 있어야 첫 리비전이 뜬다.
  depends_on = [google_secret_manager_secret_iam_member.run]

  lifecycle {
    # 전환 중 제어 어댑터가 진입을 여닫는다. 재 apply 가 그것을 되돌리지 않게
    # 한다.
    ignore_changes = [ingress]
  }
}

resource "google_compute_region_network_endpoint_group" "app" {
  name                  = "${var.name_prefix}-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.app.name
  }
}

resource "google_compute_backend_service" "app" {
  name                  = "${var.name_prefix}-backend"
  load_balancing_scheme = "EXTERNAL_MANAGED"

  backend {
    group = google_compute_region_network_endpoint_group.app.id
  }
}

resource "google_compute_url_map" "app" {
  name            = "${var.name_prefix}-urlmap"
  default_service = google_compute_backend_service.app.id
}

resource "google_compute_target_http_proxy" "app" {
  name    = "${var.name_prefix}-http-proxy"
  url_map = google_compute_url_map.app.id
}

resource "google_compute_global_address" "app" {
  name = "${var.name_prefix}-ip"
}

resource "google_compute_global_forwarding_rule" "app" {
  name                  = "${var.name_prefix}-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.app.id
  port_range            = "80"
  target                = google_compute_target_http_proxy.app.id
}
