"""Deterministic forecast baselines and rolling-origin evaluation.

A fold is fitted only on values before its origin. All candidates share folds.
No random sampling is used; seed is recorded for reproducibility contracts.
"""
from dataclasses import dataclass
from math import isfinite
from statistics import mean


MODEL_VERSION = "baseline-1"
MODELS = ("seasonal_naive", "moving_average", "exponential_smoothing", "zero")


def predict(history: list[float], horizon: int, model: str, parameters: dict | None = None) -> list[float]:
    if not history or horizon not in (7, 14, 28):
        raise ValueError("A nonempty history and a 7/14/28-day horizon are required")
    if any(not isfinite(float(x)) or x < 0 for x in history):
        raise ValueError("History must contain finite non-negative values")
    p = parameters or {}
    values = [float(x) for x in history]
    if model == "zero":
        return [0.0] * horizon
    if model == "seasonal_naive":
        period = int(p.get("period", 7))
        if period < 1 or period > len(values):
            raise ValueError("Invalid seasonal period")
        pattern = values[-period:]
        return [round(pattern[i % period], 4) for i in range(horizon)]
    if model == "moving_average":
        window = int(p.get("window", 7))
        if window < 1 or window > len(values):
            raise ValueError("Invalid moving average window")
        level = mean(values[-window:])
        return [round(level, 4)] * horizon
    if model == "exponential_smoothing":
        alpha = float(p.get("alpha", 0.3))
        if not 0 < alpha <= 1:
            raise ValueError("Smoothing alpha must be between 0 and 1")
        level = values[0]
        for current in values[1:]:
            level = alpha * current + (1 - alpha) * level
        return [round(level, 4)] * horizon
    raise ValueError("Unknown model")


def scores(actual: list[float], predicted: list[float]) -> dict:
    if not actual or len(actual) != len(predicted):
        raise ValueError("Actual and predicted lengths must agree")
    errors = [p - a for p, a in zip(predicted, actual)]
    abs_errors = [abs(e) for e in errors]
    total = sum(actual)
    return {
        "mae": round(mean(abs_errors), 6),
        "wape": round(sum(abs_errors) / total, 6) if total > 0 else None,
        "bias": round(mean(errors), 6),
        "wape_state": "defined" if total > 0 else "undefined_zero_actual",
    }


def backtest(history: list[float], horizon: int, seed: int = 42,
             parameters: dict | None = None, min_train: int = 28) -> dict:
    if horizon not in (7, 14, 28):
        raise ValueError("Unsupported horizon")
    if len(history) < min_train + horizon:
        raise ValueError("Insufficient observations for rolling-origin backtest")
    if any(not isfinite(float(x)) or x < 0 for x in history):
        raise ValueError("History must contain finite non-negative values")
    # Every method sees precisely the same chronological train/test cutoffs.
    step = max(1, horizon // 2)
    origins = list(range(min_train, len(history) - horizon + 1, step))
    if origins[-1] != len(history) - horizon:
        origins.append(len(history) - horizon)
    candidates = {}
    for model in MODELS:
        actual, predicted = [], []
        for origin in origins:
            fold_forecast = predict(history[:origin], horizon, model, parameters)
            actual.extend(history[origin:origin + horizon])
            predicted.extend(fold_forecast)
        candidates[model] = scores(actual, predicted)
    # WAPE is intentionally undefined for zero demand; fall back to MAE.
    winner = min(
        MODELS, key=lambda m: (
            candidates[m]["wape"] if candidates[m]["wape"] is not None else candidates[m]["mae"],
            candidates[m]["mae"], MODELS.index(m)
        )
    )
    return {
        "horizon": horizon,
        "seed": seed,
        "model_version": MODEL_VERSION,
        "origins": origins,
        "fold_count": len(origins),
        "models": candidates,
        "winner": winner,
        "prediction": predict(history, horizon, winner, parameters),
        "parameters": parameters or {},
    }
