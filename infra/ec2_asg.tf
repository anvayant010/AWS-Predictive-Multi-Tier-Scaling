resource "aws_launch_template" "catalog" {
  name_prefix   = "${var.project_name}-catalog-"
  image_id      = data.aws_ami.al2023.id
  instance_type = var.catalog_instance_type

  network_interfaces {
    associate_public_ip_address = true
    security_groups             = [aws_security_group.catalog_instance.id]
  }

  user_data = base64encode(templatefile("${path.module}/templates/catalog_user_data.sh.tftpl", {
    data_py_content = file("${path.module}/../demo_app/catalog_service/data.py")
    main_py_content = file("${path.module}/../demo_app/catalog_service/main.py")
    port            = var.catalog_port
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${var.project_name}-catalog"
    }
  }
}

resource "aws_autoscaling_group" "catalog" {
  name                = "${var.project_name}-catalog-asg"
  min_size            = var.catalog_asg_min_size
  max_size            = var.catalog_asg_max_size
  desired_capacity    = var.catalog_asg_desired_capacity
  vpc_zone_identifier = data.aws_subnets.default.ids
  target_group_arns   = [aws_lb_target_group.catalog.arn]
  health_check_type   = "ELB"

  launch_template {
    id      = aws_launch_template.catalog.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "${var.project_name}-catalog"
    propagate_at_launch = true
  }
}
