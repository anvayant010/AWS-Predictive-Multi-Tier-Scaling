"""Pulls CloudWatch metrics for catalog_service and thumbnail_service into TimescaleDB.

Deliberately run as a plain script/module, not deployed as a real Lambda yet -- see
docs/design.md Phase 3 for why (a real Lambda can't reach the local TimescaleDB instance).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import boto3
from psycopg2.extensions import connection as Connection
from psycopg2.extras import execute_values

from forecasting.data_pipeline.db import get_connection

CATALOG_STATS = {"RequestCount": "Sum", "TargetResponseTime": "Average"}
THUMBNAIL_STATS = {
    "Invocations": "Sum",
    "ConcurrentExecutions": "Maximum",
    "Throttles": "Sum",
}

MetricRow = tuple[datetime, str, str, float, str]


def fetch_metric(
    cloudwatch,
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    stat: str,
    start_time: datetime,
    end_time: datetime,
    period_seconds: int = 60,
) -> list[tuple[datetime, float]]:
    response = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start_time,
        EndTime=end_time,
        Period=period_seconds,
        Statistics=[stat],
    )
    return sorted((dp["Timestamp"], dp[stat]) for dp in response["Datapoints"])


def fetch_catalog_metrics(
    cloudwatch, load_balancer_dimension: str, start_time: datetime, end_time: datetime
) -> list[MetricRow]:
    dimensions = [{"Name": "LoadBalancer", "Value": load_balancer_dimension}]
    rows: list[MetricRow] = []
    for metric_name, stat in CATALOG_STATS.items():
        for timestamp, value in fetch_metric(
            cloudwatch, "AWS/ApplicationELB", metric_name, dimensions, stat, start_time, end_time
        ):
            rows.append((timestamp, "catalog_service", metric_name, value, "cloudwatch"))
    return rows


def fetch_thumbnail_metrics(
    cloudwatch, function_name: str, start_time: datetime, end_time: datetime
) -> list[MetricRow]:
    dimensions = [{"Name": "FunctionName", "Value": function_name}]
    rows: list[MetricRow] = []
    for metric_name, stat in THUMBNAIL_STATS.items():
        for timestamp, value in fetch_metric(
            cloudwatch, "AWS/Lambda", metric_name, dimensions, stat, start_time, end_time
        ):
            rows.append((timestamp, "thumbnail_service", metric_name, value, "cloudwatch"))
    return rows


def write_metrics(conn: Connection, rows: list[MetricRow]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO metrics (time, service, metric_name, value, source)
            VALUES %s
            ON CONFLICT (time, service, metric_name, source) DO NOTHING
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def run(
    cloudwatch,
    conn: Connection,
    load_balancer_dimension: str,
    thumbnail_function_name: str,
    lookback_minutes: int = 5,
) -> int:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=lookback_minutes)
    rows = fetch_catalog_metrics(cloudwatch, load_balancer_dimension, start_time, end_time)
    rows += fetch_thumbnail_metrics(cloudwatch, thumbnail_function_name, start_time, end_time)
    return write_metrics(conn, rows)


if __name__ == "__main__":
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "predictive-scaling"))
    written = run(
        cloudwatch=session.client("cloudwatch"),
        conn=get_connection(),
        load_balancer_dimension=os.environ["CATALOG_LB_DIMENSION"],
        thumbnail_function_name=os.environ["THUMBNAIL_FUNCTION_NAME"],
        lookback_minutes=int(os.environ.get("LOOKBACK_MINUTES", "5")),
    )
    print(f"wrote {written} rows")
