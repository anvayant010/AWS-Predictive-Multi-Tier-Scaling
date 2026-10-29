import json
import random
import threading
import time
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

import comparison.calibration_stages as cs
from comparison.calibration import find_knee, idle_lifetime
from comparison.calibration_stages import (
    HttpResult,
    StageEnv,
    catalog_path,
    drive_constant_rate,
    fetch_asg_cpu,
    fetch_report_messages,
    parallel_burst,
    remove_provisioned_concurrency,
    run_boot_timing,
    run_cold_start_probe,
    run_idle_staircase,
    run_knee,
    run_provisioned_timing,
    thumbnail_path,
)


# --- fakes ---------------------------------------------------------------------------------------------------


class FakeClock:
    """Time that only moves when something sleeps, so 'hours' pass instantly and deterministically."""

    def __init__(self) -> None:
        self.t = 0.0
        self._lock = threading.Lock()

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.t += seconds


class FakeLambdaService:
    """Containers with a real idle lifetime on the fake clock; concurrent requests need separate containers."""

    def __init__(self, clock: FakeClock, lifetime_s: float = 720.0, provisioned=lambda: False) -> None:
        self.clock, self.lifetime_s, self.provisioned = clock, lifetime_s, provisioned
        self.idle: dict[str, float] = {}  # container id -> last used
        self.busy: set[str] = set()
        self.created = 0
        self.lock = threading.Lock()

    def get(self, url: str) -> HttpResult:
        with self.lock:
            now = self.clock.now()
            self.idle = {c: t for c, t in self.idle.items() if now - t <= self.lifetime_s}
            if self.idle:
                cid = next(iter(self.idle))
                del self.idle[cid]
                cold = False
            else:
                self.created += 1
                cid, cold = f"c{self.created}", True
            self.busy.add(cid)
        time.sleep(0.03)  # real time: keeps simultaneous requests overlapping
        with self.lock:
            self.busy.discard(cid)
            self.idle[cid] = self.clock.now()
        init = "provisioned-concurrency" if self.provisioned() else "on-demand"
        body = json.dumps({"cold_start": cold and init == "on-demand", "container_id": cid, "init_type": init})
        return HttpResult(200, 40.0 + (700.0 if cold else 0.0), body)


def env_with(clock: FakeClock | None = None, http_get=None, **kw) -> StageEnv:
    clock = clock or FakeClock()
    return StageEnv(
        catalog_url="http://catalog", thumbnail_url="http://thumb",
        http_get=http_get or (lambda url: HttpResult(200, 10.0, "{}")),
        clock=clock.now, wall=clock.now, sleep=clock.sleep, **kw,
    )


# --- open-loop load ---------------------------------------------------------------------------------------------


def real_env(http_get) -> StageEnv:
    return StageEnv(catalog_url="http://c", thumbnail_url="http://t", http_get=http_get)


def test_the_driver_sends_about_the_requested_rate_and_records_each_request() -> None:
    seen = []

    def get(url):
        seen.append(url)
        return HttpResult(200, 5.0, None)

    rows = drive_constant_rate(real_env(get), "http://c", catalog_path, rpm=1200.0, seconds=2.0, seed=3)  # 20 rps for 2 s

    assert 20 <= len(rows) <= 65 and len(rows) == len(seen)
    assert all(r["ok"] and r["status"] == 200 and 0 <= r["t"] < 2.0 for r in rows)
    assert all(u.startswith("http://c/") for u in seen)


def test_a_slow_server_does_not_slow_the_offered_load_down() -> None:
    def slow(url):
        time.sleep(0.5)  # every reply takes half a second
        return HttpResult(200, 500.0, None)

    rows = drive_constant_rate(real_env(slow), "http://c", catalog_path, rpm=1800.0, seconds=2.0, workers=64, seed=4)

    assert len(rows) >= 40  # a closed loop with 64 users would also manage this; the point is arrivals are not delayed
    assert max(r["late_ms"] for r in rows) < 400  # requests started when scheduled, not after the previous reply


def test_failures_are_recorded_as_not_ok() -> None:
    rows = drive_constant_rate(real_env(lambda u: HttpResult(503, 3.0, None, "HTTP 503")), "http://c", catalog_path, 1200.0, 1.0, seed=5)

    assert rows and not any(r["ok"] for r in rows) and {r["status"] for r in rows} == {503}


