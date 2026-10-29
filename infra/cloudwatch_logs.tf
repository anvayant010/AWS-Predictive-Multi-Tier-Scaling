# Lambda creates its log group automatically on first invocation, and Terraform then doesn't know
# about it — so `terraform destroy` left it behind forever, keeping every log line with no expiry.
# Managing it here makes destroy remove it and gives it a retention period while it exists.
#
# The name must be exactly /aws/lambda/<function name>. The function depends on this resource
# (see lambda.tf) so Terraform creates the group first, before Lambda can auto-create it.

resource "aws_cloudwatch_log_group" "thumbnail_service" {
  name              = "/aws/lambda/${var.project_name}-thumbnail-service"
  retention_in_days = var.thumbnail_log_retention_days
}
