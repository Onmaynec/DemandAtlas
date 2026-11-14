"""Regression coverage for explainable BI, stock exposure and XLSX export."""
from datetime import date, timedelta
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from backend.app.db import SessionLocal
from backend.app.main import app
from backend.app.models import Inventory, Product, Source, Store
from backend.app.pipeline import ingest

client = TestClient(app)


def auth(role):
    return {"Authorization": "Bearer demo-" + role}


def setup_data():
    with SessionLocal.begin() as db:
        db.add_all([
            Store(id="S001", name="One", region="North"),
            Store(id="S002", name="Two", region="North"),
            Store(id="S999", name="Outside observer scope", region="South"),
        ])
        db.add_all([
            Product(sku="SKU1", name="Food One", category="Food",
                    unit_cost=5, min_order=1, pack_size=10, lead_days=7),
            Product(sku="SKU2", name="Electronics", category="Electronics",
                    unit_cost=20, min_order=1, pack_size=5, lead_days=7),
            Product(sku="=SUM(A1:A2)", name="Formula-like SKU", category="Food",
                    unit_cost=1, min_order=1, pack_size=1, lead_days=7),
        ])
        db.add_all([
            Inventory(store_id="S001", sku="SKU1", on_hand=1, inbound=0),
            Inventory(store_id="S001", sku="SKU2", on_hand=1000, inbound=10),
            Inventory(store_id="S002", sku="SKU1", on_hand=40, inbound=0),
            Inventory(store_id="S999", sku="SKU1", on_hand=1, inbound=0),
        ])
        db.add(Source(id="sales", kind="csv", entity="sales", config={}))
    data = []
    start = date.today() - timedelta(days=27)
    tx = 0
    for i in range(28):
        day = (start + timedelta(days=i)).isoformat()
        for store, sku, qty, price in [
            ("S001", "SKU1", 2, "10"),
            ("S001", "SKU2", 1 if i < 14 else 3, "25"),
            ("S002", "SKU1", 2, "10"),
        ]:
            tx += 1
            data.append({
                "date": day, "store_id": store, "sku": sku,
                "transaction_id": "T-" + str(tx), "line_id": "1",
                "qty": qty, "unit_price": price,
                "unit_cost": "5" if sku == "SKU1" else "20",
            })
    data.append({
        "date": date.today().isoformat(), "store_id": "S001",
        "sku": "=SUM(A1:A2)", "transaction_id": "INJECTION",
        "line_id": "1", "qty": 1, "unit_price": "15", "unit_cost": "1",
    })
    run = ingest("sales", "seed-test", data)
    assert run.status == "published"


def report_query(metric="revenue", group="sku"):
    start = (date.today() - timedelta(days=13)).isoformat()
    end = date.today().isoformat()
    return f"/api/v1/insights/contributions?start={start}&end={end}&metric={metric}&group_by={group}"


def test_kpi_contributions_reconcile_to_canonical_totals():
    setup_data()
    r = client.get(report_query("units"), headers=auth("analyst"))
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["currentTotal"] == 99  # S001: 28+42+1, S002: 28
    assert result["previousTotal"] == 70  # S001: 28+14, S002: 28
    assert result["absoluteChange"] == 29
    assert result["reconciled"] is True
    assert sum(x["absoluteChange"] for x in result["contributions"]) + result["otherChange"] == 29
    assert "не оценка причин" in result["method"]
    as_money = client.get(report_query("margin"), headers=auth("analyst"))
    assert as_money.status_code == 200
    d = as_money.json()
    assert sum(x["absoluteChange"] for x in d["contributions"]) + d["otherChange"] == d["absoluteChange"]


def test_category_and_store_scope_apply_to_every_period():
    setup_data()
    analyst = client.get(report_query("revenue", "category"), headers=auth("analyst"))
    manager = client.get(report_query("revenue", "category"), headers=auth("category"))
    assert analyst.status_code == 200 and manager.status_code == 200
    assert "Electronics" in {x["dimension"] for x in analyst.json()["contributions"]}
    assert {x["dimension"] for x in manager.json()["contributions"]} == {"Food"}
    observer = client.get(report_query("units", "store"), headers=auth("observer"))
    assert observer.status_code == 200
    assert "S999" not in {x["dimension"] for x in observer.json()["contributions"]}
    blocked = client.get(report_query()+"&store=S999", headers=auth("observer"))
    assert blocked.status_code == 403
    blocked_category = client.get(
        report_query()+"&category=Electronics", headers=auth("category"))
    assert blocked_category.status_code == 403
    assert client.get(report_query(), headers=auth("engineer")).status_code == 403
    limit = client.get(report_query("units")+"&limit=1", headers=auth("analyst")).json()
    assert limit["returnedDimensions"] == 1
    assert (limit["contributions"][0]["absoluteChange"] +
            limit["otherChange"] == limit["absoluteChange"])


