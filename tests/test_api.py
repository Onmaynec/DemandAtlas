from datetime import date, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend.app.db import SessionLocal
from backend.app.main import app
from backend.app.models import Forecast, Inventory, Product, Source, Store
from backend.app.pipeline import ingest

client = TestClient(app)


def auth(role):
    return {"Authorization": "Bearer demo-" + role}


def setup_data():
    with SessionLocal.begin() as db:
        db.add(Store(id="S001", name="One", region="North"))
        db.add(Store(id="S999", name="Restricted", region="South"))
        db.add(Product(sku="SKU00001", name="First", category="Food",
                       unit_cost=8, min_order=1, pack_size=10, lead_days=7))
        db.add(Inventory(store_id="S001", sku="SKU00001", on_hand=12, inbound=8))
        db.add(Source(id="demo_sales", kind="csv", entity="sales", config={}))
    rows = [
        {"date": (date.today() - timedelta(days=99-i)).isoformat(),
         "store_id": "S001", "sku": "SKU00001", "transaction_id": f"T-{i}",
         "line_id": "1", "qty": 5+i%7, "unit_price": "20", "unit_cost": "8"}
        for i in range(100)
    ]
    ingest("demo_sales", "test-data", rows)


def test_no_auth_and_scope_on_metrics():
    setup_data()
    start = (date.today()-timedelta(days=30)).isoformat()
    end = date.today().isoformat()
    url = f"/api/v1/metrics/sales?start={start}&end={end}&store=S999"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth("observer")).status_code == 403
    assert client.get(url, headers=auth("analyst")).status_code == 200
    assert client.get("/api/v1/sources", headers=auth("analyst")).status_code == 403


def test_forecast_plan_and_approval_roles():
    setup_data()
    request = {"storeId": "S001", "sku": "SKU00001", "horizon": 28, "seed": 42}
    score = client.post("/api/v1/forecasts/backtest", headers=auth("analyst"), json=request)
    assert score.status_code == 200, score.text
    assert set(score.json()["models"]) == {
        "seasonal_naive", "moving_average", "exponential_smoothing", "zero"}
    publish = client.post("/api/v1/forecasts/publish", headers=auth("analyst"), json=request)
    assert publish.status_code == 201, publish.text
    forecast_id = publish.json()["id"]
    original = None
    with SessionLocal() as db:
        original = db.get(Forecast, forecast_id).points[:]
    scenario = client.post("/api/v1/replenishment/scenario", headers=auth("analyst"),
                           json={"forecastId": forecast_id, "storeId": "S001",
                                 "sku": "SKU00001", "leadDays": 14,
                                 "reviewDays": 7, "safetyStock": 10})
    assert scenario.status_code == 200, scenario.text
    assert not scenario.json()["persisted"]
    with SessionLocal() as db:
        assert db.get(Forecast, forecast_id).points == original
    plan = client.post("/api/v1/plans", headers=auth("analyst"),
                       json={"forecastId": forecast_id, "safetyStock": 10})
    assert plan.status_code == 201, plan.text
    pid = plan.json()["id"]
    bad = client.post(f"/api/v1/plans/{pid}/approve", headers=auth("analyst"),
                      json={"expectedVersion": 1})
    assert bad.status_code == 403
    submitted = client.post(f"/api/v1/plans/{pid}/submit", headers=auth("analyst"),
                            json={"expectedVersion": 1})
    assert submitted.status_code == 200 and submitted.json()["version"] == 2
    stale = client.post(f"/api/v1/plans/{pid}/approve", headers=auth("director"),
                        json={"expectedVersion": 1})
    assert stale.status_code == 409
    approved = client.post(f"/api/v1/plans/{pid}/approve", headers=auth("director"),
                           json={"expectedVersion": 2})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    repeat = client.post(f"/api/v1/plans/{pid}/approve", headers=auth("director"),
                         json={"expectedVersion": 3})
    assert repeat.status_code == 409


def test_lineage_and_csv_role_protection():
    setup_data()
    day = date.today().isoformat()
    response = client.get(f"/api/v1/lineage?day={day}&store=S001&sku=SKU00001",
                          headers=auth("analyst"))
    assert response.status_code == 200, response.text
    assert response.json()["sourceBatches"]
    uri = f"/api/v1/reports/sales.csv?start={day}&end={day}"
    assert client.get(uri, headers=auth("observer")).status_code == 403
    assert client.get(uri, headers=auth("analyst")).status_code == 200