def test_a_stop_request_ends_the_driver_early() -> None:
    env = real_env(lambda u: HttpResult(200, 1.0, None))
    threading.Timer(0.3, env.stop.set).start()
    started = time.monotonic()

    drive_constant_rate(env, "http://c", catalog_path, rpm=600.0, seconds=30.0)

    assert time.monotonic() - started < 5.0


def test_a_zero_rate_sends_nothing() -> None:
    env = env_with()
    assert drive_constant_rate(env, "http://c", catalog_path, rpm=0, seconds=5.0) == []


@pytest.mark.parametrize("rpm,seconds", [(-1, 5), (10, 0)])
def test_nonsense_driver_arguments_are_refused(rpm, seconds) -> None:
    with pytest.raises(ValueError):
        drive_constant_rate(env_with(), "http://c", catalog_path, rpm, seconds)


def test_paths_are_valid_for_both_services() -> None:
    rng = random.Random(1)
    assert all(catalog_path(rng).startswith("/") for _ in range(50))
    assert thumbnail_path(rng).startswith("/thumbnail?image_id=")


def test_a_parallel_burst_needs_one_container_per_request() -> None:
    clock = FakeClock()
    service = FakeLambdaService(clock)

    rows = parallel_burst(env_with(clock, service.get), "http://t", thumbnail_path, k=6)

    assert len(rows) == 6 and {r["container"] for r in rows} == {f"c{i}" for i in range(1, 7)}
    assert all(r["cold"] is True for r in rows)


# --- the knee stage ------------------------------------------------------------------------------------------------


def test_the_knee_stage_climbs_until_two_steps_in_a_row_fail_and_drops_the_settle_period(monkeypatch) -> None:
    calls = []

    def fake_drive(env, base, make, rpm, seconds, **kw):
        calls.append(rpm)
        slow = rpm > 1000
        n = int(rpm * seconds / 60)
        settle = [{"t": 1.0, "ms": 9999.0, "ok": True, "late_ms": 0.0}] * int(n * 0.3)  # would wreck P95 if counted
        body = [{"t": 31.0 + i * 0.01, "ms": 500.0 if slow else 100.0, "ok": True, "late_ms": 0.0} for i in range(n)]
        return settle + body

    monkeypatch.setattr(cs, "drive_constant_rate", fake_drive)

    out = run_knee(env_with(), [200, 400, 800, 1600, 2400, 3200, 4800], settle_s=30, measure_s=60)

    assert calls == [200, 400, 800, 1600, 2400]  # stopped after 1600 and 2400 both failed
    knee = find_knee(out["steps"])
    assert knee["knee_rpm"] == 800 and knee["unloaded_p95_ms"] == 100.0
    assert len(out["windows"]) == 5 and len(out["raw"]) == 5


def test_the_knee_stage_gives_up_if_the_baseline_step_gets_no_successes(monkeypatch) -> None:
    monkeypatch.setattr(cs, "drive_constant_rate", lambda *a, **k: [{"t": 40.0, "ms": 1.0, "ok": False, "late_ms": 0.0}] * 10)

    out = run_knee(env_with(), [200, 400], settle_s=30, measure_s=60)

    assert len(out["steps"]) == 1


def test_the_knee_stage_stops_when_asked(monkeypatch) -> None:
    env = env_with()
    env.stop.set()
    monkeypatch.setattr(cs, "drive_constant_rate", lambda *a, **k: pytest.fail("must not drive load after a stop"))

    assert run_knee(env, [200, 400], settle_s=1, measure_s=1)["steps"] == []


# --- cold starts and the idle staircase ------------------------------------------------------------------------------


def test_the_cold_start_probe_sees_cold_then_warm_containers() -> None:
    clock = FakeClock()
    service = FakeLambdaService(clock)

    rows = run_cold_start_probe(env_with(clock, service.get), parallel=4, warm_calls=10, warm_spacing_s=0.1)

    assert sum(1 for r in rows[:4] if r["cold"]) == 4  # the first burst: every container is new
    assert all(r["cold"] is False for r in rows[4:14])  # sequential calls reuse a warm container
    assert len(rows) == 4 + 10 + 4


