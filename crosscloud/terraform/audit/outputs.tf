output "gcs_bucket" {
  description = "AWS→GCP 대상 GCS 버킷 이름(cli --target-bucket)."
  value       = google_storage_bucket.audit_archive.name
}

output "s3_bucket" {
  description = "GCP→AWS 대상 S3 버킷 이름(cli --target-bucket)."
  value       = aws_s3_bucket.audit_archive.bucket
}
