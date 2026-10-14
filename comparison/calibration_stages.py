"""The measurement stages of the Phase 7 calibration session (decision D5). These do the real I/O.

Everything that touches the outside world is injected (`StageEnv` for HTTP, time and sleeping; boto3
clients passed in), so the logic is tested with fakes and a local server and only the final session touches
AWS. What the measurements MEAN is decided in comparison/calibration.py.

Capacity changes made here go through the existing actuators (decision_engine.actuator_ec2 /
actuator_lambda), so their cap checks apply, and every stage passes its own much lower caps: the ASG never
goes above 2 instances and provisioned concurrency never above 1. They do NOT go through the scaling loop's
kill switch (the session is the operator's explicit, supervised action); the session script says so up front.

Open-loop load: requests are launched at pre-drawn Poisson arrival times whatever the server does, so a slow
server cannot slow the load down and hide its own latency (see load_generator/scenario_player.py).
"""

from __future__ import annotations

import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from botocore.exceptions import ClientError

from comparison.calibration import IdleProbe, LoadStep, step_passes, summarize_step
from decision_engine.actuator_ec2 import scale_ec2
from decision_engine.actuator_lambda import provision_lambda, provisioned_status
from load_generator.scenario_player import parse_marker

CATALOG_MIX = (("/health", 1), ("/products", 5), ("/products?category={cat}", 3), ("/products/{pid}", 4))
CATEGORIES = ("electronics", "home", "office")
BOOT_MAX_INSTANCES = 2  # boot timing never takes the ASG above this
PROVISIONED_MAX = 1  # nor provisioned concurrency above this


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    ms: float
    body: str | None
    error: str | None = None


def urllib_get(url: str, timeout: float = 15.0) -> HttpResult:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return HttpResult(response.status, (time.perf_counter() - start) * 1000, body)
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, (time.perf_counter() - start) * 1000, None, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001  (timeouts, resets, DNS: all recorded as a failed request)
        return HttpResult(None, (time.perf_counter() - start) * 1000, None, f"{type(exc).__name__}: {exc}"[:200])


