-- Phase 3 metrics store. One narrow hypertable for both services' metrics,
-- since catalog_service and thumbnail_service have disjoint metric sets --
-- see docs/design.md Phase 3 for the schema rationale.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS metrics (
    time        TIMESTAMPTZ      NOT NULL,
    service     TEXT             NOT NULL,   -- 'catalog_service' | 'thumbnail_service'
    metric_name TEXT             NOT NULL,   -- e.g. 'RequestCount', 'Invocations'
    value       DOUBLE PRECISION NOT NULL,
    source      TEXT             NOT NULL,   -- 'cloudwatch' | 'synthetic'
    PRIMARY KEY (time, service, metric_name, source)
);

SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_metrics_service_metric_time
    ON metrics (service, metric_name, time DESC);
