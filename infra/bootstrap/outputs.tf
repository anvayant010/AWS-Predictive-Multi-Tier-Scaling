output "state_bucket_name" {
  description = "S3 bucket holding Terraform state for the main infra/ configs."
  value       = aws_s3_bucket.tf_state.bucket
}

output "lock_table_name" {
  description = "DynamoDB table used for Terraform state locking."
  value       = aws_dynamodb_table.tf_lock.name
}

output "iam_user_name" {
  description = "Scoped IAM user for this project. Create its access key manually (see infra/README.md) — Terraform does not generate it, to keep the secret out of state."
  value       = aws_iam_user.project.name
}

output "iam_user_arn" {
  value = aws_iam_user.project.arn
}

output "backend_config_snippet" {
  description = "Paste this into infra/backend.tf (Phase 1+) to point the main infra at this state backend."
  value       = <<-EOT
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.tf_state.bucket}"
        key            = "main/terraform.tfstate"
        region         = "${var.aws_region}"
        dynamodb_table = "${aws_dynamodb_table.tf_lock.name}"
        encrypt        = true
      }
    }
  EOT
}
