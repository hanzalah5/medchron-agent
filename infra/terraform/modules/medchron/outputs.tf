output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "worker_job" {
  value = google_cloud_run_v2_job.worker.id
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "records_bucket" {
  value = google_storage_bucket.records.name
}

output "deployer_service_account" {
  value = google_service_account.deployer.email
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "secrets_to_populate" {
  value = [for s in google_secret_manager_secret.secrets : s.secret_id]
}
