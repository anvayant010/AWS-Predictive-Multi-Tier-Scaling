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

variable "thumbnail_log_retention_days" {
  description = "How long the thumbnail Lambda's CloudWatch logs are kept while the stack exists."
  type        = number
  default     = 7
}

# --- Phase 7 comparison arms -------------------------------------------------------------------------

variable "scaling_mode" {
  description = <<-EOT
    Which scaling arm the catalog ASG is built as (Phase 7):
      predictive          no AWS scaling policy; our engine sets the desired capacity (the default)
      reactive_standard   AWS target tracking on ALBRequestCountPerTarget at reactive_standard_utilization x measured capacity
      reactive_aggressive the same at reactive_aggressive_utilization x measured capacity
      static_peak         no scaling; the ASG is pinned at static_peak_instances (an oracle reference, not a deployable policy)
  EOT
  type        = string
  default     = "predictive"

  validation {
    condition     = contains(["predictive", "reactive_standard", "reactive_aggressive", "static_peak"], var.scaling_mode)
    error_message = "scaling_mode must be one of: predictive, reactive_standard, reactive_aggressive, static_peak."
  }
}

variable "catalog_capacity_rpm_per_instance" {
  description = "MEASURED per-instance capacity (requests/min at the latency knee) from the Phase 7 calibration run. Deliberately has no default: the reactive modes refuse to plan without a measured value, so an assumed number cannot slip into a comparison run."
  type        = number
  default     = null

  validation {
    condition     = var.catalog_capacity_rpm_per_instance == null ? true : var.catalog_capacity_rpm_per_instance > 0
    error_message = "catalog_capacity_rpm_per_instance must be positive."
  }
}

variable "reactive_standard_utilization" {
  description = "Target-tracking target for reactive_standard as a fraction of measured capacity (decision D3: 0.7, the same factor the predictive policy uses)."
  type        = number
  default     = 0.7

  validation {
    condition     = var.reactive_standard_utilization > 0 && var.reactive_standard_utilization <= 1
    error_message = "reactive_standard_utilization must be in (0, 1]."
  }
}

variable "reactive_aggressive_utilization" {
  description = "Target-tracking target for reactive_aggressive as a fraction of measured capacity (decision D3: 0.5)."
  type        = number
  default     = 0.5

  validation {
    condition     = var.reactive_aggressive_utilization > 0 && var.reactive_aggressive_utilization <= 1
    error_message = "reactive_aggressive_utilization must be in (0, 1]."
  }
}

variable "static_peak_instances" {
  description = "Fixed instance count for static_peak: enough for the scenario's maximum load. Set from the Phase 7 scenario once it exists; ignored by the other modes."
  type        = number
  default     = 3

  validation {
    condition     = var.static_peak_instances >= 1 && var.static_peak_instances <= 10
    error_message = "static_peak_instances must be between 1 and 10 (the hard cap)."
  }
}
