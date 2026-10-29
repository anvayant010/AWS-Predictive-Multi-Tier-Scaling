# Remote state backend created by infra/bootstrap (see its outputs.tf ->
# backend_config_snippet). Points at the same S3 bucket + DynamoDB lock
# table, under a distinct state key, so this config's state never collides
# with bootstrap's own (locally-held) state.
#
# The bucket name contains the AWS account ID, so it is NOT written here. It lives in
# a gitignored backend.hcl (copy backend.hcl.example) and is supplied at init time:
#   terraform init -backend-config=backend.hcl
terraform {
  backend "s3" {
    key            = "main/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "predictive-scaling-tfstate-lock"
    encrypt        = true
    profile        = "predictive-scaling"
  }
}
