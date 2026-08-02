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
