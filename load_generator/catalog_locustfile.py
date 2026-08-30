from __future__ import annotations

import os
import random

from locust import HttpUser, task

from load_generator.shapes import SeasonalLoadShape  # noqa: F401  (registers the shape with locust)

CATEGORIES = ["electronics", "home", "office"]


class CatalogUser(HttpUser):
    host = os.environ.get("CATALOG_HOST", "http://localhost:8000")

    @task(1)
    def health(self):
        self.client.get("/health")

    @task(5)
    def list_products(self):
        self.client.get("/products")

    @task(3)
    def list_products_by_category(self):
        self.client.get("/products", params={"category": random.choice(CATEGORIES)})

    @task(4)
    def get_product(self):
        self.client.get(f"/products/{random.randint(1, 7)}", name="/products/[id]")