def test_the_staircase_finds_the_gap_at_which_containers_expire() -> None:
    clock = FakeClock()
    service = FakeLambdaService(clock, lifetime_s=12 * 60)  # containers die after 12 minutes idle

    out = run_idle_staircase(env_with(clock, service.get), [5, 10, 15, 20], parallel=4)

    survival = {p.gap_minutes: p.survival for p in out["probes"]}
    assert survival == {5: 1.0, 10: 1.0, 15: 0.0, 20: 0.0}
    result = idle_lifetime(out["probes"])
    assert result["lifetime_lower_bound_minutes"] == 10 and result["recommended_gap_minutes"] == 15


def test_the_staircase_reports_a_lifetime_longer_than_it_tested() -> None:
    clock = FakeClock()
    service = FakeLambdaService(clock, lifetime_s=10_000 * 60)

    out = run_idle_staircase(env_with(clock, service.get), [3, 6], parallel=3)

    assert idle_lifetime(out["probes"])["exceeds_longest_tested"]


def test_the_staircase_gaps_are_really_waited_in_the_gaps_order() -> None:
    clock = FakeClock()
    service = FakeLambdaService(clock)

    run_idle_staircase(env_with(clock, service.get), [10, 3], parallel=2)

    assert clock.now() >= (3 + 10) * 60


def test_a_stop_during_a_gap_ends_the_staircase_without_a_bogus_probe() -> None:
    clock = FakeClock()
    service = FakeLambdaService(clock)
    env = env_with(clock, service.get)
    original_sleep = clock.sleep

    def stopping_sleep(s):
        original_sleep(s)
        if clock.now() > 100:
            env.stop.set()

    env.sleep = stopping_sleep

    out = run_idle_staircase(env, [5, 10], parallel=2)

    assert out["probes"] == []


# --- boot and provisioned-concurrency timings ------------------------------------------------------------------------------


class FakeAutoscaling:
    def __init__(self, clock: FakeClock, min_size=1, max_size=10) -> None:
        self.clock, self.min_size, self.max_size = clock, min_size, max_size
        self.desired = 1
        self.requested_at = 0.0
        self.set_calls: list[int] = []

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [{"MinSize": self.min_size, "MaxSize": self.max_size, "DesiredCapacity": self.desired}]}

    def set_desired_capacity(self, AutoScalingGroupName, DesiredCapacity, HonorCooldown):
        self.set_calls.append(DesiredCapacity)
        self.desired, self.requested_at = DesiredCapacity, self.clock.now()


class FakeElb:
    """One instance always healthy; the second becomes healthy `boot_s` after it is requested and goes at once on scale-in."""

    def __init__(self, asg: FakeAutoscaling, clock: FakeClock, boot_s: float = 95.0) -> None:
        self.asg, self.clock, self.boot_s = asg, clock, boot_s

    def describe_target_health(self, TargetGroupArn):
        healthy = 1 + (1 if self.asg.desired == 2 and self.clock.now() - self.asg.requested_at >= self.boot_s else 0)
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}] * healthy}


def test_boot_timing_measures_request_to_healthy_and_returns_to_one_instance() -> None:
    clock = FakeClock()
    asg = FakeAutoscaling(clock)
    env = env_with(clock)

    out = run_boot_timing(env, asg, FakeElb(asg, clock, boot_s=95.0), "asg", "tg-arn", repeats=3, poll_s=5.0)

    assert len(out["seconds"]) == 3 and out["timeouts"] == 0
    assert all(95.0 <= s <= 101.0 for s in out["seconds"])
    assert asg.set_calls == [2, 1, 2, 1, 2, 1] and asg.desired == 1


def test_boot_timing_records_a_timeout_instead_of_waiting_forever() -> None:
    clock = FakeClock()
    asg = FakeAutoscaling(clock)

    out = run_boot_timing(env_with(clock), asg, FakeElb(asg, clock, boot_s=10_000.0), "asg", "tg", repeats=1, timeout_s=120.0)

    assert out["seconds"] == [] and out["timeouts"] == 1 and asg.desired == 1


def test_boot_timing_never_asks_for_more_than_two_instances() -> None:
    clock = FakeClock()
    asg = FakeAutoscaling(clock)

    run_boot_timing(env_with(clock), asg, FakeElb(asg, clock), "asg", "tg", repeats=2)

    assert max(asg.set_calls) == 2


