# 이전 대상(또는 GCP→AWS 원본 고정물) DB 와 Secret 그릇.
#
# 시험 환경 결정(2026-11-07)에 따라 운영 옵션은 끈다: 단일 존, 백업·삭제 보호
# 없음, 공개 IP 에 허용 망 0.0.0.0/0. 접속은 TLS 필수(인증서 검증 없는
# sslmode=require 와 맞는 ENCRYPTED_ONLY)와 강한 무작위 비밀번호로 막는다.
# postgres 사용자 비밀번호는 여기서 정하지 않는다 — state 에 평문이 남는다.

resource "google_sql_database_instance" "db" {
  name                = "${var.name_prefix}-db"
  database_version    = var.database_version
  region              = var.region
  deletion_protection = false

  settings {
    # PostgreSQL 16 부터 기본 등급이 Enterprise Plus 라 공유 코어를 못 쓴다.
    edition                     = "ENTERPRISE"
    tier                        = var.db_tier
    availability_type           = "ZONAL"
    disk_size                   = 10
    deletion_protection_enabled = false
    user_labels                 = local.labels

    backup_configuration {
      enabled = false
    }

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
      # 앱 verify-full 이 호스트 이름을 대조할 수 있게 SAN 을 싣는 CAS 기반
      # 서버 인증서를 쓴다(설계 0.1 S7). SAN 내용은 실행 전 확인 항목이다.
      server_ca_mode = "GOOGLE_MANAGED_CAS_CA"

      authorized_networks {
        name  = "anywhere"
        value = "0.0.0.0/0"
      }
    }

    # CDC(논리 복제)에 필요하다. 바꾸면 인스턴스가 재시작된다.
    database_flags {
      name  = "cloudsql.logical_decoding"
      value = "on"
    }
  }
}

resource "google_sql_database" "app" {
  name     = var.database_name
  instance = google_sql_database_instance.db.name
  # 남은 연결·객체가 있으면 DB 삭제가 실패한다. 인스턴스 삭제가 함께 지우므로
  # state 에서만 뺀다.
  deletion_policy = "ABANDON"
}

resource "google_secret_manager_secret" "slots" {
  for_each  = var.secret_names
  secret_id = "${var.name_prefix}-${each.key}"

  replication {
    auto {}
  }
}
