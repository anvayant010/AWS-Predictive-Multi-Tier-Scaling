import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import comparison.recheck_stages as rs
from comparison.calibration_stages import HttpResult, StageEnv
from comparison.recheck import RecheckCell
from comparison.recheck_stages import KeepAliveClient, run_connection_recheck


# --- a real local HTTP/1.1 server that counts connections ----------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/wait"):
            time.sleep(0.3)
        if self.path.startswith("/hang"):
            time.sleep(1.5)
        code = 404 if self.path.startswith("/missing") else 200
        body = json.dumps({"path": self.path}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        if self.path.startswith("/close"):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the test output quiet
        pass


class CountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.connections = 0
        self._sockets: list[socket.socket] = []

    def process_request(self, request, client_address) -> None:
        self.connections += 1
        self._sockets.append(request)
        super().process_request(request, client_address)

    def drop_all(self) -> None:
        """Close every connection from the server side while the client believes it is still open."""
        for s in self._sockets:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        self._sockets.clear()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


@pytest.fixture
def server():
    srv = CountingServer()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


# --- the keep-alive client --------------------------------------------------------------------------------------------


def test_many_sequential_requests_share_one_connection(server) -> None:
    client = KeepAliveClient()

    results = [client(f"{server.url}/products?n={i}") for i in range(30)]

    assert all(r.status == 200 for r in results)
    assert server.connections == 1 and client.connects == 1 and client.requests == 30
    client.close_all()


def test_the_path_and_query_arrive_intact_and_the_body_is_returned(server) -> None:
    client = KeepAliveClient()

    r = client(f"{server.url}/products?category=home&x=1")

    assert r.status == 200 and r.error is None and r.ms > 0
    assert json.loads(r.body) == {"path": "/products?category=home&x=1"}
    assert json.loads(client(server.url).body) == {"path": "/"}  # no path at all means "/"


def test_concurrent_threads_use_separate_connections_that_are_pooled_afterwards(server) -> None:
    client = KeepAliveClient()
    barrier = threading.Barrier(6)

    def work() -> None:
        barrier.wait()
        for _ in range(10):
            assert client(f"{server.url}/wait").status == 200

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert client.requests == 60 and client.connects == server.connections
    assert 2 <= server.connections <= 6  # as many as were in flight together, not one per request
    client.close_all()


def test_an_http_error_is_a_result_with_its_status_and_no_body(server) -> None:
    r = KeepAliveClient()(f"{server.url}/missing")

    assert r.status == 404 and r.body is None and r.error == "HTTP 404"


def test_a_pooled_connection_the_server_closed_is_retried_once_on_a_fresh_one(server) -> None:
    client = KeepAliveClient()
    assert client(f"{server.url}/a").status == 200
    server.drop_all()

    r = client(f"{server.url}/b")

    assert r.status == 200 and json.loads(r.body) == {"path": "/b"}
    assert client.stale_retries == 1 and client.connects == 2 and server.connections == 2
    client.close_all()


def test_the_retry_does_not_borrow_another_possibly_stale_pooled_connection(server) -> None:
    client = KeepAliveClient()
    threads = [threading.Thread(target=client, args=(f"{server.url}/wait",)) for _ in range(2)]  # two overlapping -> two pooled sockets
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert server.connections == 2
    server.drop_all()  # both pooled sockets are now stale

    r = client(f"{server.url}/b")

    assert r.status == 200 and client.stale_retries == 1
    client.close_all()


def test_a_failure_on_a_brand_new_connection_is_reported_not_retried_and_not_raised() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here any more once the socket closes
    client = KeepAliveClient()

    r = client(f"http://127.0.0.1:{port}/x")

    assert r.status is None and r.body is None and r.error and "Error" in r.error
    assert client.connects == 1 and client.stale_retries == 0


def test_a_timeout_is_a_failed_result_and_the_next_request_works_on_a_new_connection(server) -> None:
    client = KeepAliveClient(timeout=0.3)

    slow = client(f"{server.url}/hang")
    after = client(f"{server.url}/a")

    assert slow.status is None and slow.error and 0.25 * 1000 <= slow.ms < 1400
    assert after.status == 200 and client.connects == 2
    client.close_all()


def test_a_server_that_closes_after_the_reply_costs_a_new_connection_but_is_not_a_stale_retry(server) -> None:
    client = KeepAliveClient()

    assert client(f"{server.url}/close").status == 200
    assert client(f"{server.url}/close").status == 200

    assert client.connects == 2 and client.stale_retries == 0
    client.close_all()


def test_close_all_closes_the_pool_so_the_next_request_connects_again(server) -> None:
    client = KeepAliveClient()
    client(f"{server.url}/a")
    client.close_all()

    assert client(f"{server.url}/a").status == 200
    assert client.connects == 2
    client.close_all()


def test_https_urls_get_an_https_connection() -> None:
    import http.client

    client = KeepAliveClient()

    assert isinstance(client._take(("https", "example.invalid:443")), http.client.HTTPSConnection)
    assert not isinstance(client._take(("http", "example.invalid:80")), http.client.HTTPSConnection)


# --- the re-test stage (everything outside it is faked) -------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self._lock = threading.Lock()

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.t += seconds


class FakeAutoscaling:
    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.desired, self.requested_at, self.set_calls = clock, 1, 0.0, []

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [{"MinSize": 1, "MaxSize": 10, "DesiredCapacity": self.desired}]}

    def set_desired_capacity(self, AutoScalingGroupName, DesiredCapacity, HonorCooldown):
        self.set_calls.append(DesiredCapacity)
        self.desired, self.requested_at = DesiredCapacity, self.clock.now()


