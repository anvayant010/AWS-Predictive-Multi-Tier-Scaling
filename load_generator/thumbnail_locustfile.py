from __future__ import annotations

import random
import uuid

from locust import HttpUser, task

from load_generator.shapes import BurstyLoadShape  # noqa: F401  (registers the shape with locust)

DIMENSIONS = [64, 128, 256, 512]


class ThumbnailUser(HttpUser):
    @task
    def request_thumbnail(self):
        self.client.get(
            "/thumbnail",
            params={
                "image_id": str(uuid.uuid4()),
                "width": random.choice(DIMENSIONS),
                "height": random.choice(DIMENSIONS),
            },
            name="/thumbnail",
        )
