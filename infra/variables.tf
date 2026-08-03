variable "project_name" {
  description = "Short name used as a prefix for all project resources — must match infra/bootstrap's project_name, since IAM policies there scope access by this prefix."
  type        = string
  default     = "predictive-scaling"
}

variable "aws_region" {
  description = "AWS region for all project resources."
  type        = string
  default     = "us-east-1"
}

variable "catalog_instance_type" {
  description = "EC2 instance type for catalog_service."
  type        = string
  default     = "t3.micro"
}

variable "catalog_port" {
  description = "Port catalog_service (uvicorn) listens on inside each instance."
  type        = number
  default     = 8000
}

variable "catalog_asg_min_size" {
  type    = number
  default = 1
}

variable "catalog_asg_max_size" {
  description = "Hard cap on ASG size. decision_engine/policy.py must independently enforce this same cap — this Terraform value is not the only place it's checked."
  type        = number
  default     = 10
}

variable "catalog_asg_desired_capacity" {
  type    = number
  default = 1
}

variable "thumbnail_lambda_memory_mb" {
  type    = number
  default = 128
}

variable "thumbnail_lambda_timeout_seconds" {
  type    = number
  default = 10
}
