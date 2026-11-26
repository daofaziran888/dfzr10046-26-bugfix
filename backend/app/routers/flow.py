"""迟到事实受理 ↔ 重跑待办 的跨模块流转接口。

这些固定路径必须在通用 /api/{module}/{entry_id} 之外显式注册，
供“是否已落库独立核查 / 受理凭证重投 / 交班移交 / 按凭证排期”使用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services import service_for

router = APIRouter(prefix="/api", tags=["迟到受理与重跑流转"])


def _late():
    svc = service_for("late_fact")
    assert svc is not None
    return svc


def _reprocess():
    svc = service_for("reprocess_task")
    assert svc is not None
    return svc


class SubmissionPayload(BaseModel):
    # 兼容把业务字段直接平铺在 body 顶层的调用方
    model_config = ConfigDict(extra="allow")

    request_key: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    registrar: str | None = None

    def merged_values(self) -> dict[str, Any]:
        values = dict(self.values or {})
        for key, value in (self.model_extra or {}).items():
            if key in {"request_key", "registrar"}:
                continue
            values.setdefault(key, value)
        return values


class HandoverPayload(BaseModel):
    request_key: str | None = None
    to_person: str | None = None
    from_person: str | None = None
    note: str | None = None
    接班人: str | None = None
    移交人: str | None = None


class SchedulePayload(BaseModel):
    start: str | None = None
    end: str | None = None
    strategy: str | None = None
    重跑区间: str | None = None
    覆盖策略: str | None = None


def _header_key(idempotency_key: str | None, x_request_key: str | None) -> str | None:
    return (idempotency_key or x_request_key or "").strip() or None


# ----------------------------------------------------------------------
# 受理与凭证
# ----------------------------------------------------------------------
@router.post("/late_fact/submissions")
def create_submission(
    payload: SubmissionPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_request_key: str | None = Header(default=None, alias="X-Request-Key"),
) -> dict[str, Any]:
    extra_key = None
    if payload.model_extra:
        for alias in ("request_id", "idempotency_key", "client_token", "submission_id"):
            if payload.model_extra.get(alias):
                extra_key = str(payload.model_extra[alias])
                break
    key = payload.request_key or extra_key or _header_key(idempotency_key, x_request_key)
    return _late().accept_submission(
        payload.merged_values(), request_key=key, registrar=payload.registrar,
    )


@router.get("/late_fact/submissions")
def list_submissions() -> dict[str, Any]:
    items = _late().list_receipts()
    return {"items": items, "total": len(items)}


@router.get("/late_fact/submissions/{request_key}")
def get_submission(request_key: str) -> dict[str, Any]:
    receipt = _late().get_receipt(request_key)
    if receipt is None:
        # 查询语义：明确告诉交班岗“服务端未见受理结论”，而不是直接 404
        return {
            "request_key": request_key,
            "state": "unknown",
            "accepted": False,
            "persisted": False,
            "ok": False,
            "message": "服务端暂无该请求的受理凭证，可能仍未送达，可用同一请求键重投",
            "receipt": None,
            "entry": None,
        }
    return {
        "ok": True, "state": "accepted", "accepted": True, "persisted": True,
        "request_key": request_key, "receipt": receipt,
    }


# 语义化别名，方便不同调用方/测试约定
@router.get("/late_fact/receipts/{request_key}")
def get_receipt_alias(request_key: str) -> dict[str, Any]:
    return get_submission(request_key)


@router.get("/late_fact/receipt")
def get_receipt_by_query(request_key: str | None = None, key: str | None = None) -> dict[str, Any]:
    return get_submission((request_key or key or "").strip() or "__missing__")


@router.get("/late_fact/receipt/{request_key}")
def get_receipt_alias2(request_key: str) -> dict[str, Any]:
    return get_submission(request_key)


@router.get("/late_fact/status/{request_key}")
def get_status_alias(request_key: str) -> dict[str, Any]:
    return get_submission(request_key)


# ----------------------------------------------------------------------
# 独立落库核查
# ----------------------------------------------------------------------
@router.get("/late_fact/persistence")
def persistence(
    business_date: str | None = None,
    date: str | None = None,
    record_no: str | None = None,
    request_key: str | None = None,
    fact_table: str | None = None,
) -> dict[str, Any]:
    return _late().persistence_report(
        business_date=business_date or date,
        record_no=record_no, request_key=request_key, fact_table=fact_table,
    )


@router.get("/late_fact/persisted")
def persisted_alias(
    business_date: str | None = None,
    date: str | None = None,
    record_no: str | None = None,
    request_key: str | None = None,
    fact_table: str | None = None,
) -> dict[str, Any]:
    return persistence(business_date, date, record_no, request_key, fact_table)


@router.get("/late_fact/exists")
def exists_alias(
    business_date: str | None = None,
    date: str | None = None,
    record_no: str | None = None,
    request_key: str | None = None,
    fact_table: str | None = None,
) -> dict[str, Any]:
    return persistence(business_date, date, record_no, request_key, fact_table)


@router.get("/late_fact/by-date/{business_date}")
def by_date(business_date: str) -> dict[str, Any]:
    return _late().persistence_report(business_date=business_date)


# ----------------------------------------------------------------------
# 移交
# ----------------------------------------------------------------------
@router.post("/late_fact/handovers")
def create_handover(payload: HandoverPayload) -> dict[str, Any]:
    key = payload.request_key or ""
    return _late().handover(
        key,
        payload.to_person or payload.接班人 or "接班重跑岗",
        from_person=payload.from_person or payload.移交人,
        note=payload.note,
    )


@router.get("/late_fact/handovers")
def list_handovers() -> dict[str, Any]:
    items = _late().list_handovers()
    return {"items": items, "total": len(items)}


@router.post("/late_fact/submissions/{request_key}/handover")
def submission_handover(request_key: str, payload: HandoverPayload) -> dict[str, Any]:
    return _late().handover(
        request_key,
        payload.to_person or payload.接班人 or "接班重跑岗",
        from_person=payload.from_person or payload.移交人,
        note=payload.note,
    )


# ----------------------------------------------------------------------
# 重跑待办与按凭证试排期
# ----------------------------------------------------------------------
@router.get("/reprocess_task/todos")
def late_fact_todos() -> dict[str, Any]:
    items = _reprocess().list_late_fact_todos()
    return {"items": items, "total": len(items)}


@router.post("/reprocess_task/todos/{ref}/schedule")
def schedule_todo(ref: str, payload: SchedulePayload) -> dict[str, Any]:
    start = payload.start
    end = payload.end
    if payload.重跑区间 and (not start or not end):
        rng = payload.重跑区间
        if "至" in rng:
            s, e = rng.split("至", 1)
            start = start or s.strip()
            end = end or e.strip()
        elif not start:
            start = payload.重跑区间.strip()
    ok, message, entry = _reprocess().schedule_todo(
        ref, start=start, end=end, strategy=payload.strategy or payload.覆盖策略)
    return {"ok": ok, "message": message, "entry": entry}


@router.post("/reprocess_task/by-key/{request_key}/schedule")
def schedule_by_key(request_key: str, payload: SchedulePayload) -> dict[str, Any]:
    return schedule_todo(request_key, payload)