@dataclass
class StageEnv:
    catalog_url: str = ""  # e.g. http://<alb-dns>
    thumbnail_url: str = ""  # the API invoke URL, e.g. https://xxxx.execute-api.us-east-1.amazonaws.com
    http_get: Callable[[str], HttpResult] = urllib_get
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    stop: threading.Event = field(default_factory=threading.Event)

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`, waking early if the session is being stopped. False means stopped."""
        end = self.clock() + seconds
        while not self.stop.is_set():
            remaining = end - self.clock()
            if remaining <= 0:
                return True
            self.sleep(min(1.0, remaining))
        return False


# ---------------------------------------------------------------------------------------------------- load


def catalog_path(rng: random.Random) -> str:
    template = rng.choices([p for p, _ in CATALOG_MIX], weights=[w for _, w in CATALOG_MIX])[0]
    return template.format(cat=rng.choice(CATEGORIES), pid=rng.randint(1, 7))


def thumbnail_path(rng: random.Random) -> str:
    return f"/thumbnail?image_id={rng.getrandbits(64):x}&width={rng.choice((64, 128, 256))}&height={rng.choice((64, 128, 256))}"


def _fire(env: StageEnv, url: str, scheduled_offset: float, run_start: float, barrier: threading.Barrier | None = None) -> dict:
    if barrier is not None:
        barrier.wait(timeout=30)
    started = env.clock()
    result = env.http_get(url)
    marker = parse_marker(result.body) if result.status == 200 else parse_marker(None)
    return {
        "t": scheduled_offset,
        "late_ms": max(0.0, (started - (run_start + scheduled_offset)) * 1000),
        "ms": result.ms,
        "ok": result.status == 200,
        "status": result.status,
        "error": result.error,
        "cold": marker.cold_start,
        "container": marker.container_id,
        "init_type": marker.init_type,
    }


def drive_constant_rate(
    env: StageEnv, base_url: str, make_path: Callable[[random.Random], str], rpm: float, seconds: float, *, workers: int = 128, seed: int = 1
) -> list[dict]:
    """Open-loop Poisson load at `rpm` for `seconds`. Returns one row per request (t = scheduled offset in s)."""
    if rpm < 0 or seconds <= 0:
        raise ValueError("rpm must be non-negative and seconds positive")
    rng = random.Random(seed)
    rows: list[dict] = []
    if rpm == 0:
        env.wait(seconds)
        return rows
    start = env.clock()
    offset = 0.0
    futures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while not env.stop.is_set():
            offset += rng.expovariate(rpm / 60.0)
            if offset >= seconds:
                break
            delay = start + offset - env.clock()
            if delay > 0:
                env.sleep(delay)
            futures.append(pool.submit(_fire, env, base_url + make_path(rng), offset, start))
        rows = [f.result() for f in futures]
    return rows


def parallel_burst(env: StageEnv, base_url: str, make_path: Callable[[random.Random], str], k: int, seed: int = 1) -> list[dict]:
    """`k` requests released at the same instant, so each needs its own container."""
    rng = random.Random(seed)
    barrier = threading.Barrier(k)
    start = env.clock()
    with ThreadPoolExecutor(max_workers=k) as pool:
        futures = [pool.submit(_fire, env, base_url + make_path(rng), 0.0, start, barrier) for _ in range(k)]
        return [f.result() for f in futures]


# ----------------------------------------------------------------------------------------- stage: the knee


def run_knee(
    env: StageEnv, rates: list[float], *, settle_s: float, measure_s: float, workers: int = 128,
    stop_after_failures: int = 2, latency_factor: float = 2.0, max_error_rate: float = 0.01, min_fidelity: float = 0.95,
) -> dict:
    """Step the catalog load up until the server (or the generator) gives out. Rows before `settle_s` are dropped."""
    steps: list[LoadStep] = []
    windows: list[dict] = []
    raw: list[dict] = []
    unloaded_p95 = None
    failures = 0
    for i, rate in enumerate(sorted(rates)):
        if env.stop.is_set():
            break
        wall_start = env.wall()
        rows = drive_constant_rate(env, env.catalog_url, catalog_path, rate, settle_s + measure_s, workers=workers, seed=100 + i)
        wall_end = env.wall()
        measured = [r for r in rows if r["t"] >= settle_s]
        step = summarize_step(measured, rate, measure_s)
        steps.append(step)
        windows.append({"offered_rpm": rate, "wall_start": wall_start, "wall_end": wall_end,
                        "max_late_ms": max((r["late_ms"] for r in rows), default=0.0)})
        raw.append({"offered_rpm": rate, "rows": rows})
        if unloaded_p95 is None:
            unloaded_p95 = step.p95_ms
            if unloaded_p95 is None:
                break  # the baseline step failed entirely: nothing sensible to climb from
            continue
        passed, _reason = step_passes(step, unloaded_p95, latency_factor, max_error_rate, min_fidelity)
        failures = 0 if passed else failures + 1
        if failures >= stop_after_failures:
            break
    return {"steps": steps, "windows": windows, "raw": raw}


# ----------------------------------------------------------------------------- stage: thumbnail cold starts


def run_cold_start_probe(env: StageEnv, *, parallel: int = 8, warm_calls: int = 40, warm_spacing_s: float = 0.2) -> list[dict]:
    """Right after deployment every container is new: a parallel burst (all cold), warm calls, then another burst."""
    rows: list[dict] = []
    rows += parallel_burst(env, env.thumbnail_url, thumbnail_path, parallel, seed=1)
    rng = random.Random(2)
    start = env.clock()
    for _ in range(warm_calls):
        if env.stop.is_set():
            break
        rows.append(_fire(env, env.thumbnail_url + thumbnail_path(rng), env.clock() - start, start))
        env.wait(warm_spacing_s)
    rows += parallel_burst(env, env.thumbnail_url, thumbnail_path, parallel, seed=3)
    return rows


def run_idle_staircase(env: StageEnv, gap_minutes: list[float], *, parallel: int = 6) -> dict:
    """Idle gaps of growing length; after each, which previously known containers answer again?"""
    rows: list[dict] = []
    probes: list[IdleProbe] = []
    first = parallel_burst(env, env.thumbnail_url, thumbnail_path, parallel, seed=10)
    rows += first
    known = frozenset(r["container"] for r in first if r["container"])
    for i, gap in enumerate(sorted(gap_minutes)):
        if not env.wait(gap * 60.0):
            break
        burst = parallel_burst(env, env.thumbnail_url, thumbnail_path, parallel, seed=11 + i)
        rows += burst
        seen = frozenset(r["container"] for r in burst if r["container"])
        probes.append(IdleProbe(gap, known, seen))
        known = seen  # every container that just answered has just been used
    return {"probes": probes, "rows": rows}


# ------------------------------------------------------------------------------------ stage: boot timings


def _healthy_targets(elbv2, target_group_arn: str) -> int:
    health = elbv2.describe_target_health(TargetGroupArn=target_group_arn)
    return sum(1 for d in health["TargetHealthDescriptions"] if d["TargetHealth"]["State"] == "healthy")


def _wait_for(env: StageEnv, condition: Callable[[], bool], timeout_s: float, poll_s: float) -> bool:
    deadline = env.clock() + timeout_s
    while not env.stop.is_set():
        if condition():
            return True
        if env.clock() >= deadline:
            return False
        env.wait(poll_s)
    return False


def run_boot_timing(env: StageEnv, autoscaling, elbv2, asg_name: str, target_group_arn: str, repeats: int,
                    *, timeout_s: float = 600.0, poll_s: float = 5.0) -> dict:
    """Seconds from asking for a second instance to it being healthy behind the load balancer, repeated."""
    seconds: list[float | None] = []
    for _ in range(repeats):
        if env.stop.is_set():
            break
        t0 = env.clock()
        scale_ec2(autoscaling, asg_name, 2, min_capacity=1, max_capacity=BOOT_MAX_INSTANCES, dry_run=False)
        ready = _wait_for(env, lambda: _healthy_targets(elbv2, target_group_arn) >= 2, timeout_s, poll_s)
        seconds.append(env.clock() - t0 if ready else None)
        scale_ec2(autoscaling, asg_name, 1, min_capacity=1, max_capacity=BOOT_MAX_INSTANCES, dry_run=False)
        _wait_for(env, lambda: _healthy_targets(elbv2, target_group_arn) <= 1, timeout_s, poll_s)
    return {"seconds": [s for s in seconds if s is not None], "timeouts": sum(1 for s in seconds if s is None)}


def run_provisioned_timing(env: StageEnv, lambda_client, function_name: str, alias: str, repeats: int,
                           *, timeout_s: float = 900.0, poll_s: float = 5.0) -> dict:
    """Seconds from requesting 1 unit of provisioned concurrency to READY, plus a check that it really serves."""
    seconds: list[float | None] = []
    serve_checks: list[dict] = []

    def status():
        return provisioned_status(lambda_client, function_name, alias)

    for i in range(repeats):
        if env.stop.is_set():
            break
        t0 = env.clock()
        provision_lambda(lambda_client, function_name, alias, 1, min_concurrency=0, max_concurrency=PROVISIONED_MAX, dry_run=False)
        ready = _wait_for(env, lambda: (s := status()) is not None and s.status in ("READY", "FAILED"), timeout_s, poll_s)
        final = status()
        ok = ready and final is not None and final.status == "READY"
        seconds.append(env.clock() - t0 if ok else None)
        if ok:
            probe = parallel_burst(env, env.thumbnail_url, thumbnail_path, 1, seed=50 + i)[0]
            serve_checks.append({"init_type": probe["init_type"], "cold": probe["cold"], "ok": probe["ok"]})
        provision_lambda(lambda_client, function_name, alias, 0, min_concurrency=0, max_concurrency=PROVISIONED_MAX, dry_run=False)
        _wait_for(env, lambda: status() is None, 300.0, poll_s)
    return {"seconds": [s for s in seconds if s is not None], "timeouts": sum(1 for s in seconds if s is None), "serve_checks": serve_checks}


def remove_provisioned_concurrency(lambda_client, function_name: str, alias: str) -> bool:
    """Clean-up helper: make sure no provisioned-concurrency config is left (it would block `terraform destroy`)."""
    if provisioned_status(lambda_client, function_name, alias) is None:
        return True
    provision_lambda(lambda_client, function_name, alias, 0, min_concurrency=0, max_concurrency=PROVISIONED_MAX, dry_run=False)
    return provisioned_status(lambda_client, function_name, alias) is None


# ------------------------------------------------------------------------------------------- AWS read-outs


def fetch_report_messages(logs, log_group: str, since_ms: int) -> list[str]:
    """All Lambda REPORT log lines at or after `since_ms` (epoch milliseconds)."""
    messages: list[str] = []
    streams = []
    kwargs = {"logGroupName": log_group, "orderBy": "LastEventTime", "descending": True}
    while True:
        page = logs.describe_log_streams(**kwargs)
        streams += page.get("logStreams", [])
        token = page.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    for stream in streams:
        if stream.get("lastEventTimestamp", 0) < since_ms:
            continue
        token = None
        while True:
            args = {"logGroupName": log_group, "logStreamName": stream["logStreamName"], "startFromHead": True, "startTime": since_ms}
            if token:
                args["nextToken"] = token
            page = logs.get_log_events(**args)
            messages += [e["message"] for e in page.get("events", []) if e["message"].startswith("REPORT")]
            nxt = page.get("nextForwardToken")
            if not page.get("events") or nxt == token:
                break
            token = nxt
    return messages


def fetch_asg_cpu(cloudwatch, asg_name: str, start, end) -> list[dict]:
    """Best-effort CPU datapoints (average/maximum percent) for the ASG's instances; [] if CloudWatch has none."""
    try:
        out = cloudwatch.get_metric_statistics(
            Namespace="AWS/EC2", MetricName="CPUUtilization",
            Dimensions=[{"Name": "AutoScalingGroupName", "Value": asg_name}],
            StartTime=start, EndTime=end, Period=300, Statistics=["Average", "Maximum"],
        )
    except ClientError:
        return []
    return sorted(
        [{"time": p["Timestamp"].isoformat(), "average": p["Average"], "maximum": p["Maximum"]} for p in out.get("Datapoints", [])],
        key=lambda p: p["time"],
    )
