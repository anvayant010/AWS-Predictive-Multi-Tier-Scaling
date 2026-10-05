"""Route a service's forecast to the right classifier + policy.

Each service is tied to exactly one compute primitive (catalog code runs only on EC2 behind the
ALB; thumbnail is a Lambda handler), so this is a fixed lookup, not a choice between tiers. What
varies is the *mode* inside the tier, which the classifier supplies.

Pure and dry-run only: it returns decisions, it never calls AWS.
"""

from __future__ import annotations

from dataclasses import dataclass

from decision_engine.classifier import CATALOG_CLASSIFIER, THUMBNAIL_CLASSIFIER, ClassifierConfig, classify
from decision_engine.models import Aggressiveness, Classification, Forecast, ScalingDecision
from decision_engine.policy import (
    Ec2PolicyConfig,
    LambdaPolicyConfig,
    ec2_desired_capacity,
    lambda_provisioned_concurrency,
)


@dataclass(frozen=True)
class Route:
    classifier: ClassifierConfig
    ec2: Ec2PolicyConfig | None = None
    lambda_: LambdaPolicyConfig | None = None

    def __post_init__(self) -> None:
        if (self.ec2 is None) == (self.lambda_ is None):
            raise ValueError("a route must target exactly one primitive: EC2 or Lambda")


ROUTES: dict[str, Route] = {
    "catalog_service": Route(classifier=CATALOG_CLASSIFIER, ec2=Ec2PolicyConfig()),
    "thumbnail_service": Route(classifier=THUMBNAIL_CLASSIFIER, lambda_=LambdaPolicyConfig()),
}


@dataclass(frozen=True)
class Decision:
    classification: Classification
    scaling: ScalingDecision


def decide(
    forecast: Forecast,
    current: int,
    aggressiveness: Aggressiveness = Aggressiveness.BALANCED,
    routes: dict[str, Route] = ROUTES,
) -> Decision:
    """Classify the forecast, then size capacity for the service's own primitive."""
    if forecast.service not in routes:
        raise ValueError(f"no route configured for service {forecast.service!r}")
    route = routes[forecast.service]
    classification = classify(forecast, route.classifier)

    if route.ec2 is not None:
        scaling = ec2_desired_capacity(forecast, current, route.ec2, aggressiveness, shape=classification.shape)
    else:
        assert route.lambda_ is not None  # guaranteed by Route.__post_init__
        scaling = lambda_provisioned_concurrency(forecast, current, route.lambda_, aggressiveness)
    return Decision(classification=classification, scaling=scaling)
