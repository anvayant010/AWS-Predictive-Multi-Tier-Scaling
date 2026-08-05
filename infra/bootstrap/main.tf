data "aws_caller_identity" "current" {}

locals {
  # Account ID suffix keeps the bucket name globally unique without needing
  # a manually-chosen, possibly-colliding name.
  state_bucket_name = "${var.project_name}-tfstate-${data.aws_caller_identity.current.account_id}"
  lock_table_name   = "${var.project_name}-tfstate-lock"

  account_id = data.aws_caller_identity.current.account_id
  partition  = "aws"
}

# ---------------------------------------------------------------------------
# Terraform remote state backend
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "tf_state" {
  bucket = local.state_bucket_name

  # State files can contain sensitive values; never allow accidental deletion
  # of the bucket itself via `terraform destroy` run against this config.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket                  = aws_s3_bucket.tf_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tf_lock" {
  name         = local.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

# ---------------------------------------------------------------------------
# Scoped IAM user for this project's Terraform runs + boto3 actuators
# ---------------------------------------------------------------------------

resource "aws_iam_user" "project" {
  name = var.iam_user_name
}

# Access keys are deliberately NOT created here — an access key's secret
# would be written into this Terraform state file. Create it manually:
#   aws iam create-access-key --user-name <iam_user_name>
# (see infra/README.md)

# -- Terraform state backend access (bucket + lock table only) --------------

data "aws_iam_policy_document" "tf_backend" {
  statement {
    sid     = "StateBucketObjectAccess"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [
      "${aws_s3_bucket.tf_state.arn}/*",
    ]
  }

  statement {
    sid       = "StateBucketListing"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.tf_state.arn]
  }

  statement {
    sid = "StateLockTable"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [aws_dynamodb_table.tf_lock.arn]
  }
}

resource "aws_iam_policy" "tf_backend" {
  name   = "${var.project_name}-tf-backend"
  policy = data.aws_iam_policy_document.tf_backend.json
}

# -- Compute: EC2/ASG/ALB (sustained tier) + Lambda/API Gateway (bursty tier) -

data "aws_iam_policy_document" "compute" {
  statement {
    sid = "Ec2ReadAndLifecycle"
    actions = [
      "ec2:Describe*",
      "ec2:RunInstances",
      "ec2:TerminateInstances",
      "ec2:CreateTags",
      "ec2:CreateLaunchTemplate",
      "ec2:CreateLaunchTemplateVersion",
      "ec2:ModifyLaunchTemplate",
      "ec2:DeleteLaunchTemplate",
      "ec2:CreateSecurityGroup",
      "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress",
    ]
    # EC2 does not support resource-level restriction for most of these
    # actions (AWS limitation, not a policy choice) — action list is kept
    # narrow to compensate.
    resources = ["*"]
  }

  statement {
    sid = "AutoScaling"
    actions = [
      "autoscaling:CreateAutoScalingGroup",
      "autoscaling:UpdateAutoScalingGroup",
      "autoscaling:DeleteAutoScalingGroup",
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeScalingActivities",
      "autoscaling:SetDesiredCapacity",
      "autoscaling:CreateOrUpdateTags",
      "autoscaling:DeleteTags",
      "autoscaling:PutScalingPolicy",
      "autoscaling:DeletePolicy",
      "autoscaling:AttachLoadBalancerTargetGroups",
      "autoscaling:DetachLoadBalancerTargetGroups",
    ]
    resources = ["*"] # no resource-level support in this AWS API
  }

  statement {
    sid = "ElasticLoadBalancing"
    actions = [
      "elasticloadbalancing:CreateLoadBalancer",
      "elasticloadbalancing:DeleteLoadBalancer",
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:CreateTargetGroup",
      "elasticloadbalancing:DeleteTargetGroup",
      "elasticloadbalancing:ModifyTargetGroup",
      "elasticloadbalancing:ModifyTargetGroupAttributes",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetGroupAttributes",
      "elasticloadbalancing:RegisterTargets",
      "elasticloadbalancing:DeregisterTargets",
      "elasticloadbalancing:DescribeTargetHealth",
      "elasticloadbalancing:CreateListener",
      "elasticloadbalancing:DeleteListener",
      "elasticloadbalancing:ModifyListener",
      "elasticloadbalancing:DescribeListeners",
      "elasticloadbalancing:AddTags",
      "elasticloadbalancing:DescribeTags",
    ]
    resources = ["*"] # no resource-level support in this AWS API
  }

  statement {
    sid = "LambdaFunctionLifecycle"
    actions = [
      "lambda:CreateFunction",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "lambda:DeleteFunction",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:GetFunctionCodeSigningConfig",
      "lambda:ListFunctions",
      "lambda:ListVersionsByFunction",
      "lambda:PublishVersion",
      "lambda:CreateAlias",
      "lambda:UpdateAlias",
      "lambda:DeleteAlias",
      "lambda:GetAlias",
      "lambda:PutProvisionedConcurrencyConfig",
      "lambda:GetProvisionedConcurrencyConfig",
      "lambda:DeleteProvisionedConcurrencyConfig",
      "lambda:AddPermission",
      "lambda:RemovePermission",
      "lambda:TagResource",
      "lambda:UntagResource",
    ]
    resources = [
      "arn:${local.partition}:lambda:${var.aws_region}:${local.account_id}:function:${var.project_name}-*",
    ]
  }

  statement {
    sid = "ApiGateway"
    actions = [
      "apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:DELETE", "apigateway:PATCH",
      "apigateway:TagResource", "apigateway:UntagResource",
    ]
    resources = ["arn:${local.partition}:apigateway:${var.aws_region}::/*"]
  }

  statement {
    sid = "EventBridgeScheduler"
    actions = [
      "events:PutRule",
      "events:DeleteRule",
      "events:DescribeRule",
      "events:ListRules",
      "events:EnableRule",
      "events:DisableRule",
      "events:PutTargets",
      "events:RemoveTargets",
      "events:ListTargetsByRule",
    ]
    resources = [
      "arn:${local.partition}:events:${var.aws_region}:${local.account_id}:rule/${var.project_name}-*",
    ]
  }
}

