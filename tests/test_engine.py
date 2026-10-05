from datetime import datetime, timezone

import pytest

from decision_engine.classifier import ClassifierConfig
from decision_engine.engine import ROUTES, Route, decide
from decision_engine.models import Aggressiveness, Forecast, LoadShape
from decision_engine.policy import Ec2PolicyConfig, LambdaPolicyConfig

ORIGIN = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def forecast(service: str, p90: list[float]) -> Forecast:
    p50 = tuple(v * 0.6 for v in p90)
    return Forecast(service, ORIGIN, tuple(v * 0.3 for v in p90), p50, tuple(p90))


def test_catalog_routes_to_ec2_and_thumbnail_to_lambda() -> None:
    catalog = decide(forecast("catalog_service", [120.0] * 15), current=1)
    thumbnail = decide(forecast("thumbnail_service", [400.0] * 15), current=0)

    assert catalog.scaling.target == "ec2_asg"
    assert thumbnail.scaling.target == "lambda_provisioned_concurrency"


def test_unknown_service_is_rejected_not_guessed() -> None:
    with pytest.raises(ValueError, match="no route configured"):
        decide(forecast("mystery_service", [10.0] * 15), current=1)


def test_a_route_must_target_exactly_one_primitive() -> None:
    cls = ClassifierConfig(quiet_below=10)
    with pytest.raises(ValueError):
        Route(classifier=cls)
    with pytest.raises(ValueError):
        Route(classifier=cls, ec2=Ec2PolicyConfig(), lambda_=LambdaPolicyConfig())


def test_bursty_catalog_forecast_is_not_chased_by_ec2() -> None:
    spike = [40.0] * 11 + [300.0, 400.0, 380.0, 350.0]

    decision = decide(forecast("catalog_service", spike), current=1, aggressiveness=Aggressiveness.BALANCED)

    assert decision.classification.shape is LoadShape.BURSTY
    assert decision.scaling.desired == 1  # sized to the ~40 req/min baseline, not the 400 spike


def test_sustained_catalog_forecast_scales_up_to_the_peak() -> None:
    decision = decide(forecast("catalog_service", [200.0] * 15), current=1)

    assert decision.classification.shape is LoadShape.SUSTAINED
    assert decision.scaling.desired == 4  # ceil(200 / 50)


def test_lambda_burst_warning_provisions_concurrency_and_quiet_does_not() -> None:
    imminent = [60.0, 250.0, 400.0] + [2.0] * 12  # burst inside the 3-minute lookahead
    quiet = [2.0] * 15

    assert decide(forecast("thumbnail_service", imminent), current=0).scaling.desired >= 1
    assert decide(forecast("thumbnail_service", quiet), current=0).scaling.desired == 0


def test_lambda_ignores_a_burst_forecast_beyond_its_lookahead() -> None:
    # Provisioning now for a burst 13+ minutes out would just pay for idle capacity.
    distant = [2.0] * 12 + [60.0, 250.0, 400.0]

    assert decide(forecast("thumbnail_service", distant), current=0).scaling.desired == 0


def test_every_route_respects_its_caps_for_an_absurd_forecast() -> None:
    for service in ROUTES:
        decision = decide(
            forecast(service, [1e12] * 15), current=1, aggressiveness=Aggressiveness.LATENCY_OPTIMIZED
        )
        assert decision.scaling.cap_applied is True
        route = ROUTES[service]
        limit = route.ec2.max_instances if route.ec2 else route.lambda_.max_concurrency
        assert decision.scaling.desired == limit
