variable "project_name" {
  description = "Short name used as a prefix for all project resources (state bucket, lock table, IAM user, and the naming scope every IAM permission is restricted to)."
  type        = string
  default     = "predictive-scaling"
}

variable "aws_region" {
  description = "AWS region for the state backend and (by convention) the rest of the project's resources."
  type        = string
  default     = "us-east-1"
}

variable "iam_user_name" {
  description = "Name of the scoped IAM user created for this project's Terraform runs and actuator boto3 calls."
  type        = string
  default     = "predictive-scaling-engine"
}

variable "budget_limit_usd" {
  description = "Monthly cost limit (USD) for the account-level budget alarm. Alerts fire at 50/80/100% of actual spend and at 100% of forecasted spend."
  type        = number
  default     = 10

  validation {
    condition     = var.budget_limit_usd > 0
    error_message = "budget_limit_usd must be positive."
  }
}

variable "budget_alert_email" {
  description = "Email address that receives budget alerts. No default on purpose: set it in a local, gitignored terraform.tfvars (see terraform.tfvars.example) so a personal address is never committed."
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$", var.budget_alert_email))
    error_message = "budget_alert_email must look like an email address."
  }
}