class FakeElb:
    def __init__(self, asg: FakeAutoscaling, clock: FakeClock, boot_s: float = 95.0) -> None:
        self.asg, self.clock, self.boot_s = asg, clock, boot_s

    def describe_target_health(self, TargetGroupArn):
        healthy = 1 + (1 if self.asg.desired == 2 and self.clock.now() - self.asg.requested_at >= self.boot_s else 0)
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}] * healthy}


class FakeClient:
    """Stands in for KeepAliveClient: the fake driver 'opens' connections by bumping `connects`."""

    instances: list = []

    def __init__(self) -> None:
        self.connects, self.closed = 0, False
        FakeClient.instances.append(self)

    def __call__(self, url):
        return HttpResult(200, 1.0, None)

    def close_all(self) -> None:
        self.closed = True


@pytest.fixture
def world(monkeypatch):
    clock = FakeClock()
    asg = FakeAutoscaling(clock)
    elb = FakeElb(asg, clock)
    env = StageEnv(catalog_url="http://c", thumbnail_url="http://t", http_get=lambda u: HttpResult(200, 1.0, None), clock=clock.now, wall=clock.now, sleep=clock.sleep)
    FakeClient.instances = []
    calls: list[dict] = []

    def fake_drive(drive_env, base, make_path, rpm, seconds, **kw):
        client = drive_env.http_get if isinstance(drive_env.http_get, FakeClient) else None
        if client is not None:
            client.connects += 3
        calls.append({"rpm": rpm, "seconds": seconds, "instances": asg.desired, "http_get": drive_env.http_get, "workers": kw.get("workers"), "base": base})
        slow_in_settle = [{"t": 1.0, "ms": 9000.0, "ok": True, "late_ms": 12.0}] * 4  # must not reach the table
        body = [{"t": 35.0 + i, "ms": 500.0, "ok": True, "late_ms": 7.0} for i in range(6)]
        return slow_in_settle + body

    monkeypatch.setattr(rs, "drive_constant_rate", fake_drive)
    return type("World", (), {"clock": clock, "asg": asg, "elb": elb, "env": env, "calls": calls})


def run(world, plan, **kw):
    kw.setdefault("client_factory", FakeClient)
    return run_connection_recheck(world.env, world.asg, world.elb, "asg", "tg", tuple(plan), settle_s=30.0, measure_s=60.0, **kw)


def test_each_block_runs_on_its_instance_count_in_plan_order(world) -> None:
    plan = [RecheckCell("keep_alive", 1, (100.0, 200.0)), RecheckCell("per_request", 1, (300.0,)), RecheckCell("keep_alive", 2, (400.0,))]

    out = run(world, plan)

    assert [(c["rpm"], c["instances"]) for c in world.calls] == [(100.0, 1), (200.0, 1), (300.0, 1), (400.0, 2)]
    assert [(c["mode"], c["instances"], c["offered_rpm"]) for c in out["cells"]] == [("keep_alive", 1, 100.0), ("keep_alive", 1, 200.0), ("per_request", 1, 300.0), ("keep_alive", 2, 400.0)]
    assert max(world.asg.set_calls) == 2  # never above the cap


def test_keep_alive_blocks_get_the_pooled_client_and_per_request_blocks_keep_the_plain_getter(world) -> None:
    plan = [RecheckCell("keep_alive", 1, (100.0,)), RecheckCell("per_request", 1, (100.0,))]

    run(world, plan)

    assert isinstance(world.calls[0]["http_get"], FakeClient)
    assert world.calls[1]["http_get"] is world.env.http_get and not isinstance(world.calls[1]["http_get"], FakeClient)


