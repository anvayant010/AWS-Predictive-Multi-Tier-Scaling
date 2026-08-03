resource "aws_apigatewayv2_api" "thumbnail" {
  name          = "${var.project_name}-thumbnail-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "thumbnail" {
  api_id                 = aws_apigatewayv2_api.thumbnail.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_alias.thumbnail_service_live.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "thumbnail" {
  api_id    = aws_apigatewayv2_api.thumbnail.id
  route_key = "GET /thumbnail"
  target    = "integrations/${aws_apigatewayv2_integration.thumbnail.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.thumbnail.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.thumbnail_service.function_name
  qualifier     = aws_lambda_alias.thumbnail_service_live.name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.thumbnail.execution_arn}/*/*"
}