class FakeLambdaClient:
    def __init__(self, clock: FakeClock, ready_after_s: float = 165.0, fail: bool = False) -> None:
        self.clock, self.ready_after_s, self.fail = clock, ready_after_s, fail
        self.config = None
        self.requested_at = 0.0
        self.calls: list[tuple[str, int | None]] = []

    def get_alias(self, FunctionName, Name):
        return {"Name": Name}

    def put_provisioned_concurrency_config(self, FunctionName, Qualifier, ProvisionedConcurrentExecutions):
        self.calls.append(("put", ProvisionedConcurrentExecutions))
        self.config, self.requested_at = ProvisionedConcurrentExecutions, self.clock.now()

    def get_provisioned_concurrency_config(self, FunctionName, Qualifier):
        if self.config is None:
            raise ClientError({"Error": {"Code": "ProvisionedConcurrencyConfigNotFoundException", "Message": "none"}}, "Get")
        ready = self.clock.now() - self.requested_at >= self.ready_after_s
        status = "FAILED" if (self.fail and ready) else ("READY" if ready else "IN_PROGRESS")
        return {"RequestedProvisionedConcurrentExecutions": self.config, "Status": status,
                "AvailableProvisionedConcurrentExecutions": self.config if status == "READY" else 0,
                "AllocatedProvisionedConcurrentExecutions": self.config if status == "READY" else 0}

    def delete_provisioned_concurrency_config(self, FunctionName, Qualifier):
        self.calls.append(("delete", None))
        self.config = None


def test_provisioned_timing_measures_request_to_ready_and_confirms_it_serves_requests() -> None:
    clock = FakeClock()
    lam = FakeLambdaClient(clock, ready_after_s=165.0)
    service = FakeLambdaService(clock, provisioned=lambda: lam.config is not None)

    out = run_provisioned_timing(env_with(clock, service.get), lam, "fn", "live", repeats=2, poll_s=5.0)

    assert len(out["seconds"]) == 2 and all(165.0 <= s <= 171.0 for s in out["seconds"])
    assert [c["init_type"] for c in out["serve_checks"]] == ["provisioned-concurrency"] * 2
    assert all(c["cold"] is False for c in out["serve_checks"])  # provisioned containers never report a cold start
    assert lam.config is None  # cleaned up after every repeat


def test_provisioned_timing_never_asks_for_more_than_one_unit() -> None:
    clock = FakeClock()
    lam = FakeLambdaClient(clock)

    run_provisioned_timing(env_with(clock, FakeLambdaService(clock).get), lam, "fn", "live", repeats=2)

    assert {n for op, n in lam.calls if op == "put"} == {1}


def test_a_failed_provisioning_is_counted_not_timed() -> None:
    clock = FakeClock()
    lam = FakeLambdaClient(clock, fail=True)

    out = run_provisioned_timing(env_with(clock, FakeLambdaService(clock).get), lam, "fn", "live", repeats=1, timeout_s=300.0)

    assert out["seconds"] == [] and out["timeouts"] == 1 and lam.config is None


def test_cleanup_removes_a_leftover_configuration_and_is_a_noop_otherwise() -> None:
    clock = FakeClock()
    lam = FakeLambdaClient(clock)

    assert remove_provisioned_concurrency(lam, "fn", "live") and lam.calls == []
    lam.put_provisioned_concurrency_config("fn", "live", 1)
    assert remove_provisioned_concurrency(lam, "fn", "live") and lam.config is None


# --- read-outs ---------------------------------------------------------------------------------------------------


class FakeLogs:
    def __init__(self, streams: dict[str, list[tuple[int, str]]]) -> None:
        self.streams = streams

    def describe_log_streams(self, logGroupName, orderBy, descending, nextToken=None):
        items = [{"logStreamName": n, "lastEventTimestamp": max(t for t, _ in ev)} for n, ev in self.streams.items()]
        return {"logStreams": items[:1], "nextToken": "page2"} if not nextToken else {"logStreams": items[1:]}

    def get_log_events(self, logGroupName, logStreamName, startFromHead, startTime, nextToken=None):
        if nextToken:
            return {"events": [], "nextForwardToken": nextToken}
        events = [{"message": m, "timestamp": t} for t, m in self.streams[logStreamName] if t >= startTime]
        return {"events": events, "nextForwardToken": "tok"}


