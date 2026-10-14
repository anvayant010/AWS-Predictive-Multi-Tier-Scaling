"""Cost model for the Phase 7 comparison: capacity-hours x published prices (decision D3/D9).

The AWS bill lags by hours and cannot be split per run, so each run is costed from what it actually held:
how many instances and how much provisioned concurrency were running, for how long, plus the requests it
served. The same function costs a real run, a simulated run and the pre-run spend estimate, so the arms are
always priced identically.

PRICES ARE ESTIMATES, NOT AN INVOICE. They are the us-east-1 list prices as read from public pages on
2026-10-13 (mostly third-party pages quoting AWS; the T3 surplus-credit rate is on AWS's own T3 page). They
were NOT checked against AWS's live price list, so every result carries `prices_verified: False` until
someone does that. They sit in one frozen dataclass so correcting a number is a one-line change.

Simplifications (all small at this scale): EC2 per-second billing without the 60-second minimum; provisioned
concurrency without AWS's round-up to 5 minutes per activation; Lambda INIT time and data transfer ignored;
CloudWatch, logs and the state bucket ignored; ALB capacity units assumed (they are tiny at these volumes).
Unlimited-credit surcharge is an UPPER BOUND unless a measured CPU utilisation is supplied.

Capacity log format (JSON lines, written by the Phase 7 run script, one row per sample, rows may interleave):
    {"t": <epoch seconds>, "service": "catalog_service",   "instances": <int>}
    {"t": <epoch seconds>, "service": "thumbnail_service", "provisioned": <int>}
A sample's value holds until the next sample of the same service (and the last one until the run's end).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

CATALOG = "catalog_service"
THUMBNAIL = "thumbnail_service"
SECONDS_PER_HOUR = 3600.0

# The project-wide hard caps: a sample above these means the sampler or the run is broken.
MAX_INSTANCES = 10
MAX_PROVISIONED = 10


@dataclass(frozen=True)
class Prices:
    """us-east-1 list prices in USD. See the module docstring for their status."""

    t3_micro_per_hour: float = 0.0104
    t3_micro_vcpus: int = 2
    t3_micro_baseline_utilization: float = 0.20  # 10% per vCPU, as a fraction of the whole instance
    unlimited_surcharge_per_vcpu_hour: float = 0.05  # only for CPU above baseline once credits are spent
    alb_per_hour: float = 0.0225
    alb_per_lcu_hour: float = 0.008
    lambda_per_million_requests: float = 0.20
    lambda_per_gb_second: float = 0.0000166667  # on-demand execution
    lambda_provisioned_per_gb_second: float = 0.0000041667  # holding provisioned concurrency
    lambda_provisioned_exec_per_gb_second: float = 0.0000097222  # executing on provisioned concurrency
    apigw_http_per_million_requests: float = 1.00
    as_of: str = "2026-10-13"
    verified: bool = False


PRICES = Prices()


@dataclass(frozen=True)
class LambdaShape:
    memory_mb: int = 128  # infra/variables.tf default
    billed_ms_per_request: float = 100.0  # ASSUMED handler duration (policy.py uses 0.1 s); replace with a measurement

    def __post_init__(self) -> None:
        if self.memory_mb <= 0 or self.billed_ms_per_request < 0:
            raise ValueError("invalid Lambda shape")

    @property
    def gb(self) -> float:
        return self.memory_mb / 1024.0


# ---------------------------------------------------------------------------------------------- integration


def integrate_step(samples: list[tuple[float, float]], end: float, *, cap: float | None = None) -> float:
    """Area under a step function: sum(value x seconds), each value held until the next sample, the last until `end`."""
    if not samples:
        return 0.0
    times = [t for t, _ in samples]
    if any(b < a for a, b in zip(times, times[1:])):
        raise ValueError("capacity samples must be in time order")
    if end < times[-1]:
        raise ValueError("the run end is before the last capacity sample")
    total = 0.0
    for i, (t, value) in enumerate(samples):
        if value < 0 or not math.isfinite(value):
            raise ValueError("capacity samples must be finite and non-negative")
        if cap is not None and value > cap:
            raise ValueError(f"capacity sample {value} exceeds the hard cap {cap}")
        until = samples[i + 1][0] if i + 1 < len(samples) else end
        total += value * (until - t)
    return total


def read_capacity_log(path: Path) -> dict[str, list[tuple[float, float]]]:
    """Capacity log -> {"catalog_service": [(t, instances)], "thumbnail_service": [(t, provisioned)]}."""
    out: dict[str, list[tuple[float, float]]] = {CATALOG: [], THUMBNAIL: []}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        service = row["service"]
        if service == CATALOG:
            out[CATALOG].append((float(row["t"]), float(row["instances"])))
        elif service == THUMBNAIL:
            out[THUMBNAIL].append((float(row["t"]), float(row["provisioned"])))
        else:
            raise ValueError(f"unknown service in capacity log: {service!r}")
    for samples in out.values():
        samples.sort(key=lambda s: s[0])
    return out


# ---------------------------------------------------------------------------------------------------- parts


def instance_hours(samples: list[tuple[float, float]], end: float) -> float:
    return integrate_step(samples, end, cap=MAX_INSTANCES) / SECONDS_PER_HOUR


def provisioned_unit_hours(samples: list[tuple[float, float]], end: float) -> float:
    return integrate_step(samples, end, cap=MAX_PROVISIONED) / SECONDS_PER_HOUR


def ec2_cost(hours: float, prices: Prices = PRICES, cpu_utilization: float = 1.0) -> dict[str, float]:
    """Instance-hours at list price plus the unlimited-credit surcharge.

    `cpu_utilization` (0-1, fraction of the whole instance) defaults to 1.0, an upper bound. Only CPU above
    the baseline is charged, and only after the instance's credits are spent, so the true figure is lower.
    """
    if not 0 <= cpu_utilization <= 1:
        raise ValueError("cpu_utilization must be in [0, 1]")
    over_baseline = max(0.0, cpu_utilization - prices.t3_micro_baseline_utilization)
    surcharge_per_hour = over_baseline * prices.t3_micro_vcpus * prices.unlimited_surcharge_per_vcpu_hour
    return {
        "ec2_instances": hours * prices.t3_micro_per_hour,
        "ec2_unlimited_surcharge_upper_bound": hours * surcharge_per_hour,
    }


def alb_cost(stack_hours: float, prices: Prices = PRICES, lcu_per_hour: float = 1.0) -> float:
    return stack_hours * (prices.alb_per_hour + lcu_per_hour * prices.alb_per_lcu_hour)


def lambda_cost(
    *,
    requests: int,
    provisioned_hours: float,
    provisioned_fraction: float = 0.0,
    shape: LambdaShape = LambdaShape(),
    prices: Prices = PRICES,
) -> dict[str, float]:
    """Requests + API Gateway + execution time + the cost of merely holding provisioned concurrency."""
    if requests < 0 or provisioned_hours < 0:
        raise ValueError("requests and provisioned hours cannot be negative")
    if not 0 <= provisioned_fraction <= 1:
        raise ValueError("provisioned_fraction must be in [0, 1]")
    seconds_each = shape.billed_ms_per_request / 1000.0
    served_provisioned = requests * provisioned_fraction
    served_on_demand = requests - served_provisioned
    return {
        "lambda_requests": requests / 1e6 * prices.lambda_per_million_requests,
        "api_gateway": requests / 1e6 * prices.apigw_http_per_million_requests,
        "lambda_execution_on_demand": served_on_demand * seconds_each * shape.gb * prices.lambda_per_gb_second,
        "lambda_execution_provisioned": served_provisioned
        * seconds_each
        * shape.gb
        * prices.lambda_provisioned_exec_per_gb_second,
        "lambda_provisioned_holding": provisioned_hours
        * SECONDS_PER_HOUR
        * shape.gb
        * prices.lambda_provisioned_per_gb_second,
    }


# ------------------------------------------------------------------------------------------------ one run


def run_cost(
    *,
    capacity: dict[str, list[tuple[float, float]]],
    end: float,
    stack_start: float,
    thumbnail_requests: int,
    provisioned_fraction: float = 0.0,
    cpu_utilization: float = 1.0,
    shape: LambdaShape = LambdaShape(),
    prices: Prices = PRICES,
) -> dict:
    """Cost of one run from its capacity log. `stack_start` .. `end` is how long the ALB etc. existed."""
    if end < stack_start:
        raise ValueError("the run ends before the stack started")
    stack_hours = (end - stack_start) / SECONDS_PER_HOUR
    hours = instance_hours(capacity.get(CATALOG, []), end)
    pc_hours = provisioned_unit_hours(capacity.get(THUMBNAIL, []), end)
    parts = {
        **ec2_cost(hours, prices, cpu_utilization),
        "alb": alb_cost(stack_hours, prices),
        **lambda_cost(
            requests=thumbnail_requests,
            provisioned_hours=pc_hours,
            provisioned_fraction=provisioned_fraction,
            shape=shape,
            prices=prices,
        ),
    }
    return {
        "total_usd": sum(parts.values()),
        "parts_usd": parts,
        "instance_hours": hours,
        "provisioned_unit_hours": pc_hours,
        "stack_hours": stack_hours,
        "assumptions": {
            "cpu_utilization": cpu_utilization,
            "lambda_billed_ms_per_request": shape.billed_ms_per_request,
            "lambda_memory_mb": shape.memory_mb,
        },
        "prices_as_of": prices.as_of,
        "prices_verified": prices.verified,
        "note": "estimate from capacity-hours x list prices, not an invoice",
    }


# ------------------------------------------------------------------------------------- pre-run estimate


def estimate_before_run(
    *,
    scenario_minutes: float,
    setup_minutes: float = 25.0,
    max_instances: int = MAX_INSTANCES,
    max_provisioned: int = MAX_PROVISIONED,
    expected_thumbnail_requests: int = 0,
    cpu_utilization: float = 1.0,
    shape: LambdaShape = LambdaShape(),
    prices: Prices = PRICES,
) -> dict:
    """Upper bound on one run's spend, printed before the run starts (decision D9).

    Worst case: the full `max_instances` and `max_provisioned` held for the whole stack lifetime (scenario
    plus setup/teardown time), and the surcharge at the given utilisation. A real run should come in well
    under it; if it does not, something is wrong and the run's time limit and teardown are the backstop.
    """
    if scenario_minutes <= 0 or setup_minutes < 0:
        raise ValueError("scenario_minutes must be positive and setup_minutes non-negative")
    if not 1 <= max_instances <= MAX_INSTANCES or not 0 <= max_provisioned <= MAX_PROVISIONED:
        raise ValueError("instance/provisioned maxima must respect the hard caps")
    stack_seconds = (scenario_minutes + setup_minutes) * 60.0
    capacity = {
        CATALOG: [(0.0, float(max_instances))],
        THUMBNAIL: [(0.0, float(max_provisioned))],
    }
    out = run_cost(
        capacity=capacity,
        end=stack_seconds,
        stack_start=0.0,
        thumbnail_requests=expected_thumbnail_requests,
        provisioned_fraction=1.0 if max_provisioned else 0.0,
        cpu_utilization=cpu_utilization,
        shape=shape,
        prices=prices,
    )
    out["kind"] = "upper bound for one run"
    return out
