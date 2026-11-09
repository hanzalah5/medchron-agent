# dev environment. State lives in a GCS bucket you create once:
#   terraform init -backend-config="bucket=<your-tf-state-bucket>"
terraform {
  required_version = ">= 1.6"
  backend "gcs" {
    prefix = "medchron/dev"
  }
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.10"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "medchron" {
  source             = "../../modules/medchron"
  project_id         = var.project_id
  project_number     = var.project_number
  region             = var.region
  environment        = "dev"
  image              = var.image
  app_env            = var.app_env
  auth_mode          = var.auth_mode
  oidc_issuer        = var.oidc_issuer
  public             = var.public
  case_budget_usd    = var.case_budget_usd
  github_repository  = var.github_repository
  billing_account    = var.billing_account
  monthly_budget_usd = var.monthly_budget_usd
}

output "api_url" { value = module.medchron.api_url }
output "image_repository" { value = module.medchron.image_repository }
output "deployer_service_account" { value = module.medchron.deployer_service_account }
output "workload_identity_provider" { value = module.medchron.workload_identity_provider }
output "secrets_to_populate" { value = module.medchron.secrets_to_populate }
