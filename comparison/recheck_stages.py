"""The measuring part of the connection re-test (see comparison/recheck.py for why and how it is read).

`KeepAliveClient` is an HTTP GET over a shared pool of persistent connections, i.e. what Locust (and any real
browser or service client) does, as opposed to calibration_stages.urllib_get which opens a new connection for
every request. `run_connection_recheck` steps the catalog load through a plan of (connection mode, instance
count, rates) blocks and records one table row per step plus the raw requests.

Capacity changes go through the same capped actuator as the first calibration (never more than 2 instances),
and, like it, they do not go through the scaling loop's kill switch.
"""

from __future__ import annotations

import http.client
import threading
import time
import urllib.parse
from dataclasses import replace
from typing import Callable

from comparison.calibration_stages import (
    BOOT_MAX_INSTANCES,
    HttpResult,
    StageEnv,
    _healthy_targets,
    _wait_for,
    catalog_path,
    drive_constant_rate,
)
from comparison.recheck import RecheckCell, cell_record
from decision_engine.actuator_ec2 import scale_ec2

_STALE_ERRORS = (http.client.RemoteDisconnected, ConnectionError)  # a pooled socket the server closed while it sat idle


class KeepAliveClient:
    """GET over a shared pool of persistent connections. Callable like `urllib_get`: `client(url) -> HttpResult`.

    A connection is taken from the pool (or made), used for one request and put back, so the number of open
    connections follows the peak concurrency, as with a real client. If a pooled connection turns out to have
    been closed by the other side while idle, the request is retried ONCE on a fresh connection (the usual
    keep-alive behaviour); anything else is returned as a failed result, never raised.
    """

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._idle: dict[tuple[str, str], list[http.client.HTTPConnection]] = {}
        self._lock = threading.Lock()
        self.requests = 0
        self.connects = 0  # connections actually opened (a request on a reused socket does not count)
        self.stale_retries = 0

    def _take(self, key: tuple[str, str], *, fresh: bool = False) -> http.client.HTTPConnection:
        if not fresh:
            with self._lock:
                pool = self._idle.setdefault(key, [])
                if pool:
                    return pool.pop()
        cls = http.client.HTTPSConnection if key[0] == "https" else http.client.HTTPConnection
        return cls(key[1], timeout=self.timeout)

    def _give_back(self, key: tuple[str, str], conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._idle.setdefault(key, []).append(conn)

    def __call__(self, url: str) -> HttpResult:
        start = time.perf_counter()
        parts = urllib.parse.urlsplit(url)
        key = (parts.scheme, parts.netloc)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        with self._lock:
            self.requests += 1
        for attempt in (0, 1):
            conn = self._take(key, fresh=attempt == 1)  # the retry never trusts another pooled (possibly stale) socket
            reused = conn.sock is not None
            if not reused:
                with self._lock:
                    self.connects += 1
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                body = response.read().decode("utf-8", "replace")
                ms = (time.perf_counter() - start) * 1000
                self._give_back(key, conn)  # http.client has already closed it itself if the server asked for that
                if response.status >= 400:
                    return HttpResult(response.status, ms, None, f"HTTP {response.status}")
                return HttpResult(response.status, ms, body)
            except _STALE_ERRORS as exc:
                conn.close()
                if reused and attempt == 0:
                    with self._lock:
                        self.stale_retries += 1
                    continue
                return HttpResult(None, (time.perf_counter() - start) * 1000, None, f"{type(exc).__name__}: {exc}"[:200])
            except Exception as exc:  # noqa: BLE001  (timeouts, DNS, TLS: recorded as a failed request, connection dropped)
                conn.close()
                return HttpResult(None, (time.perf_counter() - start) * 1000, None, f"{type(exc).__name__}: {exc}"[:200])
        raise AssertionError("unreachable")  # pragma: no cover

    def close_all(self) -> None:
        with self._lock:
            pools, self._idle = list(self._idle.values()), {}
        for pool in pools:
            for conn in pool:
                conn.close()


def _ensure_instances(env: StageEnv, autoscaling, elbv2, asg_name: str, target_group_arn: str, n: int, timeout_s: float, poll_s: float) -> bool:
    scale_ec2(autoscaling, asg_name, n, min_capacity=1, max_capacity=BOOT_MAX_INSTANCES, dry_run=False)
    return _wait_for(env, lambda: _healthy_targets(elbv2, target_group_arn) == n, timeout_s, poll_s)


def run_connection_recheck(
    env: StageEnv, autoscaling, elbv2, asg_name: str, target_group_arn: str, plan: tuple[RecheckCell, ...], *,
    settle_s: float, measure_s: float, workers: int = 256, timeout_s: float = 600.0, poll_s: float = 5.0,
    client_factory: Callable[[], KeepAliveClient] = KeepAliveClient,
) -> dict:
    """Run every block of `plan`; returns {"cells": [table rows], "raw": [{mode, instances, offered_rpm, rows}], "skipped": [...]}.

    A block whose instance count cannot be reached in time is recorded as skipped and the plan goes on.
    The settle period of every step is dropped from its table row, as in the first calibration.
    """
    cells: list[dict] = []
    raw: list[dict] = []
    skipped: list[dict] = []
    seed = 500
    for block in plan:
        if env.stop.is_set():
            break
        if not _ensure_instances(env, autoscaling, elbv2, asg_name, target_group_arn, block.instances, timeout_s, poll_s):
            skipped.append({"mode": block.mode, "instances": block.instances, "reason": "instances did not become healthy in time"})
            continue
        client = client_factory() if block.mode == "keep_alive" else None
        block_env = replace(env, http_get=client) if client is not None else env
        try:
            for rate in block.rates:
                if env.stop.is_set():
                    break
                seed += 1
                connects_before = client.connects if client else 0
                rows = drive_constant_rate(block_env, env.catalog_url, catalog_path, rate, settle_s + measure_s, workers=workers, seed=seed)
                measured = [r for r in rows if r["t"] >= settle_s]
                per_request = ((client.connects - connects_before) / len(rows)) if (client and rows) else (1.0 if rows else None)
                record = cell_record(block.mode, block.instances, measured, rate, measure_s, connects_per_request=per_request)
                record["max_late_ms"] = max((r["late_ms"] for r in rows), default=0.0)
                cells.append(record)
                raw.append({"mode": block.mode, "instances": block.instances, "offered_rpm": rate, "rows": rows})
        finally:
            if client is not None:
                client.close_all()
    return {"cells": cells, "raw": raw, "skipped": skipped}
