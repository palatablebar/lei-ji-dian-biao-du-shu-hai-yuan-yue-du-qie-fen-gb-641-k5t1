"""M8 导出证据行：月度守恒账与租户可证实费用，数字与页面逐位同源。"""
from __future__ import annotations

from ... import reasons
from . import service

PARK_MONTH_HEADER = ["月份", "总表号", "总表用量kWh", "租户可证实kWh",
                     "租户可证实费用元", "待查缺口kWh", "缺口费用元", "状态",
                     "守恒", "缺口归因"]
PARK_TENANT_HEADER = ["月份", "分表号", "租户", "可证实用量kWh", "分摊口径用量kWh",
                      "费用元", "覆盖天数", "月天数", "覆盖率", "账单状态", "标记"]


def month_rows(months: list[dict]) -> list[list[str]]:
    rows = []
    for item in months:
        rows.append([
            item["month"], item.get("master_meter_id") or "",
            f"{item['master_usage_kwh']:.3f}",
            f"{item['verified_usage_kwh']:.3f}",
            f"{item['verified_amount_yuan']:.2f}",
            f"{item['gap_kwh']:.3f}", f"{item['gap_amount_yuan']:.2f}",
            service.status_label(item["status"]),
            "是" if item["conservation_ok"] else "否",
            "、".join(reasons.label(code) for code in item["gap_codes"]),
        ])
    return rows


def tenant_rows(tenants: list[dict]) -> list[list[str]]:
    rows = []
    for item in tenants:
        if item.get("not_yet_activated"):
            bill_state = "尚未投运"
        elif item["bill_missing"]:
            bill_state = "漏抄无账单"
        else:
            bill_state = "已出账"
        rows.append([
            item["month"], item["meter_id"], item["tenant_name"],
            f"{item['usage_kwh']:.3f}",
            f"{item.get('allocated_kwh', item['usage_kwh']):.3f}",
            f"{item['amount_yuan']:.2f}",
            f"{item['covered_days']:.3f}", f"{item['month_days']:.3f}",
            f"{item['coverage_ratio']:.4f}",
            bill_state,
            "、".join(reasons.label(code) for code in item["flags"]),
        ])
    return rows
