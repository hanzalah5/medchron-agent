variable "project_id" { type = string }
variable "project_number" {
  type    = string
  default = ""
}
variable "region" {
  type    = string
  default = "us-central1"
}
variable "image" {
  type    = string
  default = "us-docker.pkg.dev/cloudrun/container/hello"
}
variable "app_env" {
  type    = string
  default = "demo"
}
variable "auth_mode" {
  type    = string
  default = "dev"
}
variable "oidc_issuer" {
  type    = string
  default = ""
}
variable "public" {
  type    = bool
  default = true
}
variable "case_budget_usd" {
  type    = number
  default = 1
}
variable "github_repository" {
  type    = string
  default = "hanzalah5/medchron-agent"
}
variable "billing_account" {
  type    = string
  default = null
}
variable "monthly_budget_usd" {
  type    = number
  default = 10
}
