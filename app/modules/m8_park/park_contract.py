"""园区契约：园区总表与租户分表的角色归属、分表投运时点。

文件 `park.csv` 是可选输入：不存在时 M8 整段跳过，既有 7 模块行为不变。
- meter_role：master（园区总表，全园区只能有一只）/ tenant（租户分表）；
- activated_at：分表投运时点，空表示当月历元起就在运；
- 表号必须同时出现在 meters.csv 契约里，否则进园区隔离区。
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
from pathlib import Path

from ... import config, errors, reasons
from ..m2_contract import contract as contract_mod

FIELDS = ("meter_id", "meter_role", "activated_at")
ROLE_ALIASES = {
    "master": config.ROLE_MASTER, "总表": config.ROLE_MASTER, "园区总表": config.ROLE_MASTER,
    "tenant": config.ROLE_TENANT, "分表": config.ROLE_TENANT, "租户": config.ROLE_TENANT,
    "租户分表": config.ROLE_TENANT,
}


@dataclasses.dataclass(frozen=True)
class ParkContract:
    meter_id: str
    role: str
    activated_at: str | None          # ISO8601（UTC 存储），None 表示无投运起点
    tenant_name: str


def _quarantine(meter_id: str, code: str, detail: str) -> dict:
    return {"lineno": 0, "segment": 0, "code": code, "stage": "park",
            "level": reasons.level(code), "field": "park",
            "raw_value": meter_id, "meter_id": meter_id, "detail": detail}


def _parse_activated(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00").replace("/", "-")
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise errors.ContractInvalid(
            f"投运时点无法解析：{raw!r}", code="E_CONTRACT_INVALID") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=config.CHINA_TZ)
    return value.astimezone(config.UTC).isoformat()


def parse_rows(rows: list[dict],
               contracts: dict[str, contract_mod.MeterContract]) -> tuple[dict, list[dict]]:
    """返回 (ParkContract 字典, 园区隔离行)。坏行进隔离区但不阻断好行。"""
    park: dict[str, ParkContract] = {}
    quarantine: list[dict] = []
    masters: list[str] = []
    for row in rows:
        meter_id = (row.get("meter_id") or "").strip()
        if not meter_id:
            quarantine.append(_quarantine("", "Q_PARK_ROLE_UNKNOWN", "园区契约存在空表号"))
            continue
        role = ROLE_ALIASES.get((row.get("meter_role") or "").strip().lower())
        if role is None:
            quarantine.append(_quarantine(
                meter_id, "Q_PARK_ROLE_UNKNOWN",
                f"园区角色无法识别：{row.get('meter_role')!r}"))
            continue
        if meter_id not in contracts:
            quarantine.append(_quarantine(
                meter_id, "Q_PARK_TENANT_UNKNOWN", "表号不在表计契约 meters.csv"))
            continue
        try:
            activated = _parse_activated(row.get("activated_at") or "")
        except errors.ContractInvalid:
            activated = None
        if role == config.ROLE_MASTER:
            masters.append(meter_id)
        if meter_id in park:
            quarantine.append(_quarantine(meter_id, "Q_PARK_ROLE_UNKNOWN", "表号在园区契约重复"))
            continue
        park[meter_id] = ParkContract(
            meter_id=meter_id, role=role, activated_at=activated,
            tenant_name=contracts[meter_id].name)
    if len(masters) > 1:
        for meter_id in masters[1:]:
            quarantine.append(_quarantine(meter_id, "Q_PARK_MASTER_DUPLICATE",
                                          f"园区总表只能有一只，重复总表：{meter_id}"))
    return park, quarantine


def load(settings,
         contracts: dict[str, contract_mod.MeterContract] | None = None
         ) -> tuple[dict[str, ParkContract], list[dict]]:
    """读 park.csv；文件缺失视为「本批不做总分对账」，返回空契约而非报错。"""
    path = Path(settings.sample_dir) / config.PARK_FILE
    if not path.is_file():
        return {}, []
    contracts = contracts if contracts is not None else contract_mod.load(settings)
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    return parse_rows(rows, contracts)


def master_id(park: dict[str, ParkContract]) -> str | None:
    for item in park.values():
        if item.role == config.ROLE_MASTER:
            return item.meter_id
    return None


def tenant_ids(park: dict[str, ParkContract]) -> list[str]:
    return sorted(item.meter_id for item in park.values()
                  if item.role == config.ROLE_TENANT)


def payload(park: dict[str, ParkContract]) -> list[dict]:
    return [{
        "meter_id": item.meter_id,
        "role": item.role,
        "role_label": "园区总表" if item.role == config.ROLE_MASTER else "租户分表",
        "activated_at": item.activated_at,
        "tenant_name": item.tenant_name,
    } for item in sorted(park.values(), key=lambda c: (c.role, c.meter_id))]
