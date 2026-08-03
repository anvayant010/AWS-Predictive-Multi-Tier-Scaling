import json

import pytest

from demo_app.thumbnail_service.handler import generate_thumbnail, lambda_handler


def test_generate_thumbnail_returns_expected_fields() -> None:
    result = generate_thumbnail("img-1", 128, 128)
    assert result["image_id"] == "img-1"
    assert result["width"] == 128
    assert result["height"] == 128
    assert len(result["thumbnail_hash"]) == 64
    assert result["processing_time_ms"] > 0


def test_generate_thumbnail_rejects_non_positive_dimensions() -> None:
    with pytest.raises(ValueError):
        generate_thumbnail("img-1", 0, 128)


def test_generate_thumbnail_is_deterministic_for_same_input() -> None:
    first = generate_thumbnail("img-1", 64, 64)
    second = generate_thumbnail("img-1", 64, 64)
    assert first["thumbnail_hash"] == second["thumbnail_hash"]


def test_lambda_handler_success() -> None:
    event = {"queryStringParameters": {"image_id": "img-42", "width": "64", "height": "64"}}
    response = lambda_handler(event, context=None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["image_id"] == "img-42"


def test_lambda_handler_defaults_when_no_params() -> None:
    response = lambda_handler({}, context=None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["image_id"] == "unknown"
    assert body["width"] == 128


def test_lambda_handler_rejects_invalid_dimensions() -> None:
    event = {"queryStringParameters": {"width": "-10"}}
    response = lambda_handler(event, context=None)
    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert "error" in body
