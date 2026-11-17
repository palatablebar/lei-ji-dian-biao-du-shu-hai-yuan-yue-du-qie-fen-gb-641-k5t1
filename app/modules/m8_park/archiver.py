"""月度归档：版本化快照 + 原子发布 + 同批示数幂等。

三条硬规则：
1. 归档中断不破坏旧版：先写 `*.tmp` 再 os.replace 原子换名，进程在中途被杀死，
   上一版已发布文件与库记录仍然完整可读；
2. 原始示数覆盖后无法恢复：归档快照冻结当月全部结果数字（总表/分表/缺口/费用）
   与输入摘要，后续重算只产生新版本，不改写历史版本；
3. 同一批示数（archive_key = 批次 + 月份 + 结果摘要）重复提交不新增版本：
   命中既有版本直接返回原版本号，版本数、行数、合计都不变。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from ... import config, errors

ARCHIVE_DIRNAME = "park_archives"


class ArchiveError(errors.MeterbackError):
    status_code = 409
    code = "E_ARCHIVE_STATE"


def _now() -> dt.datetime:
    return dt.datetime.now(config.UTC)


def archive_dir(settings) -> Path:
    path = Path(settings.data_dir) / ARCHIVE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def result_digest(month_row: dict, tenant_rows: list[dict]) -> str:
    """当月结果摘要：任何一个结果数字变化都会改变批示数。"""
    import hashlib
    payload = json.dumps(
        {"month": month_row, "tenants": tenant_rows},
        ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def archive_key(batch_id: str, month: str, digest: str) -> str:
    return f"{batch_id}|{month}|{digest[:16]}"


def _snapshot(*, batch: dict, month_row: dict, tenant_rows: list[dict],
              digest: str, version: int, archived_at: str) -> dict:
    return {
        "schema": "meterback-park-archive/1",
        "batch_id": batch["batch_id"],
        "batch_key": batch["batch_key"],
        "rule_version": batch["rule_version"],
        "input_digest": batch["input_digest"],
        "month": month_row["month"],
        "version": version,
        "archived_at": archived_at,
        "result_digest": digest,
        "month_row": month_row,
        "tenants": tenant_rows,
    }


def publish(store, settings, *, batch: dict, month_row: dict,
            tenant_rows: list[dict]) -> dict:
    """归档一个月。返回 (快照, 是否幂等命中)。"""
    month = month_row["month"]
    digest = result_digest(month_row, tenant_rows)
    key = archive_key(batch["batch_id"], month, digest)

    existing = store.find_park_archive_by_key(key)
    if existing is not None:
        snapshot = store.load_park_archive(existing["archive_id"])
        return {"snapshot": snapshot, "idempotent": True,
                "archive_id": existing["archive_id"]}

    if month_row["status"] == "unreconciled":
        raise ArchiveError(
            f"{month} 总表缺失、无法对账，不能归档", code="E_ARCHIVE_STATE",
            detail={"month": month, "status": month_row["status"]})

    version = store.next_park_archive_version(batch["batch_id"], month)
    archived_at = _now().isoformat()
    snapshot = _snapshot(batch=batch, month_row=month_row,
                         tenant_rows=tenant_rows, digest=digest,
                         version=version, archived_at=archived_at)
    archive_id = f"par-{batch['batch_id'][3:]}-{month.replace('-', '')}-v{version:02d}"
    filename = f"{archive_id}.json"

    # 原子发布：临时文件与目标文件同目录，os.replace 在 POSIX 上是原子的
    target = archive_dir(settings) / filename
    tmp = target.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

    try:
        store.insert_park_archive(
            archive_id=archive_id, batch_id=batch["batch_id"], month=month,
            version=version, archive_key=key, result_digest=digest,
            filename=filename, archived_at=archived_at,
            snapshot_json=snapshot)
    except Exception:
        # 库写失败也要把已发布文件清掉，避免孤儿文件
        target.unlink(missing_ok=True)
        raise
    return {"snapshot": snapshot, "idempotent": False, "archive_id": archive_id}


def publish_batch(store, settings, *, batch: dict, result: dict) -> dict:
    """把批次里所有可归档月份逐个发布；任一月份中断不影响已发布的旧版。"""
    by_month: dict[str, list[dict]] = {}
    for row in result["tenants"]:
        by_month.setdefault(row["month"], []).append(row)
    published: list[dict] = []
    for month_row in result["months"]:
        outcome = publish(store, settings, batch=batch, month_row=month_row,
                          tenant_rows=by_month.get(month_row["month"], []))
        published.append({"month": month_row["month"],
                          "archive_id": outcome["archive_id"],
                          "version": outcome["snapshot"]["version"],
                          "idempotent": outcome["idempotent"],
                          "status": month_row["status"]})
    return {"archives": published,
            "created": sum(1 for item in published if not item["idempotent"]),
            "reused": sum(1 for item in published if item["idempotent"])}