def test_every_keep_alive_block_gets_its_own_client_and_closes_it(world) -> None:
    plan = [RecheckCell("keep_alive", 1, (100.0,)), RecheckCell("keep_alive", 1, (200.0,))]

    run(world, plan)

    assert len(FakeClient.instances) == 2 and all(c.closed for c in FakeClient.instances)
    assert world.calls[0]["http_get"] is not world.calls[1]["http_get"]


def test_the_client_is_closed_even_if_the_load_driver_blows_up(world, monkeypatch) -> None:
    monkeypatch.setattr(rs, "drive_constant_rate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        run(world, [RecheckCell("keep_alive", 1, (100.0,))])

    assert FakeClient.instances[0].closed


def test_the_settle_period_is_dropped_from_the_table_but_kept_in_the_raw_rows(world) -> None:
    out = run(world, [RecheckCell("per_request", 1, (100.0,))])

    cell = out["cells"][0]
    assert cell["sent"] == 6 and cell["slow_share"] == 0.0 and cell["p95_ms"] == 500.0  # the 9 s settle rows would show up here
    assert len(out["raw"][0]["rows"]) == 10 and out["raw"][0]["offered_rpm"] == 100.0
    assert world.calls[0]["seconds"] == 90.0  # settle + measure


def test_connections_per_request_counts_new_connections_over_all_requests_of_the_step(world) -> None:
    out = run(world, [RecheckCell("keep_alive", 1, (100.0, 200.0)), RecheckCell("per_request", 1, (100.0,))])

    assert out["cells"][0]["connects_per_request"] == pytest.approx(3 / 10)  # 3 connections for 10 requests, not cumulative
    assert out["cells"][1]["connects_per_request"] == pytest.approx(3 / 10)
    assert out["cells"][2]["connects_per_request"] == 1.0  # a new connection for every request, by construction


def test_the_longest_delay_before_a_request_is_recorded(world) -> None:
    assert run(world, [RecheckCell("per_request", 1, (100.0,))])["cells"][0]["max_late_ms"] == 12.0


def test_a_block_whose_instances_never_become_healthy_is_skipped_and_the_plan_goes_on(world) -> None:
    world.elb.boot_s = 10_000.0
    plan = [RecheckCell("keep_alive", 2, (100.0,)), RecheckCell("per_request", 1, (100.0,))]

    out = run(world, plan, timeout_s=120.0)

    assert out["skipped"] == [{"mode": "keep_alive", "instances": 2, "reason": "instances did not become healthy in time"}]
    assert [(c["mode"], c["instances"]) for c in out["cells"]] == [("per_request", 1)]
    assert FakeClient.instances == []  # no client is made for a block that never ran


def test_the_actuator_call_itself_is_capped_at_two_instances(world) -> None:
    from decision_engine.actuation import ActuationRefused

    with pytest.raises(ActuationRefused):
        rs._ensure_instances(world.env, world.asg, world.elb, "asg", "tg", 3, 60.0, 5.0)  # a plan can never ask for this; the cap is a 2nd line of defence

    assert world.asg.set_calls == []


def test_a_stop_before_the_start_drives_no_load(world) -> None:
    world.env.stop.set()

    out = run(world, [RecheckCell("keep_alive", 1, (100.0,))])

    assert out == {"cells": [], "raw": [], "skipped": []} and world.calls == []


def test_a_stop_during_the_plan_ends_it_without_starting_the_next_step(world, monkeypatch) -> None:
    real = rs.drive_constant_rate

    def stopping(drive_env, *a, **k):
        rows = real(drive_env, *a, **k)
        world.env.stop.set()
        return rows

    monkeypatch.setattr(rs, "drive_constant_rate", stopping)

    out = run(world, [RecheckCell("keep_alive", 1, (100.0, 200.0)), RecheckCell("per_request", 1, (100.0,))])

    assert len(out["cells"]) == 1 and FakeClient.instances[0].closed


def test_the_load_is_driven_against_the_catalog_with_many_workers(world) -> None:
    run(world, [RecheckCell("per_request", 1, (100.0,))])

    assert world.calls[0]["base"] == "http://c" and world.calls[0]["workers"] >= 256  # 8000 rpm needs ~110 requests in flight


def test_each_step_uses_a_different_random_seed(world, monkeypatch) -> None:
    seeds = []
    real = rs.drive_constant_rate
    monkeypatch.setattr(rs, "drive_constant_rate", lambda *a, **k: (seeds.append(k["seed"]), real(*a, **k))[1])

    run(world, [RecheckCell("per_request", 1, (100.0, 200.0, 300.0))])

    assert len(set(seeds)) == 3
