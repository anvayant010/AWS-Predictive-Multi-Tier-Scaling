data "archive_file" "thumbnail_service" {
  type        = "zip"
  source_file = "${path.module}/../demo_app/thumbnail_service/handler.py"
  output_path = "${path.module}/build/thumbnail_service.zip"
}

resource "aws_lambda_function" "thumbnail_service" {
  function_name = "${var.project_name}-thumbnail-service"
  role          = aws_iam_role.thumbnail_lambda.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.11"
  timeout       = var.thumbnail_lambda_timeout_seconds
  memory_size   = var.thumbnail_lambda_memory_mb

  filename         = data.archive_file.thumbnail_service.output_path
  source_code_hash = data.archive_file.thumbnail_service.output_base64sha256

  # Publish a concrete version on every deploy so there's always a stable
  # target for the "live" alias below — provisioned concurrency (Phase 6)
  # cannot be set on $LATEST, it needs a published version or an alias.
  publish = true

  # Create the Terraform-managed log group first, or Lambda would auto-create it and the two
  # would collide (see cloudwatch_logs.tf).
  depends_on = [aws_cloudwatch_log_group.thumbnail_service]
}

resource "aws_lambda_alias" "thumbnail_service_live" {
  name             = "live"
  function_name    = aws_lambda_function.thumbnail_service.function_name
  function_version = aws_lambda_function.thumbnail_service.version
}
