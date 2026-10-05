"""Turn the trained GBM quantile models into decision_engine `Forecast` objects.

Batched on purpose: the dry-run replays thousands of forecast origins, so features for every
(origin, horizon) pair are built at once and predicted in one call per quantile.

Features are causal (see forecasting.gbm and its leakage test): a forecast for an origin uses only
data at or before that origin, so passing the full series is safe.
"""

from __future__ import annotations

import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from decision_engine.models import Forecast
from forecasting.gbm import (
    HORIZONS,
    build_dataset,
    fit_quantile_models,
    origin_features,
    predict_quantiles,
    target_features,
)


class GbmForecaster:
    def __init__(
        self, service: str, models: dict[float, HistGradientBoostingRegressor], *, recent: bool = True
    ) -> None:
        self.service = service
        self.models = models
        self.recent = recent

    @classmethod
    def fit(
        cls,
        service: str,
        series: pd.Series,
        train_end: pd.Timestamp,
        *,
        recent: bool = True,
        origin_stride: int = 3,
    ) -> "GbmForecaster":
        """Fit on rows whose *target* is at or before train_end — nothing from the holdout."""
        X, y, _ = build_dataset(
            series, recent=recent, origin_stride=origin_stride, target_until=train_end
        )
        return cls(service, fit_quantile_models(X, y), recent=recent)

    def forecast_many(self, series: pd.Series, origins: pd.DatetimeIndex) -> list[Forecast]:
        """One 15-step Forecast per origin (step k = origin + k minutes)."""
        if len(origins) == 0:
            return []
        origin_feats = origin_features(series).reindex(origins) if self.recent else None
        if origin_feats is not None and origin_feats.isna().any().any():
            raise ValueError(
                "some origins lack enough history for the lag features (or are not in the series); "
                "refusing to forecast from incomplete inputs"
            )

        frames = []
        for h in HORIZONS:
            parts = [] if origin_feats is None else [origin_feats]
            parts.append(target_features(series, origins, h))
            frames.append(pd.concat(parts, axis=1))
        stacked = predict_quantiles(self.models, pd.concat(frames))

        n_origins, n_horizons = len(origins), len(HORIZONS)
        # Rows are ordered horizon-major: block h holds every origin for horizon h.
        arrays = {
            name: stacked[name].to_numpy().reshape(n_horizons, n_origins).T for name in ("p10", "p50", "p90")
        }
        return [
            Forecast(
                service=self.service,
                origin=origin.to_pydatetime(),
                p10=tuple(float(v) for v in arrays["p10"][i]),
                p50=tuple(float(v) for v in arrays["p50"][i]),
                p90=tuple(float(v) for v in arrays["p90"][i]),
            )
            for i, origin in enumerate(origins)
        ]

    def forecast(self, series: pd.Series, origin: pd.Timestamp) -> Forecast:
        return self.forecast_many(series, pd.DatetimeIndex([origin]))[0]

