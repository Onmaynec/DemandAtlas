from backend.app.domain import replenishment
from forecast_service.forecasting import MODELS, backtest, predict, scores


def test_replenishment_acceptance_at09():
    p = replenishment(35, 12, 8, 10, 10, 1)
    assert p["raw_need"] == 25
    assert p["recommended"] == 30
    assert "25" in p["explanation"] and "30" in p["explanation"]


def test_zero_demand_does_not_trigger_moq():
    p = replenishment(0, 20, 5, 0, 10, 30)
    assert p["recommended"] == 0


def test_moq_and_pack_multiple():
    p = replenishment(1, 0, 0, 0, 12, 25)
    assert p["recommended"] == 36


def test_wape_zero_actual_at07():
    result = scores([0, 0, 0], [3, 2, 1])
    assert result["wape"] is None
    assert result["wape_state"] == "undefined_zero_actual"


def test_forecast_determinism_and_common_origins():
    history = [float(4 + i % 7) for i in range(120)]
    first = backtest(history, 28, seed=42)
    second = backtest(history, 28, seed=42)
    assert first == second
    assert set(MODELS) == set(first["models"])
    assert first["origins"][0] == 28
    assert first["origins"][-1] == len(history) - 28
    assert len(first["prediction"]) == 28


def test_rolling_origin_cannot_train_on_future():
    history = [5.0] * 110
    result = backtest(history, 14)
    first_origin = result["origins"][0]
    assert predict(history[:first_origin], 14, "moving_average") == [5.0] * 14
    modified_future = history[:first_origin] + [999.0] * (110 - first_origin)
    assert predict(modified_future[:first_origin], 14, "moving_average") == [5.0] * 14
