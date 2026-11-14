"""Forecasting and procurement API. Published facts are append-only."""
import os
from datetime import date, timedelta
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from .auth import Principal, current_user, require_scope, roles
from .db import get_db
from .domain import replenishment
from .models import (
    DailySalesMart, DatasetState, Forecast, Inventory, Plan, PlanRevision, Product, Store
)

router = APIRouter(prefix="/api/v1")


class ForecastInput(BaseModel):
    storeId: str | None = None
    sku: str | None = None
    horizon: int = Field(default=7)
    seed: int = 42
    parameters: dict = Field(default_factory=dict)


class PublishInput(ForecastInput):
    model: str | None = None


class ScenarioInput(BaseModel):
    forecastId: str | None = None
    forecastQty: float | None = Field(default=None, ge=0)
    storeId: str
    sku: str
    leadDays: int | None = Field(default=None, ge=0, le=28)
    reviewDays: int = Field(default=7, ge=0, le=28)
    safetyStock: int = Field(default=0, ge=0)
    onHand: int | None = Field(default=None, ge=0)
    inbound: int | None = Field(default=None, ge=0)


class PlanInput(BaseModel):
    forecastId: str
    safetyStock: int = Field(default=0, ge=0)
    reviewDays: int = Field(default=7, ge=0, le=28)


class VersionInput(BaseModel):
    expectedVersion: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=400)


def check_dimensions(db: Session, user: Principal, store_id: str | None, sku: str | None):
    if store_id is not None and not db.get(Store, store_id):
        raise HTTPException(404, "Unknown store")
    product = db.get(Product, sku) if sku is not None else None
    if sku is not None and not product:
        raise HTTPException(404, "Unknown SKU")
    if user.categories and sku is None:
        raise HTTPException(400, "Category-scoped users must select a SKU")
    require_scope(user, store_id, product.category if product else None)
    return product


def get_history(db: Session, user: Principal, store_id: str | None, sku: str | None):
    check_dimensions(db, user, store_id, sku)
    stmt = select(
        DailySalesMart.date, func.sum(DailySalesMart.units),
        func.max(DailySalesMart.promo.cast(Integer)), func.max(DailySalesMart.stockout.cast(Integer))
    ).where(DailySalesMart.units >= 0)
    if store_id:
        stmt = stmt.where(DailySalesMart.store_id == store_id)
    if sku:
        stmt = stmt.where(DailySalesMart.sku == sku)
    if user.stores:
        stmt = stmt.where(DailySalesMart.store_id.in_(user.stores))
    items = db.execute(stmt.group_by(DailySalesMart.date).order_by(DailySalesMart.date)).all()
    if not items:
        raise HTTPException(422, "No historical sales for these dimensions")
    start, end = items[0][0], items[-1][0]
    by_day = {item[0]: item for item in items}
    history, flags = [], {"missingDays": [], "promotionDays": 0, "stockoutDays": 0}
    current = start
    while current <= end:
        item = by_day.get(current)
        if item:
            history.append(float(item[1]))
            flags["promotionDays"] += int(bool(item[2]))
            flags["stockoutDays"] += int(bool(item[3]))
        else:
            # Explicitly surface imputation, never imply a complete time series.
            history.append(0.0)
            flags["missingDays"].append(current.isoformat())
        current += timedelta(days=1)
    if len(history) > 1500:
        history = history[-1500:]
        flags["missingDays"] = [d for d in flags["missingDays"] if d >=
                                 (end - timedelta(days=1499)).isoformat()]
    return history, end, flags


def calculate(history, horizon, seed, parameters):
    url = os.getenv("FORECAST_URL", "").rstrip("/")
    if url:
        try:
            response = httpx.post(url + "/backtest", json={
                "history": history, "horizon": horizon, "seed": seed,
                "parameters": parameters
            }, headers={"X-Service-Token": os.getenv("FORECAST_SERVICE_TOKEN", "")}, timeout=30.0)
            if response.status_code == 422:
                raise HTTPException(422, response.json().get("detail", "Invalid forecast"))
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Forecast service unavailable") from exc
    # Offline / test mode uses the very same forecasting implementation.
    from forecast_service.forecasting import backtest
    try:
        return backtest(history, horizon, seed, parameters)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/forecasts/backtest")