def test_only_report_lines_after_the_start_time_are_collected_across_streams() -> None:
    logs = FakeLogs({
        "s1": [(100, "START RequestId: a"), (150, "REPORT RequestId: a Duration: 5 ms"), (900, "REPORT RequestId: b Duration: 6 ms")],
        "s2": [(1000, "REPORT RequestId: c Duration: 7 ms"), (1001, "END RequestId: c")],
        "old": [(10, "REPORT RequestId: z Duration: 1 ms")],
    })

    got = fetch_report_messages(logs, "/aws/lambda/x", since_ms=500)

    assert got == ["REPORT RequestId: b Duration: 6 ms", "REPORT RequestId: c Duration: 7 ms"]


def test_cpu_datapoints_are_returned_sorted_and_a_cloudwatch_error_means_none() -> None:
    class CW:
        def get_metric_statistics(self, **kw):
            return {"Datapoints": [
                {"Timestamp": datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc), "Average": 40.0, "Maximum": 70.0},
                {"Timestamp": datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc), "Average": 20.0, "Maximum": 30.0},
            ]}

    class Broken:
        def get_metric_statistics(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Get")

    points = fetch_asg_cpu(CW(), "asg", None, None)

    assert [p["maximum"] for p in points] == [30.0, 70.0]
    assert fetch_asg_cpu(Broken(), "asg", None, None) == []


def test_the_default_http_getter_turns_errors_into_failed_results_not_exceptions() -> None:
    result = cs.urllib_get("http://127.0.0.1:1/never", timeout=1.0)

    assert result.status is None and result.error and result.body is None


def test_waiting_wakes_early_when_stopped() -> None:
    clock = FakeClock()
    env = env_with(clock)
    env.stop.set()

    assert env.wait(3600.0) is False and clock.now() == 0.0
    assert env_with(clock).wait(5.0) is True


def test_calibration_never_takes_capacity_above_its_own_small_caps() -> None:
    assert cs.BOOT_MAX_INSTANCES == 2 and cs.PROVISIONED_MAX == 1


def test_boot_timing_is_refused_by_the_actuator_if_it_ever_asked_for_more_than_its_cap(monkeypatch) -> None:
    from decision_engine.actuation import ActuationRefused

    clock = FakeClock()
    asg = FakeAutoscaling(clock)
    monkeypatch.setattr(cs, "BOOT_MAX_INSTANCES", 1)  # a cap below what the stage requests

    with pytest.raises(ActuationRefused):
        run_boot_timing(env_with(clock), asg, FakeElb(asg, clock), "asg", "tg", repeats=1)
    assert asg.set_calls == []


def test_each_staircase_step_starts_from_the_containers_that_answered_the_step_before() -> None:
    clock = FakeClock()
    service = FakeLambdaService(clock, lifetime_s=7 * 60)  # containers are replaced at the 10-minute step

    out = run_idle_staircase(env_with(clock, service.get), [5, 10, 15], parallel=3)

    probes = out["probes"]
    assert probes[1].survival == 0.0 and probes[0].seen_after != probes[1].seen_after
    assert probes[1].known_before == probes[0].seen_after and probes[2].known_before == probes[1].seen_after


def test_a_parallel_burst_releases_all_its_requests_through_a_barrier(monkeypatch) -> None:
    waits = []

    class CountingBarrier(threading.Barrier):
        def wait(self, timeout=None):
            waits.append(1)
            return super().wait(timeout)

    monkeypatch.setattr(cs.threading, "Barrier", CountingBarrier)

    parallel_burst(env_with(http_get=lambda u: HttpResult(200, 1.0, None)), "http://t", thumbnail_path, k=5)

    assert len(waits) == 5


def test_log_streams_that_ended_before_the_start_time_are_not_even_opened() -> None:
    opened = []

    class Logs(FakeLogs):
        def get_log_events(self, logGroupName, logStreamName, **kw):
            opened.append(logStreamName)
            return super().get_log_events(logGroupName, logStreamName, **kw)

    logs = Logs({"new": [(900, "REPORT RequestId: b Duration: 6 ms")], "old": [(10, "REPORT RequestId: z Duration: 1 ms")]})

    fetch_report_messages(logs, "/aws/lambda/x", since_ms=500)

    assert set(opened) == {"new"}
