terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Intentionally local state: this config creates the S3 bucket + DynamoDB
  # table that the *rest* of the project's Terraform will use as a remote
  # backend. It cannot depend on infrastructure it hasn't created yet.
  # The resulting terraform.tfstate stays local and gitignored.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform-bootstrap"
    }
  }
}
