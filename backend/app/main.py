import csv
import io
from decimal import Decimal
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .auth import Principal, can_access, current_user, require_scope, roles
from .db import get_db
from .domain import METRICS
from .models import (
    DailyCoverage, DailySalesMart, DatasetState, PipelineRun, Product,
    QualityFinding, Source, StagedSale, Store
)
from .pipeline import fetch_sql, ingest, parse_csv
from .decisions import router as decisions_router
from .reprocessing import router as reprocessing_router
from .insights import router as insights_router

app = FastAPI(title="DemandAtlas API", version="0.3.0", openapi_url="/openapi.json")
requests = defaultdict(list)


@app.middleware("http")
async def middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", "")[:80] or str(uuid4())
    request.state.request_id = request_id
    # Bearer-only API: no cookie authentication / CSRF state.
    if request.url.path.startswith("/api/"):
        identity = request.headers.get("Authorization", "anonymous")
        now = time.monotonic()
        bucket = requests[identity]
        requests[identity] = [point for point in bucket if now - point < 60]
        if len(requests[identity]) >= int(os.getenv("RATE_LIMIT_PER_MINUTE", "240")):
            return JSONResponse({"code": "rate_limited", "message": "Too many requests",
                                 "requestId": request_id, "details": {}},
                                status_code=429, headers={"X-Request-ID": request_id})
        requests[identity].append(now)
    try:
        response = await call_next(request)
    except Exception:
        raise
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"code": "http_" + str(exc.status_code), "message": str(exc.detail),
                         "requestId": request.state.request_id, "details": {}},
                        status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse({"code": "validation_error", "message": "Invalid request",
                         "requestId": request.state.request_id,
                         "details": {"errors": [{"loc": list(e["loc"]), "msg": e["msg"]}
                                                for e in exc.errors()]}}, status_code=400)


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    import logging
    logging.exception("request_failed requestId=%s", request.state.request_id)
    return JSONResponse({"code": "internal_error", "message": "Internal server error",
                         "requestId": request.state.request_id, "details": {}}, status_code=500)


class SourceInput(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: str = Field(pattern="^(csv|sql)$")
    entity: str = Field(pattern="^(sales|stock)$")
    config: dict = Field(default_factory=dict)
    schedule_minutes: int | None = Field(default=None, ge=5, le=10080)


class BatchInput(BaseModel):
    idempotencyKey: str = Field(min_length=1, max_length=128)
    rows: list[dict] | None = None
    csv: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "service": "api"}


@app.get("/api/v1/auth/me")
def me(user: Principal = Depends(current_user)):
    return {"name": user.name, "role": user.role,
            "stores": user.stores, "categories": user.categories}