resource "aws_iam_policy" "compute" {
  name   = "${var.project_name}-compute"
  policy = data.aws_iam_policy_document.compute.json
}

# -- Observability + project-scoped app state (CloudWatch, logs, DynamoDB, SSM) -

data "aws_iam_policy_document" "observability_and_state" {
  statement {
    sid = "CloudWatchMetricsAndAlarms"
    actions = [
      "cloudwatch:PutMetricData",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DeleteAlarms",
      "cloudwatch:PutDashboard",
      "cloudwatch:GetDashboard",
      "cloudwatch:DeleteDashboards",
    ]
    resources = ["*"] # CloudWatch metrics/alarms have no useful resource-level scoping
  }

  statement {
    sid = "LambdaExecutionLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:GetLogEvents",
    ]
    resources = [
      "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${var.project_name}-*",
      "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${var.project_name}-*:*",
    ]
  }

  # App-level DynamoDB tables (e.g. the actuation kill switch), distinct
  # from the Terraform lock table above.
  statement {
    sid = "AppDynamoDbTables"
    actions = [
      "dynamodb:CreateTable",
      "dynamodb:DeleteTable",
      "dynamodb:DescribeTable",
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [
      "arn:${local.partition}:dynamodb:${var.aws_region}:${local.account_id}:table/${var.project_name}-*",
    ]
  }

  statement {
    sid = "KillSwitchSsmParameter"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:PutParameter",
      "ssm:DeleteParameter",
    ]
    resources = [
      "arn:${local.partition}:ssm:${var.aws_region}:${local.account_id}:parameter/${var.project_name}/*",
    ]
  }
}

resource "aws_iam_policy" "observability_and_state" {
  name   = "${var.project_name}-observability-and-state"
  policy = data.aws_iam_policy_document.observability_and_state.json
}

# -- IAM: create execution roles for Lambda / EC2, scoped by name prefix ----
# (Required because Terraform/boto3 will need to create the Lambda execution
# role and the EC2 instance profile as part of provisioning demo_app.)

data "aws_iam_policy_document" "iam_role_management" {
  statement {
    sid = "ManageProjectRoles"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:TagRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:PassRole",
    ]
    resources = [
      "arn:${local.partition}:iam::${local.account_id}:role/${var.project_name}-*",
    ]
  }

  statement {
    sid = "ManageProjectInstanceProfiles"
    actions = [
      "iam:CreateInstanceProfile",
      "iam:DeleteInstanceProfile",
      "iam:GetInstanceProfile",
      "iam:AddRoleToInstanceProfile",
      "iam:RemoveRoleFromInstanceProfile",
    ]
    resources = [
      "arn:${local.partition}:iam::${local.account_id}:instance-profile/${var.project_name}-*",
    ]
  }
}

resource "aws_iam_policy" "iam_role_management" {
  name   = "${var.project_name}-iam-role-management"
  policy = data.aws_iam_policy_document.iam_role_management.json
}

# -- Attach everything to the project user -----------------------------------

resource "aws_iam_user_policy_attachment" "tf_backend" {
  user       = aws_iam_user.project.name
  policy_arn = aws_iam_policy.tf_backend.arn
}

resource "aws_iam_user_policy_attachment" "compute" {
  user       = aws_iam_user.project.name
  policy_arn = aws_iam_policy.compute.arn
}

resource "aws_iam_user_policy_attachment" "observability_and_state" {
  user       = aws_iam_user.project.name
  policy_arn = aws_iam_policy.observability_and_state.arn
}

resource "aws_iam_user_policy_attachment" "iam_role_management" {
  user       = aws_iam_user.project.name
  policy_arn = aws_iam_policy.iam_role_management.arn
}
