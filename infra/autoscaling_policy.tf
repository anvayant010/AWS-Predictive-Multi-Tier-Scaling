# Scaling arm for the catalog ASG (Phase 7). Only the two reactive modes create an AWS scaling policy;
# `predictive` is driven by our engine and `static_peak` is pinned in ec2_asg.tf.

locals {
  is_static_peak = var.scaling_mode == "static_peak"

  reactive_utilization = {
    reactive_standard   = var.reactive_standard_utilization
    reactive_aggressive = var.reactive_aggressive_utilization
  }

  is_reactive = contains(keys(local.reactive_utilization), var.scaling_mode)

  # ALBRequestCountPerTarget is requests per target per MINUTE, the same unit as the measured capacity.
  reactive_target_value = local.is_reactive && var.catalog_capacity_rpm_per_instance != null ? (
    var.catalog_capacity_rpm_per_instance * local.reactive_utilization[var.scaling_mode]
  ) : null
}

resource "aws_autoscaling_policy" "catalog_reactive" {
  count = local.is_reactive ? 1 : 0

  name                   = "${var.project_name}-catalog-${replace(var.scaling_mode, "_", "-")}"
  autoscaling_group_name = aws_autoscaling_group.catalog.name
  policy_type            = "TargetTrackingScaling"

  # Everything else is left at AWS defaults on purpose (warm-up, alarm timing, scale-in enabled): the
  # baseline is "what a normal user would configure", not a tuned or crippled one.
  target_tracking_configuration {
    target_value = local.reactive_target_value

    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.catalog.arn_suffix}/${aws_lb_target_group.catalog.arn_suffix}"
    }
  }

  # The metric only exists once the listener is routing to the target group.
  depends_on = [aws_lb_listener.catalog_http]

  lifecycle {
    precondition {
      condition     = var.catalog_capacity_rpm_per_instance != null
      error_message = "Reactive modes need catalog_capacity_rpm_per_instance, the value MEASURED in the calibration run. There is deliberately no default, so an assumed number cannot be used by mistake."
    }
  }
}
