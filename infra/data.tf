data "aws_caller_identity" "current" {}

# Use the account's default VPC/subnets rather than provisioning a new
# network — this is a demo workload, not a project about VPC design.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Looked up via ec2:DescribeImages (already covered by the scoped IAM
# user's "ec2:Describe*" grant) rather than the public SSM parameter path,
# which that user has no access to (bootstrap's SSM permissions are scoped
# to /predictive-scaling/* only).
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}
