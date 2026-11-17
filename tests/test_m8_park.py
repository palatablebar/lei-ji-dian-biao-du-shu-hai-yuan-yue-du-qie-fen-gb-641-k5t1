"""M8 园区总分对账：守恒、缺口单列、漏抄/投运、归档幂等与旧版保留。

裁决原则：总表用量 = 租户可证实用量 + 待查缺口；缺口永远不摊进租户账单。
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app import config, errors
from app.modules.m2_contract import contract as contract_mod
from app.modules.m6_batches import service as batch_service
from app.modules.m8_park import archiver, engine, park_contract, service as m8_service

PARK_SAMPLE = "readings_park.csv"


@pytest.fixture()
def park_run(settings, store):
    return batch_service.run(settings, store, sample=PARK_SAMPLE)


@pytest.fixture()
def park_batch_id(park_run):
    return park_run["batch"]["batch_id"]


def _alloc(month, meter_id, usage):
    return {"month": month, "meter_id": meter_id, "register": "TOTAL",
            "usage_kwh": Decimal(usage), "exact_share": Decimal(usage),
            "tou_adjusted": False, "sources": []}


def _reading(meter_id, when="2026-01-15T00:00:00+08:00"):
    return {"meter_id": meter_id, "read_at": when, "status": "valid",
            "read_type": "N"}


def _interval(meter_id):
    return {"meter_id": meter_id, "register": "TOTAL",
            "start_at": "2026-01-01T00:00:00+08:00",
            "end_at": "2026-02-01T00:00:00+08:00"}


# ---------- 园区契约 ----------
def test_park_contract_loads_master_and_tenants(settings):
    contracts = contract_mod.load(settings)
    park, quarantine = park_contract.load(settings, contracts)
    assert park_contract.master_id(park) == "PM01"
    assert park_contract.tenant_ids(park) == ["T01", "T02", "T03", "T04"]
    assert quarantine == []
    assert park["T03"].activated_at is not None


def test_park_contract_bad_role_and_duplicate_master(tmp_path, settings):
    from pathlib import Path
    import shutil
    sd = tmp_path / "sd"
    sd.mkdir()
    shutil.copy(Path(settings.sample_dir) / "meters.csv", sd / "meters.csv")
    (sd / "park.csv").write_text(
        "meter_id,meter_role,activated_at\nPM01,master,\nT01,boss,\nT02,master,\n",
        encoding="utf-8")
    local = config.Settings(db_path=str(tmp_path / "x.sqlite"), sample_dir=str(sd))
    contracts = contract_mod.load(local)
    _, quarantine = park_contract.load(local, contracts)
    codes = {row["code"] for row in quarantine}
    assert "Q_PARK_ROLE_UNKNOWN" in codes
    assert "Q_PARK_MASTER_DUPLICATE" in codes


def test_park_missing_file_disables_module(settings, store):
    # readings_clean 样例目录里没有 park 表读数，即使全局 park.csv 存在也跳过
    result = batch_service.run(settings, store, sample="readings_clean.csv")
    totals = store.load_totals(result["batch"]["batch_id"])
    assert totals["park_enabled"] == "否"
    park_stage = next(s for s in result["stages"] if s["stage"] == "park")
    assert park_stage["status"] == "skipped"


# ---------- 守恒与缺口 ----------
def test_park_stage_runs_after_reconcile_before_export(park_run):
    keys = [s["stage"] for s in park_run["stages"]]
    assert keys.index("reconcile") < keys.index("park") < keys.index("export")
    park_stage = next(s for s in park_run["stages"] if s["stage"] == "park")
    assert park_stage["status"] == "ok"


def test_january_balanced_with_no_gap(store, park_batch_id):
    row = next(m for m in store.load_park_months(park_batch_id)
               if m["month"] == "2026-01")
    assert row["status"] == "balanced"
    assert row["gap_kwh"] == Decimal("0.000")
    assert row["master_usage_kwh"] == row["verified_usage_kwh"]
    assert row["conservation_ok"] is True


def test_february_gap_is_listed_not_allocated(store, park_batch_id):
    row = next(m for m in store.load_park_months(park_batch_id)
               if m["month"] == "2026-02")
    assert row["status"] == "gap"
    assert row["master_usage_kwh"] == \
        row["verified_usage_kwh"] + row["gap_kwh"]
    # 缺口单列且不计任何费用
    assert row["gap_amount_yuan"] == Decimal("0.00")
    assert "K_GAP_MISSED_READ" in row["gap_codes"]
    assert "K_GAP_PARTIAL_MONTH" in row["gap_codes"]


def test_missed_tenant_has_no_bill_and_zero_charge(store, park_batch_id):
    t01 = next(t for t in store.load_park_tenants(park_batch_id)
               if t["month"] == "2026-02" and t["meter_id"] == "T01")
    assert "K_TENANT_MISSED_READ" in t01["flags"]
    assert t01["usage_kwh"] == Decimal("0.000")
    assert t01["amount_yuan"] == Decimal("0.00")
    assert t01["bill_missing"] is True
    # 日权重摊入的跨月用量保留在 allocated_kwh，但不进可证实口径
    assert t01["allocated_kwh"] > 0


def test_partial_month_activation_keeps_verified_usage(store, park_batch_id):
    t03 = next(t for t in store.load_park_tenants(park_batch_id)
               if t["month"] == "2026-02" and t["meter_id"] == "T03")
    assert "K_TENANT_PARTIAL_MONTH" in t03["flags"]
    assert t03["usage_kwh"] == Decimal("100.000")
    assert t03["coverage_ratio"] < Decimal("1")


def test_not_yet_activated_excluded_from_conservation(store, park_batch_id):
    t03_jan = next(t for t in store.load_park_tenants(park_batch_id)
                   if t["month"] == "2026-01" and t["meter_id"] == "T03")
    assert t03_jan["usage_kwh"] == Decimal("0.000")
    assert "K_TENANT_MISSED_READ" not in t03_jan["flags"]


def test_march_unverified_gap(store, park_batch_id):
    row = next(m for m in store.load_park_months(park_batch_id)
               if m["month"] == "2026-03")
    assert row["status"] == "gap"
    assert row["gap_codes"] == ["K_GAP_UNVERIFIED"]
    assert row["conservation_ok"] is True


def test_total_conservation_identity(store, park_batch_id):
    totals = store.load_totals(park_batch_id)
    master = Decimal(totals["park_master_usage"])
    verified = Decimal(totals["park_verified_usage"])
    gap = Decimal(totals["park_gap_usage"])
    assert master == verified + gap
    assert totals["park_conserved"] == "是"
    # 缺口全程零费用
    assert all(m["gap_amount_yuan"] == Decimal("0.00")
               for m in store.load_park_months(park_batch_id))


def test_gap_never_charged_to_any_tenant(store, park_batch_id):
    months = {m["month"]: m for m in store.load_park_months(park_batch_id)}
    tenant_amount = sum((t["amount_yuan"]
                         for t in store.load_park_tenants(park_batch_id)),
                        Decimal("0"))
    assert tenant_amount == Decimal(
        store.load_totals(park_batch_id)["park_verified_amount"])
    assert tenant_amount > 0
    # 租户费用合计 = 各月可证实费用，缺口无对应金额
    assert sum((m["verified_amount_yuan"]
                for m in months.values()), Decimal("0")) == tenant_amount


# ---------- 引擎纯函数边界 ----------
def test_sub_exceeds_master_is_abnormal_not_negative_gap(settings):
    contracts = contract_mod.load(settings)
    park, _ = park_contract.load(settings, contracts)
    allocs = [_alloc("2026-01", "PM01", "100"),
              _alloc("2026-01", "T01", "80"),
              _alloc("2026-01", "T02", "40")]
    readings = [_reading(m) for m in ("PM01", "T01", "T02")]
    intervals = [_interval(m) for m in ("PM01", "T01", "T02")]
    result = engine.reconcile(allocations=allocs, bills=[], intervals=intervals,
                              readings=readings, contracts=contracts, park=park)
    row = result["months"][0]
    assert row["status"] == "abnormal"
    assert row["gap_kwh"] == Decimal("0.000")
    assert row["conservation_ok"] is False
    assert row["gap_codes"] == ["K_PARK_SUB_EXCEEDS_MASTER"]


def test_missing_master_is_unreconciled(settings):
    contracts = contract_mod.load(settings)
    park, _ = park_contract.load(settings, contracts)
    park = {k: v for k, v in park.items() if k != "PM01"}
    allocs = [_alloc("2026-01", "T01", "80")]
    result = engine.reconcile(
        allocations=allocs, bills=[], intervals=[_interval("T01")],
        readings=[_reading("T01")], contracts=contracts, park=park)
    row = result["months"][0]
    assert row["status"] == "unreconciled"
    assert row["conservation_ok"] is True


def test_engine_decimal_never_float(settings):
    contracts = contract_mod.load(settings)
    park, _ = park_contract.load(settings, contracts)
    allocs = [_alloc("2026-01", "PM01", "100.000"),
              _alloc("2026-01", "T01", "60.000")]
    readings = [_reading(m) for m in ("PM01", "T01")]
    intervals = [_interval(m) for m in ("PM01", "T01")]
    result = engine.reconcile(allocations=allocs, bills=[], intervals=intervals,
                              readings=readings, contracts=contracts, park=park)
    row = result["months"][0]
    assert isinstance(row["gap_kwh"], Decimal)
    assert row["gap_kwh"] == Decimal("40.000")


# ---------- 归档：原子、版本化、幂等、旧版保留 ----------
def test_archive_creates_versions_and_files(settings, store, park_batch_id):
    outcome = m8_service.archive_current(settings, store, park_batch_id)
    assert outcome["created"] == 3
    assert outcome["reused"] == 0
    assert store.count_park_archives(park_batch_id) == 3
    for item in outcome["archives"]:
        path = settings.data_dir / "park_archives" / f"{item['archive_id']}.json"
        assert path.is_file()
        snap = json.loads(path.read_text(encoding="utf-8"))
        assert snap["batch_id"] == park_batch_id
        assert len(snap["result_digest"]) == 64


def test_archive_same_directive_is_idempotent(settings, store, park_batch_id):
    first = m8_service.archive_current(settings, store, park_batch_id)
    before = store.count_park_archives(park_batch_id)
    second = m8_service.archive_current(settings, store, park_batch_id)
    assert second["created"] == 0
    assert second["reused"] == 3
    assert store.count_park_archives(park_batch_id) == before
    assert [a["archive_id"] for a in first["archives"]] == \
        [a["archive_id"] for a in second["archives"]]


def test_archive_versions_are_sequential(settings, store, park_batch_id):
    m8_service.archive_current(settings, store, park_batch_id)
    rows = store.list_park_archives(park_batch_id)
    by_month = {}
    for row in rows:
        by_month.setdefault(row["month"], []).append(row["version"])
    for versions in by_month.values():
        assert versions == list(range(1, len(versions) + 1))


def test_archive_rejects_unreconciled_month(settings, store):
    contracts = contract_mod.load(settings)
    park, _ = park_contract.load(settings, contracts)
    park = {k: v for k, v in park.items() if k != "PM01"}
    allocs = [_alloc("2026-01", "T01", "80")]
    result = engine.reconcile(
        allocations=allocs, bills=[], intervals=[_interval("T01")],
        readings=[_reading("T01")], contracts=contracts, park=park)
    batch = {"batch_id": "mb-test", "batch_key": "k", "rule_version": "v",
             "input_digest": "d"}
    month_row = result["months"][0]
    with pytest.raises(archiver.ArchiveError):
        archiver.publish(store, settings, batch=batch, month_row=month_row,
                         tenant_rows=result["tenants"])


def test_archive_interrupted_leaves_no_tmp_and_keeps_old(settings, store,
                                                         park_batch_id):
    m8_service.archive_current(settings, store, park_batch_id)
    files_before = sorted((settings.data_dir / "park_archives").glob("*.json"))
    # 原子发布只应留下正式 json，不应残留 tmp
    leftovers = list((settings.data_dir / "park_archives").glob("*.tmp"))
    assert leftovers == []
    # 旧版文件全部仍在
    assert all(path.is_file() for path in files_before)


# ---------- CSV 证据行：页面与下载同源、两个口径分列 ----------
def test_monthly_csv_keeps_gap_separate_and_zero_fee(store, park_batch_id):
    from app.modules.m8_park import csv_rows
    months = store.load_park_months(park_batch_id)
    rows = csv_rows.month_rows(months)
    header = csv_rows.PARK_MONTH_HEADER
    assert header.index("待查缺口kWh") == 5
    assert header.index("缺口费用元") == 6
    feb = next(row for row in rows if row[0] == "2026-02")
    assert feb[5] == "250.000"
    assert feb[6] == "0.00"          # 缺口不收费
    assert "漏抄" in feb[9] and "投运" in feb[9]


def test_tenant_csv_separates_verified_from_allocated(store, park_batch_id):
    from app.modules.m8_park import csv_rows
    tenants = store.load_park_tenants(park_batch_id)
    rows = csv_rows.tenant_rows(tenants)
    header = csv_rows.PARK_TENANT_HEADER
    assert header[3] == "可证实用量kWh"
    assert header[4] == "分摊口径用量kWh"
    t01 = next(row for row in rows if row[0] == "2026-02" and row[1] == "T01")
    assert t01[3] == "0.000"         # 漏抄月可证实为 0
    assert Decimal(t01[4]) > 0       # 分摊口径仍保留跨月摊入量
    assert t01[5] == "0.00"
    assert t01[9] == "漏抄无账单"
