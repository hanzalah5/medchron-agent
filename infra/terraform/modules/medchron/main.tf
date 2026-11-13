# MedChron on Google Cloud: scale-to-zero, low idle cost.
#
#   Cloud Run service (API + UI) --enqueue--> Postgres (Neon or Cloud SQL, external)
#        |                                        ^
#        +--runs execution--> Cloud Run job (worker --once) --+
#   GCS bucket (record productions) . Secret Manager . Artifact Registry
#   GitHub Actions deploys through Workload Identity Federation (no keys)

locals {
  name   = "medchron-${var.environment}"
  labels = { app = "medchron", env = var.environment, data = "synthetic-only" }
  apis = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "storage.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- images
resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "medchron"
  format        = "DOCKER"
  labels        = local.labels

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- identities
resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "${local.name}-api"
  display_name = "MedChron API (${var.environment})"
}

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = "${local.name}-worker"
  display_name = "MedChron worker (${var.environment})"
}

# Runs database migrations as the owner. The API and worker never get the owner URL.
resource "google_service_account" "migrate" {
  project      = var.project_id
  account_id   = "${local.name}-migrate"
  display_name = "MedChron migrations (${var.environment})"
}

# ---------------------------------------------------------------- storage
resource "google_storage_bucket" "records" {
  project                     = var.project_id
  name                        = "${var.project_id}-${local.name}-records"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.environment != "prod-demo"
  labels                      = local.labels

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = var.retention_days
    }
    action {
      type = "Delete"
    }
  }

  dynamic "encryption" {
    for_each = var.kms_key_name == null ? [] : [var.kms_key_name]
    content {
      default_kms_key_name = encryption.value
    }
  }
}

resource "google_storage_bucket_iam_member" "records_rw" {
  for_each = {
    api    = google_service_account.api.email
    worker = google_service_account.worker.email
  }
  bucket = google_storage_bucket.records.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${each.value}"
}

# ---------------------------------------------------------------- secrets
# Terraform creates the secret containers only. Values are added out of band
# (gcloud secrets versions add ...), so they never enter Terraform state.
#
# Least privilege: each identity can read only the secrets it uses. The API and the
# worker connect as their own non-owner database roles, and only the migrate job
# holds the owner URL. These maps (env var -> secret) are the single source of truth.
locals {
  api_secret_env = {
    DATABASE_URL              = "database-url-api"
    MEDCHRON_DEMO_ACCESS_CODE = "demo-access-code"
  }
  worker_secret_env = {
    DATABASE_URL      = "database-url-worker"
    ANTHROPIC_API_KEY = "anthropic-api-key"
  }
  migrate_secret_env = {
    DATABASE_URL = "database-url-migrate"
  }
  # secret -> the one identity allowed to read it
  secret_readers = merge(
    { for s in values(local.api_secret_env) : s => "api" },
    { for s in values(local.worker_secret_env) : s => "worker" },
    { for s in values(local.migrate_secret_env) : s => "migrate" },
  )
  service_account_emails = {
    api     = google_service_account.api.email
    worker  = google_service_account.worker.email
    migrate = google_service_account.migrate.email
  }
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = local.secret_readers
  project   = var.project_id
  secret_id = "${local.name}-${each.key}"
  labels    = local.labels

  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = local.secret_readers
  project   = var.project_id
  secret_id = google_secret_manager_secret.secrets[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.service_account_emails[each.value]}"
}

locals {
  common_env = {
    MEDCHRON_ENV       = var.app_env
    MEDCHRON_AUTH_MODE = var.auth_mode
    MEDCHRON_STORAGE   = "gs://${google_storage_bucket.records.name}"
    MEDCHRON_MODEL     = var.model
  }
  oidc_env = var.auth_mode == "oidc" ? {
    MEDCHRON_OIDC_ISSUER   = var.oidc_issuer
    MEDCHRON_OIDC_AUDIENCE = var.oidc_audience
  } : {}
}

# ---------------------------------------------------------------- worker (job)
resource "google_cloud_run_v2_job" "worker" {
  project             = var.project_id
  name                = "${local.name}-worker"
  location            = var.region
  labels              = local.labels
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account = google_service_account.worker.email
      timeout         = "1800s"
      max_retries     = 1
      containers {
        image   = var.image
        command = ["medchron"]
        args    = ["worker", "--once"] # migrations run in the separate migrate job
        resources {
          limits = { cpu = "1", memory = "2Gi" }
        }
        dynamic "env" {
          for_each = merge(local.common_env, { MEDCHRON_CASE_BUDGET_USD = tostring(var.case_budget_usd) })
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = local.worker_secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.secrets[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.access]
}

# The API may start worker executions, and nothing else.
resource "google_cloud_run_v2_job_iam_member" "api_runs_worker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.api.email}"
}

# ---------------------------------------------------------------- migrations (job)
resource "google_cloud_run_v2_job" "migrate" {
  project             = var.project_id
  name                = "${local.name}-migrate"
  location            = var.region
  labels              = local.labels
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account = google_service_account.migrate.email
      timeout         = "600s"
      max_retries     = 0
      containers {
        image   = var.image
        command = ["medchron"]
        args    = ["migrate"]
        resources {
          limits = { cpu = "1", memory = "1Gi" }
        }
        dynamic "env" {
          for_each = local.migrate_secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.secrets[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.access]
}

# ---------------------------------------------------------------- API (service)
resource "google_cloud_run_v2_service" "api" {
  project             = var.project_id
  name                = "${local.name}-api"
  location            = var.region
  labels              = local.labels
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = 0 # scale to zero: no idle compute cost
      max_instance_count = var.max_instances
    }
    containers {
      image = var.image
      ports {
        container_port = 8080
      }
      resources {
        limits   = { cpu = "1", memory = "1Gi" }
        cpu_idle = true
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
      dynamic "env" {
        for_each = merge(local.common_env, local.oidc_env, {
          MEDCHRON_WORKER_JOB          = google_cloud_run_v2_job.worker.id
          MEDCHRON_DEMO_MAX_BUDGET_USD = tostring(var.case_budget_usd)
        })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.api_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secrets[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.access]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.public ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------- CI/CD identity (GitHub OIDC)
resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = "${local.name}-deploy"
  display_name = "MedChron GitHub deployer (${var.environment})"
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "${local.name}-github"
  display_name              = "GitHub Actions (${var.environment})"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  # Only this repository can authenticate.
  attribute_condition = "assertion.repository == \"${var.github_repository}\""
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

resource "google_project_iam_member" "deployer_roles" {
  for_each = toset(["roles/run.developer", "roles/artifactregistry.writer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_acts_as" {
  for_each = {
    api     = google_service_account.api.name
    worker  = google_service_account.worker.name
    migrate = google_service_account.migrate.name
  }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# ---------------------------------------------------------------- spend alert
resource "google_billing_budget" "monthly" {
  count           = var.billing_account == null ? 0 : 1
  billing_account = var.billing_account
  display_name    = "${local.name} monthly budget"

  budget_filter {
    projects = ["projects/${var.project_number}"]
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_usd)
    }
  }
  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }
}