@app.get("/api/v1/catalog")
def catalog(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    stores = db.scalars(select(Store)).all()
    products = db.scalars(select(Product)).all()
    return {
        "stores": [{"id": s.id, "name": s.name, "region": s.region}
                   for s in stores if not user.stores or s.id in user.stores],
        "products": [{"sku": p.sku, "name": p.name, "category": p.category}
                     for p in products if not user.categories or p.category in user.categories],
    }


@app.post("/api/v1/sources", status_code=201)
def source_create(body: SourceInput, user: Principal = Depends(roles("data_engineer")),
                  db: Session = Depends(get_db)):
    if db.get(Source, body.id):
        raise HTTPException(409, "Source already exists")
    if body.kind == "sql":
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", str(body.config.get("table", ""))):
            raise HTTPException(400, "SQL source requires a safe table name")
    elif body.config or body.schedule_minutes:
        raise HTTPException(400, "CSV sources require manual uploads and no connection configuration")
    source = Source(id=body.id, kind=body.kind, entity=body.entity, config=body.config,
                    schedule_minutes=body.schedule_minutes,
                    next_run_at=(datetime.now(timezone.utc) + timedelta(minutes=body.schedule_minutes)
                                 if body.schedule_minutes else None))
    db.add(source)
    db.commit()
    return {"id": source.id, "kind": source.kind, "entity": source.entity}


@app.get("/api/v1/sources")
def source_list(user: Principal = Depends(roles("data_engineer")), db: Session = Depends(get_db)):
    return [{"id": s.id, "kind": s.kind, "entity": s.entity,
             "schedule_minutes": s.schedule_minutes, "next_run_at": s.next_run_at}
            for s in db.scalars(select(Source))]


@app.post("/api/v1/pipelines/{source_id}/runs")
def run_pipeline(source_id: str, body: BatchInput,
                 user: Principal = Depends(roles("data_engineer")), db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    db.close()
    try:
        if source.kind == "sql":
            if body.rows is not None or body.csv is not None:
                raise HTTPException(400, "SQL connector does not accept client-supplied rows")
            rows = fetch_sql(source)
        else:
            if (body.csv is None) == (body.rows is None):
                raise HTTPException(400, "Provide exactly one of rows or csv")
            rows = body.rows if body.rows is not None else parse_csv(body.csv)
        run = ingest(source_id, body.idempotencyKey, rows)
        return {"runId": run.id, "status": run.status, "checksum": run.checksum,
                "rowsReceived": run.rows_received, "rowsAccepted": run.rows_accepted,
                "rowsRejected": run.rows_rejected, "datasetVersion": run.dataset_version}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/quality/runs/{run_id}")
def run_quality(run_id: str, user: Principal = Depends(roles("data_engineer", "analyst")),
                db: Session = Depends(get_db)):
    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    findings = db.scalars(select(QualityFinding).where(QualityFinding.run_id == run.id)).all()
    return {"runId": run.id, "sourceId": run.source_id, "status": run.status,
            "checksum": run.checksum, "schemaVersion": run.schema_version,
            "datasetVersion": run.dataset_version, "rowsReceived": run.rows_received,
            "rowsAccepted": run.rows_accepted, "rowsRejected": run.rows_rejected,
            "startedAt": run.started_at, "finishedAt": run.finished_at,
            "error": run.error, "mode": run.mode, "scope": run.scope,
            "findings": [{"row": f.row_number, "rule": f.rule, "severity": f.severity,
                          "message": f.message} for f in findings]}


@app.get("/api/v1/quality/runs")
def runs_list(user: Principal = Depends(roles("data_engineer", "analyst")),
              db: Session = Depends(get_db)):
    runs = db.scalars(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(50)).all()
    return [{"id": r.id, "source": r.source_id, "status": r.status,
             "received": r.rows_received, "rejected": r.rows_rejected,
             "version": r.dataset_version, "mode": r.mode} for r in runs]


@app.get("/api/v1/datasets/current")
def current_dataset(user: Principal = Depends(current_user),
                    db: Session = Depends(get_db)):
    # No business data exposed to an engineer, but the version is required
    # to perform optimistic-concurrency-safe reprocessing.
    version = db.scalar(select(DatasetState.version).where(DatasetState.id == 1)) or 0
    last = db.scalar(select(func.max(PipelineRun.finished_at)).where(
        PipelineRun.status == "published"
    ))
    return {"version": version, "lastPublishedAt": last}


@app.get("/api/v1/metrics/catalog")
def metrics_catalog(user: Principal = Depends(current_user)):
    return METRICS


def sales_query(db: Session, user: Principal, start: date, end: date,
                store: str | None, region: str | None,
                category: str | None, sku: str | None, group_by: str,
                row_limit: int = 2000):
    if store and user.stores and store not in user.stores:
        raise HTTPException(403, "Store outside permitted scope")
    if category and user.categories and category not in user.categories:
        raise HTTPException(403, "Category outside permitted scope")
    if sku:
        product = db.get(Product, sku)
        if product is None:
            raise HTTPException(404, "Unknown SKU")
        require_scope(user, category=product.category)
        if category and product.category != category:
            raise HTTPException(400, "SKU and category filters conflict")
    dim = {
        "date": DailySalesMart.date, "store": DailySalesMart.store_id,
        "sku": DailySalesMart.sku, "category": Product.category,
        "region": Store.region,
    }.get(group_by)
    if dim is None:
        raise HTTPException(400, "Unknown grouping")
    stmt = select(
        dim.label("dimension"), func.sum(DailySalesMart.units).label("units"),
        func.sum(DailySalesMart.revenue).label("revenue"),
        func.sum(DailySalesMart.cost).label("cost"),
    ).join(Product, Product.sku == DailySalesMart.sku).join(
        Store, Store.id == DailySalesMart.store_id
    ).where(DailySalesMart.date >= start, DailySalesMart.date <= end)
    if store:
        stmt = stmt.where(DailySalesMart.store_id == store)
    if sku:
        stmt = stmt.where(DailySalesMart.sku == sku)
    if category:
        stmt = stmt.where(Product.category == category)
    if region:
        stmt = stmt.where(Store.region == region)
    if user.stores:
        stmt = stmt.where(DailySalesMart.store_id.in_(user.stores))
    if user.categories:
        stmt = stmt.where(Product.category.in_(user.categories))
    return db.execute(stmt.group_by(dim).order_by(dim).limit(row_limit)).all()


def coverage(db: Session, user: Principal, start: date, end: date,
             store: str | None, region: str | None):
    stores_stmt = select(Store.id)
    if store:
        stores_stmt = stores_stmt.where(Store.id == store)
    if region:
        stores_stmt = stores_stmt.where(Store.region == region)
    if user.stores:
        stores_stmt = stores_stmt.where(Store.id.in_(user.stores))
    ids = list(db.scalars(stores_stmt))
    expected = len(ids) * ((end - start).days + 1)
    observed = 0
    if ids:
        observed = db.scalar(select(func.count()).select_from(DailyCoverage).where(
            DailyCoverage.store_id.in_(ids), DailyCoverage.date >= start,
            DailyCoverage.date <= end
        )) or 0
    latest = db.scalar(select(func.max(PipelineRun.finished_at)).where(
        PipelineRun.status == "published"
    ))
    return {"expectedStoreDays": expected, "observedStoreDays": observed,
            "storeCoveragePct": round(100 * observed / expected, 2) if expected else None,
            "incomplete": observed < expected, "lastPublishedAt": latest}


@app.get("/api/v1/metrics/coverage")
def coverage_detail(
    start: date, end: date, store: str | None = None,
    region: str | None = None,
    user: Principal = Depends(current_user), db: Session = Depends(get_db)
):
    if user.role == "data_engineer":
        raise HTTPException(403, "Business coverage not permitted")
    if end < start or (end - start).days > 92:
        raise HTTPException(400, "Coverage range must be between 1 and 93 days")
    if store and user.stores and store not in user.stores:
        raise HTTPException(403, "Store outside permitted scope")
    stmt = select(Store.id)
    if store:
        stmt = stmt.where(Store.id == store)
    if region:
        stmt = stmt.where(Store.region == region)
    if user.stores:
        stmt = stmt.where(Store.id.in_(user.stores))
    permitted = list(db.scalars(stmt.order_by(Store.id)))
    observed = set(db.execute(select(DailyCoverage.date, DailyCoverage.store_id).where(
        DailyCoverage.date.between(start, end), DailyCoverage.store_id.in_(permitted)
    )).all()) if permitted else set()
    missing = []
    for shop in permitted:
        day = start
        while day <= end:
            if (day, shop) not in observed:
                missing.append({"storeId": shop, "date": day.isoformat()})
            day += timedelta(days=1)
    total = len(permitted) * ((end-start).days+1)
    return {"summary": {
        "expectedStoreDays": total, "observedStoreDays": total - len(missing),
        "coveragePct": round(100*(total-len(missing))/total, 2) if total else None,
        "incomplete": bool(missing)
    }, "missing": missing}


@app.get("/api/v1/metrics/sales")
def metrics_sales(
    start: date, end: date, store: str | None = None, region: str | None = None,
    category: str | None = None, sku: str | None = None, group_by: str = "date",
    user: Principal = Depends(current_user), db: Session = Depends(get_db)
):
    if end < start or (end - start).days > 730:
        raise HTTPException(400, "Date range must be 0–730 days")
    if user.role == "data_engineer":
        raise HTTPException(403, "Data engineers cannot view business reporting")
    values = sales_query(db, user, start, end, store, region, category, sku, group_by)
    rows = []
    for v in values:
        rows.append({"dimension": str(v.dimension), "units": int(v.units),
                     "revenue": float(v.revenue), "cost": float(v.cost),
                     "margin": float(v.revenue - v.cost)})
    return {"groupBy": group_by, "rows": rows, "coverage": coverage(db, user, start, end, store, region),
            "metrics": METRICS, "datasetVersion": db.scalar(select(DatasetState.version).where(DatasetState.id == 1)) or 0}


@app.get("/api/v1/metrics/compare")
def metrics_compare(
    start: date, end: date, group_by: str = "store",
    store: str | None = None, category: str | None = None,
    user: Principal = Depends(current_user), db: Session = Depends(get_db)
):
    if user.role == "data_engineer":
        raise HTTPException(403, "Not permitted")
    if end < start or (end - start).days > 365:
        raise HTTPException(400, "Invalid comparison window")
    delta = end - start + timedelta(days=1)
    current = sales_query(db, user, start, end, store, None, category, None, group_by)
    previous = sales_query(db, user, start - delta, start - timedelta(days=1),
                           store, None, category, None, group_by)
    c = {str(v.dimension): float(v.revenue) for v in current}
    p = {str(v.dimension): float(v.revenue) for v in previous}
    total_c, total_p = sum(c.values()), sum(p.values())
    diff = total_c - total_p
    return {
        "current": total_c, "previous": total_p, "absoluteChange": round(diff, 2),
        "percentChange": round(diff / total_p * 100, 2) if total_p else None,
        "contributions": [
            {"dimension": k, "current": c.get(k, 0), "previous": p.get(k, 0),
             "absoluteChange": round(c.get(k, 0) - p.get(k, 0), 2),
             "shareOfChangePct": round((c.get(k, 0) - p.get(k, 0)) / diff * 100, 2) if diff else None}
            for k in sorted(set(c) | set(p))
        ],
        "coverage": coverage(db, user, start, end, store, None),
    }


@app.get("/api/v1/lineage")
def lineage(day: date, store: str, sku: str, user: Principal = Depends(current_user),
            db: Session = Depends(get_db)):
    if user.role == "data_engineer":
        raise HTTPException(403, "Not permitted")
    product = db.get(Product, sku)
    if not product:
        raise HTTPException(404, "SKU not found")
    require_scope(user, store, product.category)
    mart = db.scalar(select(DailySalesMart).where(
        DailySalesMart.date == day, DailySalesMart.store_id == store, DailySalesMart.sku == sku
    ))
    if not mart:
        raise HTTPException(404, "No mart row for requested dimensions")
    source_runs = db.execute(select(
        StagedSale.run_id, StagedSale.source_id, func.count().label("rows")
    ).where(StagedSale.date == day, StagedSale.store_id == store,
            StagedSale.sku == sku).group_by(StagedSale.run_id, StagedSale.source_id)).all()
    return {"metric": "revenue", "date": day, "store": store, "sku": sku,
            "formula": METRICS["revenue"]["formula"], "martId": mart.id,
            "datasetVersion": mart.dataset_version, "revenue": float(mart.revenue),
            "sourceBatches": [
                {"runId": item.run_id, "sourceId": item.source_id, "rows": item.rows,
                 "checksum": db.get(PipelineRun, item.run_id).checksum}
                for item in source_runs
            ]}


def report_rows(db: Session, user: Principal, start: date, end: date,
                store: str | None, region: str | None,
                category: str | None, sku: str | None):
    if end < start or (end - start).days > 730:
        raise HTTPException(400, "Report date range must be 1–731 days")
    rows = sales_query(db, user, start, end, store, region, category, sku,
                       "sku", row_limit=10001)
    if len(rows) > 10000:
        raise HTTPException(413, "Report has over 10,000 SKUs; narrow filters")
    return rows


def safe_spreadsheet_text(value: str) -> str:
    # Excel and LibreOffice interpret the first non-whitespace character as
    # an expression starter, even when the CSV is generated by a trusted app.
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@app.get("/api/v1/reports/sales.csv")
def export_sales(
    start: date, end: date, store: str | None = None, category: str | None = None,
    region: str | None = None, sku: str | None = None,
    user: Principal = Depends(roles("analyst", "category_manager", "director")),
    db: Session = Depends(get_db),
):
    data = report_rows(db, user, start, end, store, region, category, sku)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["sku", "units", "revenue", "cost", "margin"])
    for value in data:
        writer.writerow([
            safe_spreadsheet_text(value.dimension), int(value.units),
            str(value.revenue), str(value.cost),
            str(value.revenue - value.cost)
        ])
    return Response(
        content=output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="demandatlas-sales.csv"'}
    )


@app.get("/api/v1/reports/sales.xlsx")
def export_sales_xlsx(
    start: date, end: date, store: str | None = None, category: str | None = None,
    region: str | None = None, sku: str | None = None,
    user: Principal = Depends(roles("analyst", "category_manager", "director")),
    db: Session = Depends(get_db),
):
    data = report_rows(db, user, start, end, store, region, category, sku)
    version = db.scalar(
        select(DatasetState.version).where(DatasetState.id == 1)
    ) or 0
    coverage_info = coverage(db, user, start, end, store, region)
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Информация"
    summary.sheet_view.showGridLines = False
    summary.append(["DemandAtlas — отчёт по продажам", ""])
    pairs = [
        ("Набор данных / версия", version),
        ("Период с", start.isoformat()),
        ("Период по", end.isoformat()),
        ("Магазин", safe_spreadsheet_text(store or "Все доступные")),
        ("Регион", safe_spreadsheet_text(region or "Все доступные")),
        ("Категория", safe_spreadsheet_text(category or "Все доступные")),
        ("SKU", safe_spreadsheet_text(sku or "Все доступные")),
        ("Роль", user.role),
        ("Охват магазин-дней, %", coverage_info["storeCoveragePct"]),
        ("Охват полный", not coverage_info["incomplete"]),
        ("Строк SKU", len(data)),
        ("Формула выручки", "SUM(qty * unit_price)"),
        ("Формула маржи", "SUM(revenue - cost)"),
        ("Источник", "daily_sales_mart"),
    ]
    for item in pairs:
        summary.append(list(item))
    summary.column_dimensions["A"].width = 31
    summary.column_dimensions["B"].width = 55
    summary["A1"].font = Font(bold=True, size=16, color="166C69")
    summary.freeze_panes = "A2"

    sheet = workbook.create_sheet("Продажи")
    sheet.sheet_view.showGridLines = False
    sheet.append(["SKU", "Штук", "Выручка", "Себестоимость", "Маржа"])
    for value in data:
        sku_text = safe_spreadsheet_text(value.dimension)
        row = [
            sku_text, int(value.units), float(value.revenue),
            float(value.cost), float(value.revenue - value.cost)
        ]
        sheet.append(row)
        # Do not let openpyxl convert a potentially dangerous SKU to formula.
        sheet.cell(sheet.max_row, 1).data_type = "s"
    for col in sheet[1]:
        col.fill = PatternFill("solid", fgColor="19242B")
        col.font = Font(bold=True, color="EAF4F3")
        col.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 27
    for letter, width in [("A", 26), ("B", 15), ("C", 22),
                          ("D", 22), ("E", 22)]:
        sheet.column_dimensions[letter].width = width
    for row in sheet.iter_rows(min_row=2, min_col=2, max_col=5):
        row[0].number_format = "#,##0"
        for cell in row[1:]:
            cell.number_format = '#,##0.00;[Red](#,##0.00)'
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    # Include a numeric totals row, never an executable Excel formula.
    sheet.append([
        "ИТОГО", sum(int(x.units) for x in data),
        float(sum((x.revenue for x in data), Decimal(0))),
        float(sum((x.cost for x in data), Decimal(0))),
        float(sum((x.revenue - x.cost for x in data), Decimal(0))),
    ])
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True, color="166C69")
    sheet.cell(sheet.max_row, 1).data_type = "s"
    for cell in sheet[sheet.max_row][2:]:
        cell.number_format = '#,##0.00;[Red](#,##0.00)'
    output = io.BytesIO()
    workbook.save(output)
    return Response(
        content=output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="demandatlas-sales.xlsx"'},
    )


app.include_router(decisions_router)
app.include_router(reprocessing_router)
app.include_router(insights_router)

@app.get("/")
def index():
    return FileResponse("web/index.html", media_type="text/html")
