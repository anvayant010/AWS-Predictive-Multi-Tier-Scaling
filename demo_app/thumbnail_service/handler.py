from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

# Stand-in for a real image-resize step (e.g. Pillow) so this Lambda has a
# realistic, cold-start-sensitive execution profile without needing image
# assets: a fixed I/O-wait component plus CPU work that scales with the
# requested output size.
_SIMULATED_FETCH_LATENCY_SECONDS = 0.05
_MAX_HASH_ITERATIONS = 5000


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
    params = event.get("queryStringParameters") or {}
    image_id = params.get("image_id", "unknown")

    try:
        width = int(params.get("width", 128))
        height = int(params.get("height", 128))
        result = generate_thumbnail(image_id, width, height)
    except ValueError as exc:
        return _json_response(400, {"error": str(exc)})

    return _json_response(200, result)


def _json_response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
