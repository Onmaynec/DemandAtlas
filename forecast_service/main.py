import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from .forecasting import backtest, predict, MODEL_VERSION

app = FastAPI(title="DemandAtlas Forecasting", version="0.1.0")


class ForecastInput(BaseModel):
    history: list[float] = Field(min_length=1, max_length=1500)
    horizon: int = 7
    seed: int = 42
    parameters: dict = Field(default_factory=dict)
    model: str | None = None


def authorize(secret: str | None):
    expected = os.getenv("FORECAST_SERVICE_TOKEN", "")
    if expected and secret != expected:
        raise HTTPException(401, detail="Invalid internal service token")


@app.get("/health")
def health():
    return {"status": "ok", "model_version": MODEL_VERSION}


@app.post("/backtest")
def evaluate(body: ForecastInput, x_service_token: str | None = Header(default=None)):
    authorize(x_service_token)
    try:
        return backtest(body.history, body.horizon, body.seed, body.parameters)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@app.post("/predict")
def forecast(body: ForecastInput, x_service_token: str | None = Header(default=None)):
    authorize(x_service_token)
    try:
        return {"prediction": predict(body.history, body.horizon, body.model or "seasonal_naive", body.parameters),
                "model_version": MODEL_VERSION, "seed": body.seed}
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
