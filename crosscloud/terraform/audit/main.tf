# 감사 기록 이관 대상 버킷 2개. 둘 다 잠글 수 있는 종류로 만들되 잠그지 않는다
# (평가 20 D7). 잠금은 사람 승인 뒤 마지막 단계의 일이고, 이 모듈의 범위 밖이다.
#
# *   GCS: AWS→GCP 대상. 보존 정책은 있지만 미잠금(is_locked = false).
# *   S3: GCP→AWS 대상. Object Lock 활성, 기본 보존 규칙은 두지 않는다.
#
# 라벨·태그(crosscloud-run·crosscloud-direction)는 provider 기본값으로 붙는다.

resource "google_storage_bucket" "audit_archive" {
  name                        = "${var.name_prefix}-audit-gcs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  retention_policy {
    is_locked        = false
    retention_period = var.gcs_retention_seconds
  }
}

resource "aws_s3_bucket" "audit_archive" {
  bucket              = "${var.name_prefix}-audit-s3"
  object_lock_enabled = true
  force_destroy       = true
}

resource "aws_s3_bucket_public_access_block" "audit_archive" {
  bucket                  = aws_s3_bucket.audit_archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
