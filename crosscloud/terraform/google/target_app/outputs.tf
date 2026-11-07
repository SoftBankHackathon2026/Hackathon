output "service_name" {
  description = "Cloud Run 서비스 이름."
  value       = google_cloud_run_v2_service.app.name
}

output "service_uri" {
  description = "Cloud Run 기본 URL. 운영자 PC 의 ingress 대상(INGRESS_TARGETS.gcp)이다."
  value       = google_cloud_run_v2_service.app.uri
}

output "lb_ip_address" {
  description = "외부 LB 의 공개 IPv4 주소(HTTP 80). DNS 가중 레코드가 가리킨다."
  value       = google_compute_global_address.app.address
}

output "runtime_service_account" {
  description = "Cloud Run 실행 서비스 계정 이메일."
  value       = google_service_account.run.email
}
