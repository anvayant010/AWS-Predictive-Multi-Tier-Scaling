# Remote state backend created by infra/bootstrap (see its outputs.tf ->
# backend_config_snippet). Points at the same S3 bucket + DynamoDB lock
# table, under a distinct state key, so this config's state never collides
# with bootstrap's own (locally-held) state.
terraform {
  backend "s3" {
    bucket         = "predictive-scaling-tfstate-672626785486"
    key            = "main/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "predictive-scaling-tfstate-lock"
    encrypt        = true
  }
}
