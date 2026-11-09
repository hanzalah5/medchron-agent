variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "project_number" {
  type        = string
  description = "GCP project number (used by the budget filter)."
  default     = ""
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "environment" {
  type        = string
  description = "dev or prod-demo."
  validation {
    condition     = contains(["dev", "prod-demo"], var.environment)
    error_message = "environment must be dev or prod-demo."
  }
}

variable "image" {
  type        = string
  description = "Container image (Artifact Registry). CI updates it on deploy."
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "app_env" {
  type        = string
  description = "MEDCHRON_ENV: production (OIDC required) or demo (synthetic public demo)."
  default     = "demo"
}

variable "auth_mode" {
  type    = string
  default = "dev"
  validation {
    condition     = contains(["dev", "oidc"], var.auth_mode)
    error_message = "auth_mode must be dev or oidc."
  }
}

variable "oidc_issuer" {
  type    = string
  default = ""
}

variable "oidc_audience" {
  type    = string
  default = "medchron"
}

variable "model" {
  type    = string
  default = "claude-opus-5-5"
}

variable "case_budget_usd" {
  type        = number
  description = "Hard cap on LLM spend per case."
  default     = 1
}

variable "max_instances" {
  type    = number
  default = 2
}

variable "public" {
  type        = bool
  description = "Allow unauthenticated access at the Cloud Run layer (the app still authenticates)."
  default     = false
}

variable "retention_days" {
  type        = number
  description = "Delete uploaded productions after this many days."
  default     = 30
}

variable "kms_key_name" {
  type        = string
  description = "Optional customer-managed key for the records bucket."
  default     = null
}

variable "github_repository" {
  type        = string
  description = "owner/repo allowed to deploy through Workload Identity Federation."
}

variable "billing_account" {
  type        = string
  description = "Billing account ID for the budget alert (null disables it)."
  default     = null
}

variable "monthly_budget_usd" {
  type    = number
  default = 10
}
