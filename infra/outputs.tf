output "catalog_alb_dns_name" {
  description = "catalog_service entrypoint. curl http://<this>/health and /products."
  value       = aws_lb.catalog.dns_name
}

output "catalog_asg_name" {
  description = "Pass to `aws autoscaling set-desired-capacity` for the manual Phase 1 verification."
  value       = aws_autoscaling_group.catalog.name
}

output "thumbnail_api_invoke_url" {
  description = "thumbnail_service entrypoint. GET <this>/thumbnail?image_id=x&width=128&height=128."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "thumbnail_lambda_function_name" {
  description = "Pass to `aws lambda put-provisioned-concurrency-config` for the manual Phase 1 verification."
  value       = aws_lambda_function.thumbnail_service.function_name
}

output "thumbnail_lambda_alias_name" {
  description = "Qualifier to use with put-provisioned-concurrency-config (provisioned concurrency can't target $LATEST)."
  value       = aws_lambda_alias.thumbnail_service_live.name
}
