"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    # 迟到受理/幂等重投场景的附加结论（可选，不影响既有契约）
    receipt: dict[str, Any] | None = None
    continued: bool | None = None
    state: str | None = None
    persisted: bool | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None
    request_key: str | None = None
    # 兼容不同调用方对幂等键的命名
    request_id: str | None = None
    idempotency_key: str | None = None
    client_token: str | None = None
    submission_id: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None