def forecast_backtest(
    body: ForecastInput, user: Principal = Depends(roles("analyst", "category_manager")),
    db: Session = Depends(get_db)
):
    history, last_day, flags = get_history(db, user, body.storeId, body.sku)
    result = calculate(history, body.horizon, body.seed, body.parameters)
    return {"scope": {"storeId": body.storeId, "sku": body.sku},
            "dataThrough": last_day, "historyQuality": flags,
            "datasetVersion": db.scalar(select(DatasetState.version).where(DatasetState.id == 1)) or 0,
            **result}


@router.post("/forecasts/publish", status_code=201)
def forecast_publish(
    body: PublishInput, user: Principal = Depends(roles("analyst", "category_manager")),
    db: Session = Depends(get_db)
):
    history, last_day, flags = get_history(db, user, body.storeId, body.sku)
    result = calculate(history, body.horizon, body.seed, body.parameters)
    model = body.model or result["winner"]
    if model not in result["models"]:
        raise HTTPException(400, "Unknown model")
    from forecast_service.forecasting import predict
    future = predict(history, body.horizon, model, body.parameters)
    version = db.scalar(select(DatasetState.version).where(DatasetState.id == 1)) or 0
    item = Forecast(
        id=str(uuid4()), store_id=body.storeId, sku=body.sku, horizon=body.horizon,
        model=model, model_version=result["model_version"], dataset_version=version,
        seed=body.seed, parameters=body.parameters, scores=result["models"][model],
        points=[{"date": (last_day + timedelta(days=i + 1)).isoformat(), "prediction": value}
                for i, value in enumerate(future)],
        created_by=user.name,
    )
    db.add(item)
    db.commit()
    return {"id": item.id, "datasetVersion": item.dataset_version, "modelVersion": item.model_version,
            "model": item.model, "scores": item.scores, "points": item.points, "historyQuality": flags}


