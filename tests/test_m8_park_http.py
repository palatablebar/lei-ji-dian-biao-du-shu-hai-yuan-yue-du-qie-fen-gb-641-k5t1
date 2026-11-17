"""M8 HTTP 接口与页面：总分账 API、归档接口、两份 CSV 下载与 404。

依赖 fastapi TestClient；在没有 fastapi 的离线环境整体跳过。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.m8_park import service as m8_service  # noqa: F401

pytest.importorskip("fastapi")

PARK_SAMPLE = "readings_park.csv"


def test_park_api_and_pages_over_http(client_with_run):
    client_with_run.post("/api/runs", json={"sample": PARK_SAMPLE})
    payload = client_with_run.get("/api/park/reconcile").json()
    assert payload["enabled"] is True
    assert len(payload["months"]) == 3
    assert payload["stats"]["all_conserved"] is True

    page = client_with_run.get("/park")
    assert page.status_code == 200
    assert "待查缺口" in page.text

    archive = client_with_run.post("/api/park/archive", json={}).json()
    assert archive["created"] == 3
    again = client_with_run.post("/api/park/archive", json={}).json()
    assert again["reused"] == 3 and again["created"] == 0

    monthly = client_with_run.get("/api/export/park-monthly.csv")
    assert monthly.status_code == 200
    assert monthly.text.startswith("﻿")
    tenants = client_with_run.get("/api/export/park-tenants.csv")
    assert tenants.status_code == 200
    assert "漏抄无账单" in tenants.text


def test_park_csv_keeps_gap_separate(client_with_run):
    client_with_run.post("/api/runs", json={"sample": PARK_SAMPLE})
    body = client_with_run.get("/api/export/park-monthly.csv").text
    lines = body.splitlines()
    assert lines[0].lstrip("﻿").startswith("月份,总表号,总表用量kWh")
    gap_line = next(line for line in lines if line.startswith("2026-02"))
    cells = gap_line.split(",")
    # 列序：月份,总表号,总表,可证实,可证实费用,缺口,缺口费用,状态,守恒,归因
    assert cells[5] == "250.000"          # 待查缺口单列
    assert cells[6] == "0.00"             # 缺口费用恒为 0
    assert "漏抄" in cells[9]


def test_park_tenant_csv_separates_verified_and_allocated(client_with_run):
    client_with_run.post("/api/runs", json={"sample": PARK_SAMPLE})
    body = client_with_run.get("/api/export/park-tenants.csv").text
    lines = body.splitlines()
    assert lines[0].lstrip("﻿").startswith("月份,分表号,租户,可证实用量kWh")
    # 漏抄的 T01 二月：可证实为 0，但分摊口径保留跨月摊入量
    t01 = next(line for line in lines if line.startswith("2026-02,T01"))
    cells = t01.split(",")
    assert cells[3] == "0.000"
    assert Decimal(cells[4]) > 0
    assert cells[5] == "0.00"
    assert "漏抄无账单" in cells[9]


def test_park_disabled_sample_returns_404_on_export(client):
    client.post("/api/runs", json={"sample": "readings_clean.csv"})
    response = client.get("/api/export/park-monthly.csv")
    assert response.status_code == 404
    assert response.json()["error"] == "E_BATCH_NOT_FOUND"


def test_park_page_renders_without_batch(client):
    response = client.get("/park")
    assert response.status_code == 200
    assert "empty-line" in response.text
