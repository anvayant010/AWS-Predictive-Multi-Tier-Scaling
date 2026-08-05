resource "aws_security_group" "catalog_alb" {
  name        = "${var.project_name}-catalog-alb"
  description = "catalog_service ALB: internet to HTTP"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP from internet"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "catalog_instance" {
  name        = "${var.project_name}-catalog-instance"
  description = "catalog_service EC2 instances: ALB to uvicorn port only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "catalog_service from ALB"
    from_port       = var.catalog_port
    to_port         = var.catalog_port
    protocol        = "tcp"
    security_groups = [aws_security_group.catalog_alb.id]
  }

  egress {
    description = "outbound for package installs (user_data) and normal operation"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
