# Predictive-Multi-Tier-Scaling
An ML-driven system that forecasts traffic and pre-scales both EC2 Auto Scaling Groups and Lambda Provisioned Concurrency ahead of demand.

## Load generation

```bash
# Seasonal load against catalog_service (run it locally first: uvicorn demo_app.catalog_service.main:app --port 8000)
locust -f load_generator/catalog_locustfile.py --headless --host http://localhost:8000

# Bursty load against thumbnail_service (needs live infra — no local server for this one)
locust -f load_generator/thumbnail_locustfile.py --headless --host <thumbnail_api_invoke_url>
```

Both shapes run for 30 minutes by default; tune via env vars in `load_generator/shapes.py` (e.g. `SEASONAL_PERIOD_SECONDS`, `BURST_PEAK_USERS_MAX`).
