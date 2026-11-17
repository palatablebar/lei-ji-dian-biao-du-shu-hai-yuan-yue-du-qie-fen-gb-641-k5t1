"""总分对账引擎：园区总表用量 = 租户可证实用量 + 待查缺口。

裁决原则（不把漏计/窃电冒充成租户消费）：
1. 租户只承担「可证实费用」——账单只由分表自身示数还原出的用量产生，
   总表与分表之和的差值永远不摊进任何租户账单；
2. 差值单列「待查缺口」，按可核实证据归因（分表漏抄 / 月中投运 / 未核实）；
3. 每月守恒式逐位闭合：总表用量 = 分表可证实用量合计 + 待查缺口，
   分表之和反而超过总表时不做负缺口抹平，整月标「分表超总表」异常。

漏抄与投运的证据来自原始有效读数，而不是分摊后的区间：
- 漏抄：某只在运分表在该自然月内没有任何一条有效示数锚点，
  跨月区间被日权重摊进来的用量不算「当月可证实」，要从当月可证实口径剔除；
- 投运：分表首条示数落在月中，或区间对月桶覆盖不足整月。

全部 Decimal 计算，量化刻度与 M5 一致（0.001 kWh）。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config
from ..m2_contract import contract as contract_mod
from ..m5_months import weights
from . import park_contract

QUANT = config.USAGE_QUANT


def _month_start(month: str) -> dt.datetime:
    return weights.month_start(month)


def _month_end(month: str) -> dt.datetime:
    return _month_start(weights.shift_month(month, 1))


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _month_days(month: str) -> Decimal:
    end = _month_end(month)
    return Decimal((end - _month_start(month)).total_seconds()) / Decimal(86400)


def _is_valid_reading(item: dict) -> bool:
    """兼容两种读数形态：落库行有 status；质量层内存行只有 read_type。"""
    status = item.get("status")
    if status is not None:
        return status != "quarantine"
    return item.get("read_type") in ("N", "E")


def _spanned_missed_months(intervals: list[dict], meter_id: str,
                           primary: str) -> set[str]:
    """被长区间「穿过」、当月没有任何示数闭合的月份 = 漏抄月。

    业务事实：正常每月一抄时，相邻示数间隔约一个月，区间只贴一个月桶；
    漏抄后补抄会产生一条跨多个自然月的长区间，日权重虽把用量摊进各月，
    但中间那些月份没有任何示数闭合，不具备当月可证实口径。
    - 长区间末月是补抄月（有止点示数），不算漏抄；
    - 首月若只有月初起点、当月未闭合，计漏抄；
    - 投运日之前的月份属于「尚未投运」，由调用方按首读时点剔除。
    """
    missed: set[str] = set()
    for item in intervals:
        if item["meter_id"] != meter_id or item["register"] != primary:
            continue
        slices = weights.month_slices(_parse(item["start_at"]),
                                      _parse(item["end_at"]))
        touched = [month for month, _ in slices]
        if len(touched) >= 2:
            for month in touched[:-1]:       # 末月补抄，不计漏抄
                missed.add(month)
    return missed


def _first_reading(readings: list[dict], meter_id: str) -> dt.datetime | None:
    instants = [_parse(item["read_at"]) for item in readings
                if item.get("meter_id") == meter_id and _is_valid_reading(item)]
    return min(instants) if instants else None


def _coverage(intervals: list[dict], meter_id: str, primary: str,
              month: str) -> dict:
    """该表主寄存器区间对月桶 [月初, 下月初) 的覆盖情况。"""
    mstart, mend = _month_start(month), _month_end(month)
    covered = Decimal("0")
    for item in intervals:
        if item["meter_id"] != meter_id or item["register"] != primary:
            continue
        left = max(_parse(item["start_at"]), mstart)
        right = min(_parse(item["end_at"]), mend)
        if right > left:
            covered += Decimal((right - left).total_seconds()) / Decimal(86400)
    month_days = _month_days(month)
    return {"covered_days": covered, "month_days": month_days,
            "ratio": (covered / month_days) if month_days else Decimal("0")}


def _tenant_flags(*, park: park_contract.ParkContract, month: str,
                  coverage: dict, is_missed: bool,
                  first_reading: dt.datetime | None) -> tuple[list[str], bool, bool]:
    """返回 (标记, 是否漏抄, 是否尚未投运)。

    尚未投运：当月月末仍早于契约投运时点，或早于该表首条示数所在月。
    漏抄当月不计可证实用量、不出账单；未投运月份同样零费用但不打漏抄码。
    """
    flags: list[str] = []
    mstart, mend = _month_start(month), _month_end(month)
    activated = _parse(park.activated_at) if park.activated_at else None
    not_yet = False
    if activated is not None and activated >= mend:
        not_yet = True
    if first_reading is not None and first_reading >= mend:
        not_yet = True
    first_in_month = first_reading is not None and mstart <= first_reading < mend
    partial = (coverage["ratio"] < config.COVERAGE_FULL_RATIO
               and coverage["covered_days"] > 0)
    if (activated is not None and mstart <= activated < mend) \
            or first_in_month or partial:
        flags.append("K_TENANT_PARTIAL_MONTH")

    missed = is_missed and not not_yet
    if missed:
        flags.append("K_TENANT_MISSED_READ")
    return flags, missed, not_yet


def reconcile(*, allocations: list[dict], bills: list[dict],
              intervals: list[dict], readings: list[dict],
              contracts: dict[str, contract_mod.MeterContract],
              park: dict[str, park_contract.ParkContract]) -> dict:
    master = park_contract.master_id(park)
    tenants = park_contract.tenant_ids(park)
    primary = {m: contracts[m].primary_register for m in contracts}

    # 只有读数实际覆盖了园区表（总表或任一分表）时才启用总分对账，
    # 否则 park.csv 对当前批次只是一份无关契约，M8 优雅跳过。
    seen_meters = {item.get("meter_id") for item in readings
                   if _is_valid_reading(item)}
    covered = {mid for mid in park if mid in seen_meters}
    if master not in covered and not (set(tenants) & covered):
        return {"months": [], "tenants": [],
                "stats": _empty_stats(), "master_meter_id": master,
                "tenant_ids": tenants, "enabled": False}

    bill_index = {(b["month"], b["meter_id"]): b for b in bills}
    missed_map = {meter_id: _spanned_missed_months(intervals, meter_id,
                                                   primary[meter_id])
                  for meter_id in tenants}
    first_reading_map = {meter_id: _first_reading(readings, meter_id)
                         for meter_id in tenants}

    # 对账月桶以园区总表实际覆盖的月份为准：补抄产生的租户侧额外月桶
    # 若总表没有对应示数，则无法对账，不纳入月度守恒账。
    if master is not None:
        months = sorted({item["month"] for item in allocations
                         if item["meter_id"] == master})
    else:
        months = sorted({item["month"] for item in allocations
                         if item["meter_id"] in park})
    # 月度用量索引：只取主寄存器，避免与峰平谷重复相加
    usage: dict[tuple, Decimal] = {}
    for item in allocations:
        if primary.get(item["meter_id"]) == item["register"]:
            usage[(item["month"], item["meter_id"])] = item["usage_kwh"]

    month_rows: list[dict] = []
    tenant_rows: list[dict] = []
    for month in months:
        master_usage = usage.get((month, master), Decimal("0")) if master else Decimal("0")
        master_present = master is not None and (month, master) in usage
        tenant_view: list[dict] = []
        verified_sum = Decimal("0")
        for meter_id in tenants:
            cover = _coverage(intervals, meter_id, primary[meter_id], month)
            is_missed = month in missed_map[meter_id]
            flags, missed, not_yet = _tenant_flags(
                park=park[meter_id], month=month, coverage=cover,
                is_missed=is_missed,
                first_reading=first_reading_map[meter_id])
            # 漏抄月：日权重摊入量不具当月示数证据；未投运月：本就不该有用量
            raw_usage = usage.get((month, meter_id), Decimal("0"))
            exclude = missed or not_yet
            tenant_usage = Decimal("0") if exclude else raw_usage
            bill = bill_index.get((month, meter_id))
            # 漏抄/未投运月不向租户收费
            amount = Decimal("0.00") if exclude else (
                bill["amount_yuan"] if bill else Decimal("0.00"))
            view = {
                "month": month, "meter_id": meter_id,
                "tenant_name": park[meter_id].tenant_name,
                "usage_kwh": tenant_usage.quantize(QUANT),
                "allocated_kwh": raw_usage.quantize(QUANT),
                "amount_yuan": amount.quantize(config.MONEY_QUANT),
                "covered_days": cover["covered_days"].quantize(Decimal("0.001")),
                "month_days": cover["month_days"].quantize(Decimal("0.001")),
                "coverage_ratio": cover["ratio"].quantize(Decimal("0.0001")),
                "not_yet_activated": not_yet,
                "flags": flags,
                "bill_missing": missed or (bill is None and not not_yet),
            }
            tenant_view.append(view)
            tenant_rows.append(view)
            verified_sum += tenant_usage

        raw_gap = master_usage - verified_sum
        if not master_present:
            status = "unreconciled"
            gap = Decimal("0.000")
            gap_codes: list[str] = []
        elif raw_gap < 0:
            # 分表之和超过总表：不做负缺口抹平，保留异常待人工核查
            status = "abnormal"
            gap = Decimal("0.000")
            gap_codes = ["K_PARK_SUB_EXCEEDS_MASTER"]
        elif raw_gap == 0:
            status = "balanced"
            gap = Decimal("0.000")
            gap_codes = []
        else:
            status = "gap"
            gap = raw_gap.quantize(QUANT)
            gap_codes = _attribute_gap(tenant_view)
        month_rows.append({
            "month": month,
            "master_meter_id": master,
            "master_usage_kwh": master_usage.quantize(QUANT),
            "master_present": master_present,
            "verified_usage_kwh": verified_sum.quantize(QUANT),
            "verified_amount_yuan": sum(
                (v["amount_yuan"] for v in tenant_view), Decimal("0")
            ).quantize(config.MONEY_QUANT),
            "gap_kwh": gap,
            "gap_amount_yuan": Decimal("0.00"),   # 待查缺口不计费、不向租户收费
            "gap_codes": gap_codes,
            "status": status,
            "conservation_ok": _conservation(status, master_usage, verified_sum, gap),
        })

    stats = _stats(month_rows, tenant_rows)
    return {"months": month_rows, "tenants": tenant_rows, "stats": stats,
            "master_meter_id": master, "tenant_ids": tenants}


def _attribute_gap(tenant_view: list[dict]) -> list[str]:
    """缺口归因：有漏抄给漏抄码，有投运给投运码；都解释不了才是未核实缺口。"""
    codes: list[str] = []
    if any("K_TENANT_MISSED_READ" in v["flags"] for v in tenant_view):
        codes.append("K_GAP_MISSED_READ")
    if any("K_TENANT_PARTIAL_MONTH" in v["flags"] for v in tenant_view):
        codes.append("K_GAP_PARTIAL_MONTH")
    if not codes:
        codes.append("K_GAP_UNVERIFIED")
    return codes


def _conservation(status: str, master_usage: Decimal, verified: Decimal,
                  gap: Decimal) -> bool:
    if status == "unreconciled":
        return True
    if status == "abnormal":
        return False
    return (master_usage.quantize(QUANT)
            == (verified + gap).quantize(QUANT))


def _empty_stats() -> dict:
    return {
        "month_count": 0, "gap_month_count": 0, "abnormal_month_count": 0,
        "unreconciled_month_count": 0,
        "master_usage_total": Decimal("0.000"),
        "verified_usage_total": Decimal("0.000"),
        "gap_usage_total": Decimal("0.000"),
        "verified_amount_total": Decimal("0.00"),
        "missed_tenant_months": 0, "partial_tenant_months": 0,
        "all_conserved": True,
    }


def _stats(month_rows: list[dict], tenant_rows: list[dict]) -> dict:
    gap_total = sum((m["gap_kwh"] for m in month_rows), Decimal("0"))
    verified_total = sum((m["verified_usage_kwh"] for m in month_rows), Decimal("0"))
    master_total = sum((m["master_usage_kwh"] for m in month_rows
                        if m["master_present"]), Decimal("0"))
    amount_total = sum((m["verified_amount_yuan"] for m in month_rows), Decimal("0"))
    return {
        "month_count": len(month_rows),
        "gap_month_count": sum(1 for m in month_rows if m["status"] == "gap"),
        "abnormal_month_count": sum(1 for m in month_rows if m["status"] == "abnormal"),
        "unreconciled_month_count": sum(
            1 for m in month_rows if m["status"] == "unreconciled"),
        "master_usage_total": master_total.quantize(QUANT),
        "verified_usage_total": verified_total.quantize(QUANT),
        "gap_usage_total": gap_total.quantize(QUANT),
        "verified_amount_total": amount_total.quantize(config.MONEY_QUANT),
        "missed_tenant_months": sum(
            1 for t in tenant_rows if "K_TENANT_MISSED_READ" in t["flags"]),
        "partial_tenant_months": sum(
            1 for t in tenant_rows if "K_TENANT_PARTIAL_MONTH" in t["flags"]),
        "all_conserved": all(m["conservation_ok"] for m in month_rows),
    }
