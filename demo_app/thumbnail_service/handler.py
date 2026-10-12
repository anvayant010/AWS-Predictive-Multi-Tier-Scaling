from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# Stand-in for a real image-resize step (e.g. Pillow) so this Lambda has a
# realistic, cold-start-sensitive execution profile without needing image
# assets: a fixed I/O-wait component plus CPU work that scales with the
# requested output size.
_SIMULATED_FETCH_LATENCY_SECONDS = 0.05
_MAX_HASH_ITERATIONS = 5000

# Cold-start marker (Phase 7). Module-level code runs once per Lambda container, so these values
# identify the container: a new id means a new container, and the first request a container serves
# is the one that paid for its start-up. Provisioned-concurrency containers are initialised ahead of
# time, before any user is waiting, so their first request is NOT reported as cold.
_PROVISIONED_INIT_TYPE = "provisioned-concurrency"
_CONTAINER_ID = uuid.uuid4().hex
_container_has_served = False


def _claim_cold_start() -> bool:
    """True only for the first request of an on-demand container; marks the container as used."""
    global _container_has_served
    first = not _container_has_served
    _container_has_served = True
    return first and _init_type() != _PROVISIONED_INIT_TYPE


def _init_type() -> str:
    return os.environ.get("AWS_LAMBDA_INITIALIZATION_TYPE", "on-demand")


def generate_thumbnail(image_id: str, width: int, height: int) -> dict[str, Any]:
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")

    start = time.monotonic()
    time.sleep(_SIMULATED_FETCH_LATENCY_SECONDS)

    digest = image_id.encode()
    iterations = min((width * height) // 500, _MAX_HASH_ITERATIONS)
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()

    elapsed_ms = (time.monotonic() - start) * 1000
    return {
        "image_id": image_id,
        "width": width,
        "height": height,
        "thumbnail_hash": digest.hex(),
        "processing_time_ms": round(elapsed_ms, 2),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    marker = {"cold_start": _claim_cold_start(), "container_id": _CONTAINER_ID, "init_type": _init_type()}
    params = event.get("queryStringParameters") or {}
    image_id = params.get("image_id", "unknown")

    try:
        width = int(params.get("width", 128))
        height = int(params.get("height", 128))
        result = generate_thumbnail(image_id, width, height)
    except ValueError as exc:
        return _json_response(400, {"error": str(exc), **marker})

    return _json_response(200, {**result, **marker})


def _json_response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