@router.get("/forecasts")
def list_forecasts(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    if user.role in ("data_engineer", "access_admin"):
        raise HTTPException(403, "Not permitted")
    stmt = select(Forecast).order_by(Forecast.created_at.desc()).limit(100)
    if user.stores:
        stmt = stmt.where(Forecast.store_id.in_(user.stores))
    if user.categories:
        stmt = stmt.where(Forecast.sku.in_(select(Product.sku).where(Product.category.in_(user.categories))))
    return [{"id": f.id, "storeId": f.store_id, "sku": f.sku, "model": f.model,
             "horizon": f.horizon, "datasetVersion": f.dataset_version, "scores": f.scores,
             "seed": f.seed, "points": f.points, "createdAt": f.created_at} for f in db.scalars(stmt)]


def compute_scenario(db: Session, user: Principal, body: ScenarioInput, require_forecast=False):
    product = check_dimensions(db, user, body.storeId, body.sku)
    inventory = db.scalar(select(Inventory).where(
        Inventory.store_id == body.storeId, Inventory.sku == body.sku
    ))
    if not inventory:
        raise HTTPException(422, "Inventory row missing")
    forecast = db.get(Forecast, body.forecastId) if body.forecastId else None
    if body.forecastId and not forecast:
        raise HTTPException(404, "Forecast not found")
    if require_forecast and not forecast:
        raise HTTPException(422, "A published forecast is required")
    lead = body.leadDays if body.leadDays is not None else product.lead_days
    days = lead + body.reviewDays
    if forecast:
        if forecast.store_id != body.storeId or forecast.sku != body.sku:
            raise HTTPException(400, "Forecast dimensions do not match plan")
        if days > forecast.horizon:
            raise HTTPException(422, "Forecast horizon shorter than lead + review days")
        qty = sum(float(item["prediction"]) for item in forecast.points[:days])
    else:
        if body.forecastQty is None:
            raise HTTPException(422, "forecastId or forecastQty is required")
        qty = body.forecastQty
    on_hand = body.onHand if body.onHand is not None else inventory.on_hand
    inbound = body.inbound if body.inbound is not None else inventory.inbound
    result = replenishment(qty, on_hand, inbound, body.safetyStock,
                           product.pack_size, product.min_order)
    result.update({"storeId": body.storeId, "sku": body.sku, "leadDays": lead,
                   "reviewDays": body.reviewDays, "forecastId": body.forecastId,
                   "whatIf": True, "persisted": False})
    return result


@router.post("/replenishment/scenario")
def scenario(body: ScenarioInput,
             user: Principal = Depends(roles("analyst", "category_manager", "director")),
             db: Session = Depends(get_db)):
    return compute_scenario(db, user, body)


@router.post("/plans", status_code=201)
def create_plan(body: PlanInput,
                user: Principal = Depends(roles("analyst", "category_manager")),
                db: Session = Depends(get_db)):
    forecast = db.get(Forecast, body.forecastId)
    if not forecast:
        raise HTTPException(404, "Forecast not found")
    if not forecast.store_id or not forecast.sku:
        raise HTTPException(422, "Plan requires store/SKU-level forecast")
    scenario_body = ScenarioInput(forecastId=forecast.id, storeId=forecast.store_id,
                                  sku=forecast.sku, safetyStock=body.safetyStock,
                                  reviewDays=body.reviewDays)
    result = compute_scenario(db, user, scenario_body, require_forecast=True)
    plan = Plan(
        id=str(uuid4()), forecast_id=forecast.id, store_id=forecast.store_id,
        sku=forecast.sku, quantity=result["recommended"], breakdown=result,
        status="draft", version=1, created_by=user.name, updated_by=user.name
    )
    db.add(plan)
    db.flush()
    db.add(PlanRevision(plan_id=plan.id, version=1, status="draft",
                        actor=user.name, payload=result))
    db.commit()
    return {"id": plan.id, "status": plan.status, "version": plan.version,
            "quantity": plan.quantity, "breakdown": plan.breakdown}


@router.get("/plans")
def list_plans(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == "data_engineer":
        raise HTTPException(403, "Not permitted")
    stmt = select(Plan).order_by(Plan.created_at.desc()).limit(100)
    if user.stores:
        stmt = stmt.where(Plan.store_id.in_(user.stores))
    if user.categories:
        stmt = stmt.where(Plan.sku.in_(select(Product.sku).where(Product.category.in_(user.categories))))
    if user.role == "observer":
        # Read-only observers may not see unpublished procurement drafts.
        stmt = stmt.where(Plan.status == "approved")
    return [{"id": p.id, "status": p.status, "storeId": p.store_id,
             "sku": p.sku, "quantity": p.quantity, "version": p.version,
             "createdBy": p.created_by, "breakdown": p.breakdown} for p in db.scalars(stmt)]


def transition(db: Session, user: Principal, plan_id: str, body: VersionInput,
               allowed: tuple[str, ...], target: str):
    plan = db.get(Plan, plan_id, with_for_update=True)
    if not plan:
        raise HTTPException(404, "Plan not found")
    product = db.get(Product, plan.sku)
    require_scope(user, plan.store_id, product.category)
    if plan.version != body.expectedVersion:
        raise HTTPException(409, "Plan was changed; refresh the expected version")
    if plan.status not in allowed:
        raise HTTPException(409, f"Invalid state transition from {plan.status} to {target}")
    # Approved plans are immutable. A revised order must be a new plan.
    plan.status = target
    plan.version += 1
    plan.updated_by = user.name
    from .models import now
    plan.updated_at = now()
    db.add(PlanRevision(plan_id=plan.id, version=plan.version, status=target,
                        actor=user.name, payload={"reason": body.reason,
                                                  "quantity": plan.quantity,
                                                  "breakdown": plan.breakdown}))
    db.commit()
    return {"id": plan.id, "status": plan.status, "version": plan.version}


@router.post("/plans/{plan_id}/submit")
def submit(plan_id: str, body: VersionInput,
           user: Principal = Depends(roles("analyst", "category_manager")),
           db: Session = Depends(get_db)):
    return transition(db, user, plan_id, body, ("draft", "rejected"), "pending")


@router.post("/plans/{plan_id}/approve")
def approve(plan_id: str, body: VersionInput,
            user: Principal = Depends(roles("director")), db: Session = Depends(get_db)):
    return transition(db, user, plan_id, body, ("pending",), "approved")


@router.post("/plans/{plan_id}/reject")
def reject(plan_id: str, body: VersionInput,
           user: Principal = Depends(roles("director")), db: Session = Depends(get_db)):
    return transition(db, user, plan_id, body, ("pending",), "rejected")
