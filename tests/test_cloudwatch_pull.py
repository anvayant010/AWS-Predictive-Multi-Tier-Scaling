from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

from forecasting.data_pipeline.cloudwatch_pull import (
    fetch_catalog_metrics,
    fetch_thumbnail_metrics,
    run,
    write_metrics,
)


@mock_aws
def test_fetch_catalog_metrics_reads_request_count_and_latency() -> None:
    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    now = datetime.now(timezone.utc)
    dimensions = [{"Name": "LoadBalancer", "Value": "app/test-catalog/abc123"}]
    cloudwatch.put_metric_data(
        Namespace="AWS/ApplicationELB",
        MetricData=[
            {"MetricName": "RequestCount", "Dimensions": dimensions, "Value": 42.0, "Timestamp": now},
            {"MetricName": "TargetResponseTime", "Dimensions": dimensions, "Value": 0.2, "Timestamp": now},
        ],
    )

    rows = fetch_catalog_metrics(
        cloudwatch,
        "app/test-catalog/abc123",
        now - timedelta(minutes=5),
        now + timedelta(minutes=5),
    )

    metric_names = {row[2] for row in rows}
    assert metric_names == {"RequestCount", "TargetResponseTime"}
    assert all(row[1] == "catalog_service" and row[4] == "cloudwatch" for row in rows)


@mock_aws
def test_fetch_thumbnail_metrics_reads_invocations_and_throttles() -> None:
    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    now = datetime.now(timezone.utc)
    dimensions = [{"Name": "FunctionName", "Value": "test-thumbnail"}]
    cloudwatch.put_metric_data(
        Namespace="AWS/Lambda",
        MetricData=[
            {"MetricName": "Invocations", "Dimensions": dimensions, "Value": 10.0, "Timestamp": now},
            {"MetricName": "Throttles", "Dimensions": dimensions, "Value": 2.0, "Timestamp": now},
        ],
    )

    rows = fetch_thumbnail_metrics(
        cloudwatch,
        "test-thumbnail",
        now - timedelta(minutes=5),
        now + timedelta(minutes=5),
    )

    metric_names = {row[2] for row in rows}
    assert "Invocations" in metric_names
    assert "Throttles" in metric_names
    assert all(row[1] == "thumbnail_service" and row[4] == "cloudwatch" for row in rows)


def test_write_metrics_returns_zero_for_empty_rows() -> None:
    conn = MagicMock()
    assert write_metrics(conn, []) == 0
    conn.cursor.assert_not_called()


@patch("forecasting.data_pipeline.cloudwatch_pull.execute_values")
def test_write_metrics_inserts_and_commits(mock_execute_values: MagicMock) -> None:
    conn = MagicMock()
    now = datetime.now(timezone.utc)
    rows = [(now, "catalog_service", "RequestCount", 1.0, "cloudwatch")]

    written = write_metrics(conn, rows)

    assert written == 1
    mock_execute_values.assert_called_once()
    assert mock_execute_values.call_args.args[2] == rows
    conn.commit.assert_called_once()


@mock_aws
def test_run_fetches_both_services_and_writes_all_rows() -> None:
    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    # Use a timestamp a couple of minutes in the past so its 60s bucket has
    # fully closed before `run()`'s end_time (~now) -- CloudWatch/moto won't
    # return a still-forming current-minute bucket.
    metric_time = datetime.now(timezone.utc) - timedelta(minutes=2)
    cloudwatch.put_metric_data(
        Namespace="AWS/ApplicationELB",
        MetricData=[
            {
                "MetricName": "RequestCount",
                "Dimensions": [{"Name": "LoadBalancer", "Value": "app/test-catalog/abc123"}],
                "Value": 5.0,
                "Timestamp": metric_time,
            }
        ],
    )
    cloudwatch.put_metric_data(
        Namespace="AWS/Lambda",
        MetricData=[
            {
                "MetricName": "Invocations",
                "Dimensions": [{"Name": "FunctionName", "Value": "test-thumbnail"}],
                "Value": 3.0,
                "Timestamp": metric_time,
            }
        ],
    )
    conn = MagicMock()

    with patch("forecasting.data_pipeline.cloudwatch_pull.execute_values") as mock_execute_values:
        written = run(cloudwatch, conn, "app/test-catalog/abc123", "test-thumbnail")

    assert written >= 2
    mock_execute_values.assert_called_once()
    conn.commit.assert_called_once()