def test_stock_exposure_and_incomplete_coverage_are_distinguished():
    setup_data()
    r = client.get("/api/v1/replenishment/risks?lookback_days=28",
                   headers=auth("analyst"))
    assert r.status_code == 200, r.text
    data = r.json()
    pairs = {(x["storeId"], x["sku"]): x for x in data["rows"]}
    assert pairs[("S001", "SKU1")]["status"] == "shortage"
    assert pairs[("S001", "SKU1")]["shortageUnits"] == 27
    assert pairs[("S001", "SKU2")]["status"] == "overstock"
    assert pairs[("S001", "SKU2")]["excessUnits"] > 0
    assert pairs[("S002", "SKU1")]["status"] == "balanced"
    assert pairs[("S999", "SKU1")]["status"] == "insufficient_data"
    assert data["counts"] == {
        "shortage": 1, "overstock": 1, "balanced": 1, "insufficient_data": 1,
    }
    assert "не вероятностный прогноз" in data["method"]
    limited = client.get("/api/v1/replenishment/risks?limit=1&offset=1",
                         headers=auth("analyst"))
    assert limited.status_code == 200 and len(limited.json()["rows"]) == 1
    assert limited.json()["filteredTotal"] == 4


def test_risk_scope_and_role_enforced_server_side():
    setup_data()
    r = client.get("/api/v1/replenishment/risks", headers=auth("category"))
    assert r.status_code == 200
    assert all(x["category"] == "Food" for x in r.json()["rows"])
    assert client.get("/api/v1/replenishment/risks?category=Electronics",
                      headers=auth("category")).status_code == 403
    assert client.get("/api/v1/replenishment/risks?store=S999",
                      headers=auth("observer")).status_code == 403
    assert client.get("/api/v1/replenishment/risks",
                      headers=auth("observer")).status_code == 403
    assert client.get("/api/v1/replenishment/risks").status_code == 401


def test_xlsx_export_is_scoped_and_contains_no_formula_cells():
    setup_data()
    start = (date.today() - timedelta(days=27)).isoformat()
    end = date.today().isoformat()
    url = f"/api/v1/reports/sales.xlsx?start={start}&end={end}"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth("observer")).status_code == 403
    assert client.get(url+"&category=Electronics",
                      headers=auth("category")).status_code == 403
    response = client.get(url, headers=auth("category"))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    workbook = load_workbook(BytesIO(response.content), read_only=False)
    assert workbook.sheetnames == ["Информация", "Продажи"]
    data = workbook["Продажи"]
    skus = [data.cell(r, 1).value for r in range(2, data.max_row)]
    assert "SKU1" in skus
    assert not any("SKU2" == sku for sku in skus)
    assert "'=SUM(A1:A2)" in skus
    assert all(data.cell(r, 1).data_type != "f" for r in range(2, data.max_row + 1))
    assert data.freeze_panes == "B2"
    assert workbook["Информация"]["B2"].value == 1
    assert data.cell(data.max_row, 1).value == "ИТОГО"
    assert data.cell(data.max_row, 2).value == 113
    csv_response = client.get(url.replace(".xlsx", ".csv"), headers=auth("category"))
    assert csv_response.status_code == 200
    assert "'=SUM(A1:A2)" in csv_response.text
    from urllib.parse import quote
    metadata = client.get(url+"&sku="+quote("=SUM(A1:A2)"), headers=auth("category"))
    assert metadata.status_code == 200
    sheet = load_workbook(BytesIO(metadata.content))["Информация"]
    assert sheet["B8"].data_type != "f"
    assert sheet["B8"].value == "'=SUM(A1:A2)"


def test_date_range_and_product_filter_guard():
    setup_data()
    end = date.today()
    start = (end + timedelta(days=1)).isoformat()
    invalid = client.get(
        f"/api/v1/insights/contributions?start={start}&end={end.isoformat()}",
        headers=auth("analyst"),
    )
    assert invalid.status_code == 400
    invalid_sku = client.get(
        report_query()+"&sku=DOES_NOT_EXIST", headers=auth("analyst"))
    assert invalid_sku.status_code == 404
    mismatch = client.get(report_query()+"&sku=SKU2&category=Food",
                          headers=auth("analyst"))
    assert mismatch.status_code == 400
