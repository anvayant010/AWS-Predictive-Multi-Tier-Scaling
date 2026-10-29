# infra/

Terraform for this project, split into two configs:

- **`bootstrap/`** — one-time setup. Creates the S3 bucket + DynamoDB table
  used as the Terraform remote state backend, and a scoped IAM user
  (`predictive-scaling-engine`) for everything else this project does.
  Uses local state (it can't depend on the backend it's creating).
- **(later, Phase 1+)** — the actual project infra (ASG, Lambda, API Gateway,
  ALB, IAM roles, DB) — `main.tf`, `ec2_asg.tf`, `lambda.tf`, etc., living
  directly under `infra/`, using the S3 backend that bootstrap creates.

## Running the bootstrap (one-time)

This creates real AWS resources (an S3 bucket, a DynamoDB table, an IAM user
and its policies — no compute, so no ongoing cost beyond a few cents of S3/DynamoDB
storage). Run it yourself; don't let an agent run `apply` unattended.

```bash
cd infra/bootstrap
terraform init
terraform plan     # review before applying
terraform apply
```

## Initialising the main config

`infra/backend.tf` deliberately leaves the state bucket name out (it contains your
AWS account ID). Copy `backend.hcl.example` to `backend.hcl` (gitignored), put your
bucket name in it, and pass it at init time:

```bash
cd infra
cp backend.hcl.example backend.hcl    # then edit the bucket name
terraform init -backend-config=backend.hcl
```

## After apply: create the IAM user's access key

The access key is deliberately **not** created by Terraform — its secret
would otherwise be written into the state file. Create it manually:

```bash
aws iam create-access-key --user-name predictive-scaling-engine
```

Store the resulting `AccessKeyId` / `SecretAccessKey` as a new named AWS CLI
profile (never in the repo, never in `.env` files that get committed):

```bash
aws configure --profile predictive-scaling
```

From then on, use `AWS_PROFILE=predictive-scaling` (or `--profile
predictive-scaling`) for every command related to this project — Terraform
plans/applies, boto3 actuator calls, everything. This keeps all of this
project's AWS activity physically scoped to the least-privilege policies
defined in `bootstrap/main.tf`, separate from any other credentials on this
machine.

## What the scoped user can and can't do

`bootstrap/main.tf` attaches four policies to the user:

| Policy | Covers |
|---|---|
| `*-tf-backend` | Read/write the state bucket + lock table only |
| `*-compute` | EC2/ASG/ALB, Lambda (scoped to `predictive-scaling-*` function names), API Gateway, EventBridge rules (scoped to `predictive-scaling-*`) |
| `*-observability-and-state` | CloudWatch metrics/alarms, Lambda logs (scoped to `/aws/lambda/predictive-scaling-*`), DynamoDB app tables and SSM parameters (scoped to `predictive-scaling-*` / `/predictive-scaling/*`) |
| `*-iam-role-management` | Create/manage IAM roles and instance profiles, but only ones named `predictive-scaling-*` — needed so Terraform can create the Lambda execution role and EC2 instance profile |

No statement uses `"Action": "*"` or a blanket `"Resource": "*"` across
services. A few EC2/ASG/ELB actions do use `Resource: "*"` — that's an AWS
API limitation (those services don't support resource-level IAM conditions
for most actions), not a policy choice; the action list is kept narrow to
compensate.

This policy set will need small additions as later phases land (e.g. RDS/
TimescaleDB permissions once Phase 3 settles on where metrics are stored) —
extend `bootstrap/main.tf` incrementally rather than widening scope now.

## Wiring up the main infra's backend (Phase 1+)

After bootstrap apply, `terraform output backend_config_snippet` prints the
exact `backend "s3" { ... }` block to paste into `infra/backend.tf` once
Phase 1 starts adding real project resources.

## Teardown

Bootstrap resources (state bucket, lock table, IAM user) are meant to be
long-lived across the whole project, unlike the ephemeral compute resources
described in `docs/design.md` §2.1. Only run `terraform destroy` here if
you're tearing down the entire project for good — and note the state bucket
has `prevent_destroy` set, so you'd need to remove that lifecycle block
first.
