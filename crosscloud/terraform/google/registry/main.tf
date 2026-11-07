# 이전할 컨테이너 이미지를 받을 Artifact Registry 저장소(Docker 형식).
resource "google_artifact_registry_repository" "images" {
  repository_id = "${var.name_prefix}-images"
  location      = var.region
  format        = "DOCKER"
  description   = "crosscloud ${var.name_prefix} 이미지 저장소"
}
