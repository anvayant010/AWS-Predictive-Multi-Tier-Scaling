"""Test helper (NOT a test): replays a tiny scenario with real Locust against a local fake server.

Run in a subprocess by tests/test_scenario_player.py, because importing locust monkey-patches the whole
process, which must not leak into the rest of the test session.

usage: python tests/locust_replay_driver.py '<json config>'
config: {"service", "rpm": [...], "speedup", "log", "seed"}
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import locust  # noqa: F401  (must be imported before anything that opens sockets)
import gevent
from locust.env import Environment

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from load_generator.scenario_locustfile import (  # noqa: E402
    RunClock,
    make_request_listener,
    make_shape_class,
    make_user_class,
)
from load_generator.scenario_player import RatePlan, RequestLog, users_needed  # noqa: E402

config = json.loads(sys.argv[1])
served = {"count": 0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        served["count"] += 1
        status = 500 if "fail" in config.get("mode", "") and served["count"] % 2 == 0 else 200
        body = json.dumps({"cold_start": served["count"] == 1, "container_id": "c-1", "init_type": "on-demand"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # clients disconnecting at shutdown is expected
        pass


server = QuietServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
host = f"http://127.0.0.1:{server.server_address[1]}"

plan = RatePlan(rpm=config["rpm"], tail_seconds=0.0)
speedup = float(config["speedup"])
clock = RunClock()
log = RequestLog(Path(config["log"]))
user_class = make_user_class(config["service"], plan, clock, speedup=speedup, seed=config.get("seed", 1))
shape_class = make_shape_class(plan, clock, users_needed(plan), speedup=speedup)

env = Environment(user_classes=[user_class], shape_class=shape_class(), host=host)
env.events.request.add_listener(make_request_listener(log))
runner = env.create_local_runner()
runner.start_shape()
gevent.sleep(plan.duration_seconds(speedup) + 2.0)
runner.quit()
log.close()
print(json.dumps({"served": served["count"]}), flush=True)
# Leave immediately: the fake server's greenlets and Locust's workers must not keep the process alive.
os._exit(0)
