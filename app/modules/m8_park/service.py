"""M8 编排：从已落库的分摊/账单/区间重算总分对账，并驱动版本化归档。

页面与接口共用同一份结果；没有 park.csv 的批次返回 enabled=False，
既有 7 模块页面与导出不受影响。
"""
from __future__ import annotations

from decimal import Decimal

from ... import errors, reasons
from ..m2_contract import contract as contract_mod
from . import archiver, engine, park_contract


def run(settings, store, batch_id: str) -> dict:
    contracts = contract_mod.load(settings)
    park, park_q = park_contract.load(settings, contracts)
    result: dict = {"enabled": False, "quarantine": park_q,
                    "park_contracts": park_contract.payload(park),
                    "months": [], "tenants": [], "stats": {},
                    "master_meter_id": None, "tenant_ids": []}
    if not park:
        return result
    allocations = store.load_allocations(batch_id)
    bills = store.load_bills(batch_id)
    intervals = store.load_intervals(batch_id)
    readings = store.load_readings(batch_id)
    reconciled = engine.reconcile(
        allocations=allocations, bills=bills, intervals=intervals,
        readings=readings, contracts=contracts, park=park)
    if not reconciled.get("enabled", True):
        result["quarantine"] = park_q
        return result
    reconciled["enabled"] = True
    result.update(reconciled)
    result["quarantine"] = park_q
    return result


def reason_label(code: str) -> str:
    return reasons.label(code)


def gap_label(codes: list[str]) -> str:
    return "、".join(reasons.label(code) for code in codes) if codes else "—"


def status_label(status: str) -> str:
    return {
        "balanced": "守恒",
        "gap": "有待查缺口",
        "abnormal": "分表超总表",
        "unreconciled": "总表缺失无法对账",
    }.get(status, status)


def page_context(settings, store, batch) -> dict:
    if batch is None:
        return {"park": {"enabled": False, "months": [], "tenants": [], "stats": {},
                         "quarantine": []},
                "archives": [], "gap_label": gap_label,
                "status_label": status_label, "reason_label": reason_label}
    result = run(settings, store, batch["batch_id"])
    archives = store.list_park_archives(batch["batch_id"])
    return {"park": result, "archives": archives,
            "gap_label": gap_label, "status_label": status_label,
            "reason_label": reason_label}


def archive_current(settings, store, batch_id: str) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次 {batch_id} 不存在", code="E_BATCH_NOT_FOUND")
    result = run(settings, store, batch_id)
    if not result["enabled"]:
        raise errors.BadRequest("该批次没有园区契约 park.csv，无法归档",
                                code="E_BAD_REQUEST")
    return archiver.publish_batch(store, settings, batch=batch, result=result)


def export_rows(settings, store, batch_id: str) -> list[dict]:
    """导出用的月度行（Decimal 已在 engine 量化）。"""
    result = run(settings, store, batch_id)
    return result["months"]


def dec_str(value) -> str:
    return format(Decimal(str(value)), "f")
