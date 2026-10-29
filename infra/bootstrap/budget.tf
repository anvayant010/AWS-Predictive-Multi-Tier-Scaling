# ---------------------------------------------------------------------------
# Cost guardrail: monthly budget with email alerts
# ---------------------------------------------------------------------------
# Lives in bootstrap (not infra/) on purpose: `make destroy` tears down infra/, and an alarm that is
# deleted along with the thing it watches protects nothing.
#
# This is an ALARM, not a brake: it cannot stop spending, and AWS billing data lags by hours, so it
# catches "left running for days", not "left running for an hour". The scoped project IAM user needs
# NO budget permissions — this is applied by the admin-capable identity that applies bootstrap.
# Budgets emails the address below directly (no SNS topic). If AWS sends a confirmation email after
# the first apply, confirm it; if none arrives, that is expected. Billing data lags by hours, so the
# alert path cannot be tested instantly.

locals {
  # Alert when ACTUAL spend crosses 50/80/100% of the limit, and when FORECASTED spend will pass 100%.
  budget_notifications = [
    { threshold = 50, type = "ACTUAL" },
    { threshold = 80, type = "ACTUAL" },
    { threshold = 100, type = "ACTUAL" },
    { threshold = 100, type = "FORECASTED" },
  ]
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project_name}-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = local.budget_notifications
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
