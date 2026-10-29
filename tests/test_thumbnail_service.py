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


# --- cold-start marker (Phase 7) -------------------------------------------------------------------------


import demo_app.thumbnail_service.handler as handler_module  # noqa: E402


@pytest.fixture
def fresh_container(monkeypatch: pytest.MonkeyPatch):
    """Pretend the module was just imported in a brand-new on-demand Lambda container."""
    monkeypatch.setattr(handler_module, "_container_has_served", False)
    monkeypatch.delenv("AWS_LAMBDA_INITIALIZATION_TYPE", raising=False)
    return monkeypatch


def _call() -> dict:
    return json.loads(lambda_handler({"queryStringParameters": {"width": "8", "height": "8"}}, None)["body"])


def test_first_request_of_a_new_container_is_cold_and_later_ones_are_warm(fresh_container) -> None:
    first, second, third = _call(), _call(), _call()

    assert (first["cold_start"], second["cold_start"], third["cold_start"]) == (True, False, False)


def test_every_request_in_one_container_reports_the_same_container_id(fresh_container) -> None:
    assert _call()["container_id"] == _call()["container_id"] == handler_module._CONTAINER_ID


def test_a_new_container_gets_a_different_id() -> None:
    import importlib

    before = handler_module._CONTAINER_ID
    reloaded = importlib.reload(handler_module)
    try:
        assert reloaded._CONTAINER_ID != before
        assert reloaded._container_has_served is False  # a reloaded module is a fresh container
    finally:
        importlib.reload(handler_module)


def test_provisioned_concurrency_containers_never_report_cold(fresh_container) -> None:
    fresh_container.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "provisioned-concurrency")

    body = _call()

    assert body["cold_start"] is False and body["init_type"] == "provisioned-concurrency"


def test_the_marker_is_on_error_responses_too(fresh_container) -> None:
    response = lambda_handler({"queryStringParameters": {"width": "-1"}}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 400 and body["cold_start"] is True and "container_id" in body


def test_the_marker_does_not_change_the_thumbnail_itself(fresh_container) -> None:
    body = _call()

    assert body["thumbnail_hash"] == generate_thumbnail("unknown", 8, 8)["thumbnail_hash"]
