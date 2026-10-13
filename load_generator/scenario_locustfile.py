"""Locust glue that replays a saved Phase 7 scenario against one service (open-loop pacing).

One Locust process per service, so each keeps its own clock:

    SCENARIO_DIR=scenarios/burst-dense SCENARIO_SERVICE=thumbnail_service SCENARIO_LOG=runs/r1/thumbnail.jsonl \\
        locust -f load_generator/scenario_locustfile.py --headless --host https://<api-url>

    SCENARIO_DIR=... SCENARIO_SERVICE=catalog_service SCENARIO_LOG=runs/r1/catalog.jsonl \\
        locust -f load_generator/scenario_locustfile.py --headless --host http://<alb-dns>

Every request is appended to SCENARIO_LOG as one JSON line (see `_on_request`); the results collector reads
those files. Scenario time is real time: this module refuses to run sped-up (decision D4). Pacing logic
and its reasoning live in load_generator/scenario_player.py.

Env: SCENARIO_DIR (required), SCENARIO_SERVICE (required), SCENARIO_LOG (required),
     SCENARIO_TAIL_SECONDS (default 0: keep playing the last minute's rate for this long after the end).
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

from locust import HttpUser, LoadTestShape, constant_pacing, events, task

from load_generator.scenario_player import (
    NO_MARKER,
    RatePlan,
    RequestLog,
    parse_marker,
    plan_from_scenario,
    should_fire,
    users_needed,
)

CATALOG_PATHS = (("/health", 1), ("/products", 5), ("/products?category={cat}", 3), ("/products/{pid}", 4))
CATEGORIES = ("electronics", "home", "office")
DIMENSIONS = (64, 128, 256, 512)


class RunClock:
    """Seconds since the shape's first tick (= the start of the run). Shared by the shape and the users."""

    def __init__(self) -> None:
        self._t0: float | None = None

    def start_once(self) -> None:
        if self._t0 is None:
            self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return 0.0 if self._t0 is None else time.monotonic() - self._t0


def make_shape_class(plan: RatePlan, clock: RunClock, users: int, speedup: float = 1.0) -> type[LoadTestShape]:
    class ScenarioShape(LoadTestShape):
        def tick(self):
            clock.start_once()
            if plan.minute_at(clock.elapsed(), speedup) is None:
                return None
            return users, users  # a fixed pool, all spawned within ~1 s (each user then staggers itself, see on_start)

    return ScenarioShape


def _pick_catalog_path(rng: random.Random) -> tuple[str, str]:
    template = rng.choices([p for p, _ in CATALOG_PATHS], weights=[w for _, w in CATALOG_PATHS])[0]
    path = template.format(cat=rng.choice(CATEGORIES), pid=rng.randint(1, 7))
    name = "/products/[id]" if template == "/products/{pid}" else template.split("?")[0]
    return path, name


def make_user_class(
    service: str, plan: RatePlan, clock: RunClock, speedup: float = 1.0, seed: int | None = None
) -> type[HttpUser]:
    if service not in ("catalog_service", "thumbnail_service"):
        raise ValueError(f"unknown service {service!r}")
    master = random.Random(seed)

    class ScenarioUser(HttpUser):
        wait_time = constant_pacing(1)  # one wake-up per second, however long the last request took
        abstract = False

        def on_start(self) -> None:
            self._rng = random.Random(master.getrandbits(64))
            # Spread the users' wake-ups evenly across the second, whatever way Locust batches the spawning;
            # otherwise a whole cohort would fire at the same instant and invent concurrent cold starts.
            time.sleep(self._rng.random())

        @task
        def tick(self) -> None:
            elapsed = clock.elapsed()
            minute = plan.minute_at(elapsed, speedup)
            if minute is None:
                return
            rpm = float(plan.rpm[minute])
            if not should_fire(rpm, self.environment.runner.user_count, self._rng):
                return
            context = {"minute": minute, "offered_rpm": rpm, "service": service}
            if service == "catalog_service":
                path, name = _pick_catalog_path(self._rng)
                self._send(path, name, context)
            else:
                path = (
                    f"/thumbnail?image_id={self._rng.getrandbits(64):x}"
                    f"&width={self._rng.choice(DIMENSIONS)}&height={self._rng.choice(DIMENSIONS)}"
                )
                self._send(path, "/thumbnail", context)

        def _send(self, path: str, name: str, context: dict) -> None:
            with self.client.get(path, name=name, context=context, catch_response=True) as response:
                if response.status_code == 200:
                    response.success()
                else:
                    response.failure(f"HTTP {response.status_code}")

    ScenarioUser.__name__ = f"{service.title().replace('_', '')}ScenarioUser"
    return ScenarioUser


def make_request_listener(log: RequestLog):
    """Event hook that writes one raw JSON line per request (success or failure)."""

    def _on_request(request_type, name, response_time, response_length, response, context, exception, start_time=None, url=None, **_kw):
        context = context or {}
        status = getattr(response, "status_code", None) if response is not None else None
        marker = NO_MARKER
        if response is not None and status == 200 and context.get("service") == "thumbnail_service":
            marker = parse_marker(getattr(response, "text", None))
        log.write(
            {
                "t": start_time if start_time is not None else time.time(),
                "service": context.get("service"),
                "name": name,
                "ms": round(float(response_time), 3) if response_time is not None else None,
                "ok": exception is None,
                "status": status or None,
                "error": None if exception is None else str(exception)[:200],
                "minute": context.get("minute"),
                "offered_rpm": context.get("offered_rpm"),
                "cold": marker.cold_start,
                "container": marker.container_id,
                "init_type": marker.init_type,
            }
        )

    return _on_request


# ------------------------------------------------------------------------- module-level wiring for the CLI


def _wire_from_environment() -> None:
    from comparison.scenario import load_scenario  # imported late: only needed for a real run

    if os.environ.get("SCENARIO_SPEEDUP", "1") != "1":
        raise SystemExit("refusing to run: scenario time is never compressed in a real run (decision D4)")
    service = os.environ["SCENARIO_SERVICE"]
    scenario = load_scenario(Path(os.environ["SCENARIO_DIR"]))
    if service not in scenario.services:
        raise SystemExit(f"scenario has no service {service!r}; it has {sorted(scenario.services)}")

    plan = plan_from_scenario(scenario.services[service], float(os.environ.get("SCENARIO_TAIL_SECONDS", "0")))
    clock = RunClock()
    log = RequestLog(Path(os.environ["SCENARIO_LOG"]))
    events.request.add_listener(make_request_listener(log))
    events.quitting.add_listener(lambda **_kw: log.close())

    globals()["ScenarioUser"] = make_user_class(service, plan, clock)
    globals()["ScenarioShape"] = make_shape_class(plan, clock, users_needed(plan))
    print(
        f"scenario {scenario.fingerprint()[:12]} / {service}: {plan.minutes} min, "
        f"max {plan.max_rps * 60:.0f} rpm, {users_needed(plan)} users"
    )


if os.environ.get("SCENARIO_DIR"):
    _wire_from_environment()
