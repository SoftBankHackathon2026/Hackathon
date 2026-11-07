output "repository_id" {
  description = "저장소 ID."
  value       = google_artifact_registry_repository.images.repository_id
}

output "repository_url" {
  description = "docker push·pull 에 쓰는 저장소 주소(<리전>-docker.pkg.dev/<프로젝트>/<저장소>)."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
