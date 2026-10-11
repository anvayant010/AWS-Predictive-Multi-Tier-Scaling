# Project tasks. Run from the repo root.
#
#   make test               run the unit tests (no AWS, no DB needed)
#   make destroy            tear down ALL billable project resources, then verify (asks first)
#   make destroy AUTO_APPROVE=yes   same, without the interactive prompt (for scripted use)
#   make verify-teardown    read-only check that nothing billable is still running
#   make kill               ENGAGE the kill switch: blocks all scaling actions (the panic button)
#   make kill-status        show whether the kill switch is engaged
# Releasing the kill switch has deliberately no shortcut:
#   python -m decision_engine.kill_switch release --yes

# Prefer the project virtualenv's Python (Windows layout first, then POSIX); fall back to PATH.
PYTHON ?= $(firstword $(wildcard .venv/Scripts/python.exe .venv/bin/python) python)

.PHONY: test destroy verify-teardown kill kill-status

test:
	$(PYTHON) -m pytest tests/ -v

# Only tears down infra/ (ALB, ASG, Lambda, API Gateway, ...). infra/bootstrap (state bucket, lock
# table, IAM user) is deliberately NOT touched: it is persistent and costs ~nothing.
# `terraform destroy` prompts for "yes" unless AUTO_APPROVE=yes. The verification step always runs
# and fails the target if anything billable is left (or if it could not check).
destroy:
	cd infra && terraform destroy $(if $(filter yes,$(AUTO_APPROVE)),-auto-approve,)
	$(MAKE) verify-teardown

verify-teardown:
	$(PYTHON) -m scripts.verify_teardown

kill:
	$(PYTHON) -m decision_engine.kill_switch engage

kill-status:
	$(PYTHON) -m decision_engine.kill_switch status
