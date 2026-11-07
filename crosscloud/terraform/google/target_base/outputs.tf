output "db_instance_name" {
  description = "Cloud SQL 인스턴스 이름."
  value       = google_sql_database_instance.db.name
}

output "db_connection_name" {
  description = "Cloud SQL 연결 이름(<프로젝트>:<리전>:<인스턴스>)."
  value       = google_sql_database_instance.db.connection_name
}

output "db_public_ip" {
  description = "Cloud SQL 공개 IPv4 주소."
  value       = google_sql_database_instance.db.public_ip_address
}

output "database_name" {
  description = "앱이 쓸 데이터베이스 이름."
  value       = google_sql_database.app.name
}

output "secret_ids" {
  description = "짧은 이름 → Secret Manager 비밀 ID. target_app 의 secret_env 에 넘긴다."
  value       = { for name, secret in google_secret_manager_secret.slots : name => secret.secret_id }
}
