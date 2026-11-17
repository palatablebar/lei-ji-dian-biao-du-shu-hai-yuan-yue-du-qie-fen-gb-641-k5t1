"""M8 入口：总分对账页、归档接口、月度/租户两份 CSV 下载。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ... import errors
from ...web import Ctx, get_ctx, render
from ..m6_batches import resume as resume_mod
from ..m7_export import csv_out
from . import csv_rows, service

router = APIRouter(tags=["M8 园区总分对账"])


def _response(body: str, filename: str) -> Response:
    return Response(
        content=body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/park/reconcile")
def api_park(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        return {"enabled": False, "months": [], "tenants": [], "stats": {}}
    result = service.run(ctx.settings, ctx.store, batch["batch_id"])
    result["batch_id"] = batch["batch_id"]
    result["archives"] = ctx.store.list_park_archives(batch["batch_id"])
    return result


@router.post("/api/park/archive")
def api_archive(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    batch_id = body.get("batch_id") or (ctx.current_batch() or {}).get("batch_id", "")
    resume_mod.require_batch(ctx.store, batch_id)
    outcome = service.archive_current(ctx.settings, ctx.store, batch_id)
    outcome["batch_id"] = batch_id
    return outcome


@router.get("/api/park/archives")
def api_archives(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"archives": ctx.store.list_park_archives(batch_id)}


@router.get("/api/export/park-monthly.csv")
def export_park_monthly(batch_id: str | None = None,
                        ctx: Ctx = Depends(get_ctx)) -> Response:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        from ... import errors
        raise errors.NotFound("还没有批次可以导出", code="E_BATCH_NOT_FOUND")
    result = service.run(ctx.settings, ctx.store, batch["batch_id"])
    if not result["enabled"]:
        raise errors.NotFound("该批次没有园区契约，无总分账可导出",
                              code="E_BATCH_NOT_FOUND")
    body = csv_out.render(csv_rows.PARK_MONTH_HEADER,
                          csv_rows.month_rows(result["months"]))
    return _response(body, f"park-monthly-{batch['batch_id']}.csv")


@router.get("/api/export/park-tenants.csv")
def export_park_tenants(batch_id: str | None = None,
                        ctx: Ctx = Depends(get_ctx)) -> Response:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        from ... import errors
        raise errors.NotFound("还没有批次可以导出", code="E_BATCH_NOT_FOUND")
    result = service.run(ctx.settings, ctx.store, batch["batch_id"])
    if not result["enabled"]:
        raise errors.NotFound("该批次没有园区契约，无租户账可导出",
                              code="E_BATCH_NOT_FOUND")
    body = csv_out.render(csv_rows.PARK_TENANT_HEADER,
                          csv_rows.tenant_rows(result["tenants"]))
    return _response(body, f"park-tenants-{batch['batch_id']}.csv")


@router.get("/park")
def page_park(request: Request, batch_id: str | None = None,
              ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    context = service.page_context(ctx.settings, ctx.store, batch)
    return render(request, "park.html", page="park", page_title="总分对账",
                  batch=batch, **context)
