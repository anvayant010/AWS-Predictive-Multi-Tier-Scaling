"""Shared test doubles.

moto emulates Lambda functions and aliases but NOT provisioned concurrency (put/get/delete all answer
"Not yet implemented"), so `StatefulLambda` stands in for those three calls. It keeps one configuration
in memory and uses AWS's real error codes; everything else (get_alias, ...) is delegated to moto.
It proves our own logic, NOT AWS's real API contract (async allocation, quotas): that is verified in the
real-AWS session (Phase 6f).
"""

from botocore.exceptions import ClientError


class StatefulLambda:
    """Real (moto) client for functions/aliases; in-memory provisioned concurrency with AWS error codes."""

    def __init__(self, real) -> None:
        self._real = real
        self.config: dict | None = None
        self.calls: list[tuple[str, int | None]] = []

    def __getattr__(self, name):  # get_alias etc. -> moto
        return getattr(self._real, name)

    def _not_found(self, operation: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": "ProvisionedConcurrencyConfigNotFoundException", "Message": "none"}}, operation
        )

    def put_provisioned_concurrency_config(self, FunctionName, Qualifier, ProvisionedConcurrentExecutions):
        assert ProvisionedConcurrentExecutions >= 1, "AWS rejects 0 units (botocore enforces min 1)"
        self._real.get_alias(FunctionName=FunctionName, Name=Qualifier)  # raises if the alias is missing
        self.calls.append(("put", ProvisionedConcurrentExecutions))
        self.config = {
            "RequestedProvisionedConcurrentExecutions": ProvisionedConcurrentExecutions,
            "AvailableProvisionedConcurrentExecutions": 0,
            "AllocatedProvisionedConcurrentExecutions": 0,
            "Status": "IN_PROGRESS",
        }
        return self.config

    def get_provisioned_concurrency_config(self, FunctionName, Qualifier):
        if self.config is None:
            raise self._not_found("GetProvisionedConcurrencyConfig")
        return self.config

    def delete_provisioned_concurrency_config(self, FunctionName, Qualifier):
        if self.config is None:
            raise self._not_found("DeleteProvisionedConcurrencyConfig")
        self.calls.append(("delete", None))
        self.config = None
