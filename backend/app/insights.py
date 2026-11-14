"""Auditable KPI contributions and non-probabilistic stock exposure indicators.

All aggregates use the published daily mart, not raw receipts. Missing
store-days are treated as unknown coverage, never as verified zero demand.
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .auth import Principal, current_user, require_scope, roles
from .db import get_db
from .models import (
    DailyCoverage, DailySalesMart, DatasetState, Inventory, Product, Store
)

router = APIRouter(prefix="/api/v1")
Metric = Literal["revenue", "units", "margin"]
Dimension = Literal["store", "region", "category", "sku"]
MAX_DIMENSIONS = 10000


def allowed_scope(db: Session, user: Principal, store: str | None,
                  category: str | None, sku: str | None) -> None:
    if store is not None:
        if db.get(Store, store) is None:
            raise HTTPException(404, "Store not found")
        require_scope(user, store=store)
    if category is not None:
        require_scope(user, category=category)
    if sku is not None:
        item = db.get(Product, sku)
        if item is None:
            raise HTTPException(404, "SKU not found")
        require_scope(user, category=item.category)
        if category is not None and category != item.category:
            raise HTTPException(400, "SKU and category filters conflict")


def scoped_sales(db: Session, user: Principal, start: date, end: date,
                 metric: Metric, group_by: Dimension, store: str | None,
                 region: str | None, category: str | None,
                 sku: str | None) -> dict[str, Decimal]:
    dimension = {
        "store": DailySalesMart.store_id, "region": Store.region,
        "category": Product.category, "sku": DailySalesMart.sku,
    }[group_by]
    expression = {
        "revenue": DailySalesMart.revenue,
        "units": DailySalesMart.units,
        "margin": DailySalesMart.revenue - DailySalesMart.cost,
    }[metric]
    query = select(dimension, func.sum(expression)).select_from(
        DailySalesMart
    ).join(Product, Product.sku == DailySalesMart.sku).join(
        Store, Store.id == DailySalesMart.store_id
    ).where(DailySalesMart.date.between(start, end))
    if store:
        query = query.where(DailySalesMart.store_id == store)
    if region:
        query = query.where(Store.region == region)
    if category:
        query = query.where(Product.category == category)
    if sku:
        query = query.where(DailySalesMart.sku == sku)
    if user.stores:
        query = query.where(DailySalesMart.store_id.in_(user.stores))
    if user.categories:
        query = query.where(Product.category.in_(user.categories))
    rows = db.execute(query.group_by(dimension).order_by(dimension).limit(
        MAX_DIMENSIONS + 1
    )).all()
    if len(rows) > MAX_DIMENSIONS:
        raise HTTPException(413, "Too many dimensions; narrow the report filters")
    return {str(name): Decimal(str(value or 0)) for name, value in rows}


def period_coverage(db: Session, user: Principal, start: date, end: date,
                    store: str | None, region: str | None) -> dict:
    query = select(Store.id)
    if store:
        query = query.where(Store.id == store)
    if region:
        query = query.where(Store.region == region)
    if user.stores:
        query = query.where(Store.id.in_(user.stores))
    ids = list(db.scalars(query))
    expected = len(ids) * ((end - start).days + 1)
    observed = 0
    if ids:
        observed = db.scalar(select(func.count()).select_from(DailyCoverage).where(
            DailyCoverage.store_id.in_(ids),
            DailyCoverage.date.between(start, end),
        )) or 0
    return {
        "observedStoreDays": observed, "expectedStoreDays": expected,
        "coveragePct": round(observed * 100 / expected, 2) if expected else None,
        "incomplete": observed != expected,
    }


def decimal_number(value: Decimal, metric: Metric) -> float | int:
    if metric == "units":
        return int(value)
    return float(value.quantize(Decimal("0.01")))


def contribution_report(db: Session, user: Principal, start: date, end: date,
                        group_by: Dimension, metric: Metric, store: str | None,
                        region: str | None, category: str | None, sku: str | None,
                        limit: int) -> dict:
    if end < start or (end - start).days > 365:
        raise HTTPException(400, "Comparison range must be 1–366 days")
    allowed_scope(db, user, store, category, sku)
    length = end - start + timedelta(days=1)
    before_end = start - timedelta(days=1)
    before_start = start - length
    current = scoped_sales(db, user, start, end, metric, group_by,
                           store, region, category, sku)
    previous = scoped_sales(db, user, before_start, before_end, metric, group_by,
                            store, region, category, sku)
    current_total = sum(current.values(), Decimal(0))
    previous_total = sum(previous.values(), Decimal(0))
    delta = current_total - previous_total
    all_keys = current.keys() | previous.keys()
    changes = [(key, current.get(key, Decimal(0)),
                previous.get(key, Decimal(0))) for key in all_keys]
    changes.sort(key=lambda x: (-abs(x[1] - x[2]), x[0]))
    selected = changes[:limit]
    other = sum((now - past for _, now, past in changes[limit:]), Decimal(0))
    changes_json = []
    for key, now, past in selected:
        change = now - past
        changes_json.append({
            "dimension": key,
            "current": decimal_number(now, metric),
            "previous": decimal_number(past, metric),
            "absoluteChange": decimal_number(change, metric),
            "shareOfChangePct": (round(float(change / delta * 100), 2)
                                 if delta != 0 else None),
        })
    return {
        "metric": metric, "groupBy": group_by,
        "filters": {"store": store, "region": region,
                    "category": category, "sku": sku},
        "period": {"start": start, "end": end},
        "previousPeriod": {"start": before_start, "end": before_end},
        "currentTotal": decimal_number(current_total, metric),
        "previousTotal": decimal_number(previous_total, metric),
        "absoluteChange": decimal_number(delta, metric),
        "percentChange": (round(float(delta / previous_total * 100), 2)
                          if previous_total != 0 else None),
        "contributions": changes_json,
        "otherChange": decimal_number(other, metric),
        "totalDimensions": len(changes),
        "returnedDimensions": len(selected),
        "coverage": {
            "current": period_coverage(db, user, start, end, store, region),
            "previous": period_coverage(db, user, before_start, before_end,
                                        store, region),
        },
        "datasetVersion": db.scalar(
            select(DatasetState.version).where(DatasetState.id == 1)
        ) or 0,
        "method": (
            "Разница суммы опубликованных показателей за два равных "
            "последовательных периода. Вклад каждого измерения = "
            "текущий показатель − показатель прошлого периода. "
            "Это арифметическое разложение, не оценка причин изменения."
        ),
        "reconciled": sum((now - past for _, now, past in changes),
                          Decimal(0)) == delta,
    }


@router.get("/insights/contributions")
def contributions(
    start: date, end: date,
    metric: Metric = "revenue", group_by: Dimension = "sku",
    store: str | None = None, region: str | None = None,
    category: str | None = None, sku: str | None = None,
    limit: int = Query(default=30, ge=1, le=200),
    user: Principal = Depends(current_user), db: Session = Depends(get_db),
):
    if user.role == "data_engineer":
        raise HTTPException(403, "Business insights not permitted")
    return contribution_report(db, user, start, end, group_by, metric,
                               store, region, category, sku, limit)


@router.get("/replenishment/risks")
def risks(
    as_of: date | None = None,
    lookback_days: int = Query(default=28, ge=7, le=90),
    review_days: int = Query(default=7, ge=0, le=28),
    max_stock_days: int = Query(default=60, ge=30, le=365),
    min_coverage_pct: int = Query(default=80, ge=50, le=100),
    store: str | None = None, region: str | None = None,
    category: str | None = None, sku: str | None = None,
    status: Literal["all", "shortage", "overstock", "balanced", "insufficient_data"] = "all",
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: Principal = Depends(roles("analyst", "category_manager", "director")),
    db: Session = Depends(get_db),
):
    allowed_scope(db, user, store, category, sku)
    end = as_of or date.today()
    start = end - timedelta(days=lookback_days - 1)
    demand = select(
        DailySalesMart.store_id.label("store_id"),
        DailySalesMart.sku.label("sku"),
        func.sum(DailySalesMart.units).label("total_units"),
    ).where(DailySalesMart.date.between(start, end)).group_by(
        DailySalesMart.store_id, DailySalesMart.sku
    ).subquery()
    query = select(
        Inventory.store_id, Inventory.sku, Product.name,
        Product.category, Store.region, Inventory.on_hand, Inventory.inbound,
        Product.unit_cost, Product.lead_days,
        func.coalesce(demand.c.total_units, 0),
    ).select_from(Inventory).join(
        Product, Product.sku == Inventory.sku
    ).join(Store, Store.id == Inventory.store_id).outerjoin(
        demand, and_(demand.c.sku == Inventory.sku,
                     demand.c.store_id == Inventory.store_id)
    )
    if store:
        query = query.where(Inventory.store_id == store)
    if region:
        query = query.where(Store.region == region)
    if category:
        query = query.where(Product.category == category)
    if sku:
        query = query.where(Inventory.sku == sku)
    if user.stores:
        query = query.where(Inventory.store_id.in_(user.stores))
    if user.categories:
        query = query.where(Product.category.in_(user.categories))
    inventory = db.execute(query.order_by(Inventory.store_id, Inventory.sku)).all()
    stores = set(item[0] for item in inventory)
    observed = {}
    if stores:
        observed = dict(db.execute(select(
            DailyCoverage.store_id, func.count()
        ).where(
            DailyCoverage.store_id.in_(stores),
            DailyCoverage.date.between(start, end),
        ).group_by(DailyCoverage.store_id)).all())
    counts = {"shortage": 0, "overstock": 0,
              "balanced": 0, "insufficient_data": 0}
    rows = []
    for (store_id, sku_id, name, category_name, region_name,
         on_hand, inbound, unit_cost, lead_days, units) in inventory:
        covered = int(observed.get(store_id, 0))
        coverage_pct = covered * 100 / lookback_days
        available = int(on_hand) + int(inbound)
        average = Decimal(str(units or 0)) / Decimal(lookback_days)
        target_days = int(lead_days) + review_days
        if max_stock_days <= target_days:
            raise HTTPException(400, "max_stock_days must exceed lead_days + review_days")
        status_value = "balanced"
        shortage_units, excess_units, cover_days = 0, 0, None
        if coverage_pct < min_coverage_pct:
            status_value = "insufficient_data"
        elif average == 0:
            if available > 0:
                status_value = "overstock"
                excess_units = available
        else:
            cover_days = round(float(Decimal(available) / average), 2)
            if Decimal(available) < average * Decimal(target_days):
                status_value = "shortage"
                shortage_units = int(
                    (average * target_days - Decimal(available)).to_integral_value(
                        rounding=ROUND_CEILING)
                )
            elif Decimal(available) > average * Decimal(max_stock_days):
                status_value = "overstock"
                excess_units = int(max(
                    Decimal(0),
                    Decimal(available) - (
                        average * max_stock_days
                    ).to_integral_value(rounding=ROUND_CEILING)
                ))
        counts[status_value] += 1
        if status != "all" and status != status_value:
            continue
        rows.append({
            "storeId": store_id, "sku": sku_id, "name": name,
            "category": category_name, "region": region_name,
            "status": status_value, "onHand": int(on_hand),
            "confirmedInbound": int(inbound), "available": available,
            "leadDays": int(lead_days), "reviewDays": review_days,
            "unitsSold": int(units or 0),
            "averageDailyUnits": round(float(average), 4),
            "estimatedCoverDays": cover_days,
            "shortageUnits": shortage_units, "excessUnits": excess_units,
            "exposureAtUnitCost": round(float(
                Decimal(shortage_units or excess_units) * Decimal(str(unit_cost))
            ), 2),
            "observedStoreDays": covered,
            "coveragePct": round(coverage_pct, 2),
            "rule": (
                "insufficient_store_days" if status_value == "insufficient_data"
                else "no_demand" if average == 0
                else "below_lead_plus_review" if status_value == "shortage"
                else "above_max_stock_days" if status_value == "overstock"
                else "within_thresholds"
            ),
        })
    priority = {"shortage": 0, "overstock": 1,
                "insufficient_data": 2, "balanced": 3}
    rows.sort(key=lambda item: (
        priority[item["status"]],
        -item["exposureAtUnitCost"],
        item["storeId"], item["sku"]
    ))
    return {
        "asOf": end, "lookbackStart": start, "lookbackDays": lookback_days,
        "reviewDays": review_days, "maxStockDays": max_stock_days,
        "minCoveragePct": min_coverage_pct,
        "counts": counts, "filteredTotal": len(rows),
        "limit": limit, "offset": offset, "rows": rows[offset:offset + limit],
        "datasetVersion": db.scalar(
            select(DatasetState.version).where(DatasetState.id == 1)
        ) or 0,
        "method": (
            "Дневной спрос = проданные штуки / число календарных дней. "
            "Порог дефицита = leadDays + reviewDays; порог излишка = "
            "maxStockDays. Периоды с недостаточным охватом не оцениваются. "
            "Поставки учтены без дат прибытия, оценка ориентировочная, "
            "не вероятностный прогноз и не расчёт финансового убытка."
        ),
    }
