"""Validate new analytics APIs on the running PostgreSQL Compose stack."""
from datetime import date, timedelta
from io import BytesIO
from zipfile import is_zipfile

from fastapi.testclient import TestClient

from backend.app.main import app


def run():
    client = TestClient(app)
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=27)).isoformat()
    analyst = {"Authorization": "Bearer demo-analyst"}
    engineer = {"Authorization": "Bearer demo-engineer"}
    observer = {"Authorization": "Bearer demo-observer"}
    url = f"/api/v1/insights/contributions?start={start}&end={end}&group_by=sku"
    compare = client.get(url, headers=analyst)
    assert compare.status_code == 200, compare.text
    assert compare.json()["reconciled"]
    forbidden = client.get(url, headers=engineer)
    assert forbidden.status_code == 403
    risk = client.get("/api/v1/replenishment/risks?store=S001",
                      headers=analyst)
    assert risk.status_code == 200, risk.text
    assert "insufficient_data" in risk.json()["counts"]
    assert client.get("/api/v1/replenishment/risks",
                      headers=observer).status_code == 403
    workbook = client.get(
        f"/api/v1/reports/sales.xlsx?start={start}&end={end}",
        headers=analyst,
    )
    assert workbook.status_code == 200, workbook.text
    assert is_zipfile(BytesIO(workbook.content))
    assert client.get(
        f"/api/v1/reports/sales.xlsx?start={start}&end={end}",
        headers=observer,
    ).status_code == 403
    print("Insights and XLSX smoke passed on PostgreSQL")


if __name__ == "__main__":
    run()
