resource "aws_lb" "catalog" {
  name               = "${var.project_name}-catalog"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.catalog_alb.id]
  subnets            = data.aws_subnets.default.ids
}

resource "aws_lb_target_group" "catalog" {
  name        = "${var.project_name}-catalog"
  port        = var.catalog_port
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "instance"

  health_check {
    path                = "/health"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }
}

resource "aws_lb_listener" "catalog_http" {
  load_balancer_arn = aws_lb.catalog.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.catalog.arn
  }
}
