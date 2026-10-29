resource "aws_launch_template" "catalog" {
  name_prefix   = "${var.project_name}-catalog-"
  image_id      = data.aws_ami.al2023.id
  instance_type = var.catalog_instance_type

  # Phase 7: the default "standard" mode throttles a t3 instance once its burst credits run out, which
  # would silently slow long comparison runs. Unlimited keeps full speed (surplus credits cost extra and
  # are counted in the cost model).
  credit_specification {
    cpu_credits = "unlimited"
  }

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
  min_size            = local.is_static_peak ? var.static_peak_instances : var.catalog_asg_min_size
  max_size            = local.is_static_peak ? var.static_peak_instances : var.catalog_asg_max_size
  desired_capacity    = local.is_static_peak ? var.static_peak_instances : var.catalog_asg_desired_capacity
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

  # The scaler (AWS target tracking, or our engine) owns the desired capacity after creation; a later
  # `terraform apply` must not fight it. Creation still uses the value above.
  lifecycle {
    ignore_changes = [desired_capacity]
  }
}
