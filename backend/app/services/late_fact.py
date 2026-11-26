"""迟到事实领域服务：登记校验晚到数据的回补水位，产出迟到记录数据集。

本次修复围绕“同一份实际提交”建立受理凭证（receipt）：
- 每个请求可带 request_key（幂等键），重投同一键只续原记录；
- 没有键时按业务自然键“记录编号”沿用历史登记；
- 实际受理结论（已落库）跨模块写入 reprocess_task 的重跑待办；
- 受理凭证/移交状态可独立查询，供交班人员核实续做。
"""
from __future__ import annotations

import threading
import uuid
from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _today() -> str:
    return date.today().isoformat()


def new_request_key() -> str:
    return f"LF-REQ-{uuid.uuid4().hex[:16]}"


class LateFactService(BaseModuleService):
    MODULE = "late_fact"
    ENTITY = "迟到记录"
    LIST_FIELDS = ["记录编号", "所属事实表", "业务发生日", "到达日", "滞后天数", "受影响分区", "回补结论", "登记时间"]
    REQUIRED_FIELDS = ["记录编号", "所属事实表", "业务发生日"]
    STATUS_ORDER = ["已登记", "待回补", "已回补", "拒绝回补"]
    ACTION_RULES = {"提交回补": "待回补", "确认回补": "已回补", "拒绝记录": "拒绝回补"}
    NEGATIVE_ACTIONS = ["拒绝记录"]

    def __init__(self) -> None:
        # request_key -> 受理凭证（含落库条目 id 与跨模块待办引用）
        self._receipts: dict[str, dict[str, Any]] = {}
        # 仅办理了移交、服务端尚未见到受理结论的在途请求
        self._handovers: dict[str, dict[str, Any]] = {}
        # 无显式幂等键时，按请求体指纹识别“同一请求的重投”
        self._fingerprints: dict[str, str] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------
    def find_by_record_no(self, record_id: str) -> dict[str, Any] | None:
        record_id = str(record_id or "").strip()
        if not record_id:
            return None
        for r in store.rows(self.MODULE):
            if str(r.get("记录编号", "")).strip() == record_id:
                return r
        return None

    def get_receipt(self, request_key: str) -> dict[str, Any] | None:
        with self._lock:
            receipt = self._receipts.get(str(request_key or "").strip())
            return dict(receipt) if receipt else None

    def list_receipts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._receipts.values()]

    def persistence_report(
        self,
        *,
        business_date: str | None = None,
        record_no: str | None = None,
        request_key: str | None = None,
        fact_table: str | None = None,
    ) -> dict[str, Any]:
        """独立可查：六月三日（或任意业务日/凭证）是否已经落库。"""
        rows = list(store.rows(self.MODULE))
        if business_date:
            bd = str(business_date).strip()
            rows = [r for r in rows if str(r.get("业务发生日", "")) == bd]
        if record_no:
            no = str(record_no).strip()
            rows = [r for r in rows if str(r.get("记录编号", "")) == no]
        if fact_table:
            t = str(fact_table).strip()
            rows = [r for r in rows if str(r.get("所属事实表", "")) == t]
        receipt = None
        if request_key:
            key = str(request_key).strip()
            receipt = self._receipts.get(key)
            if receipt is not None:
                entry_id = receipt.get("entry_id")
                linked = [r for r in rows if int(r.get("id", -1)) == int(entry_id)] if entry_id is not None else []
                if linked:
                    rows = linked
        return {
            "persisted": len(rows) > 0,
            "exists": len(rows) > 0,
            "accepted": len(rows) > 0 or receipt is not None,
            "count": len(rows),
            "business_date": business_date,
            "record_no": record_no,
            "request_key": request_key,
            "receipt": dict(receipt) if receipt else None,
            "items": rows,
        }

    @staticmethod
    def _fingerprint(values: dict[str, Any], business_date: str, fact_table: str) -> str:
        """对业务输入做稳定指纹：同一请求体重投得到同一指纹。"""
        import hashlib
        import json

        skip = {"request_key", "idempotency_key", "request_id", "client_token", "submission_id"}
        payload = {
            k: ("" if v is None else str(v).strip())
            for k, v in sorted(values.items())
            if k not in skip and not str(k).startswith("_")
        }
        payload.setdefault("业务发生日", business_date)
        payload.setdefault("所属事实表", fact_table)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    # ------------------------------------------------------------------
    # 登记（严格领域方法，保留既有签名与行为，附加幂等与跨模块写入）
    # ------------------------------------------------------------------
    def register_late_fact(
        self, record_id: str, fact_table: str, business_date: str,
        arrival_date: str, affected_partition: str,
        *,
        request_key: str | None = None,
        registrar: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """登记迟到事实：计算滞后天数，回补结论默认待回补。

        同一记录编号重投时沿用原记录；受理后幂等写出重跑待办。
        """
        with self._lock:
            existing = self.find_by_record_no(record_id)
            if existing is not None:
                key = request_key or str(existing.get("request_key") or f"LF-NAT-{record_id}")
                existing["request_key"] = key
                receipt = self._receipts.get(key) or self._build_receipt(
                    key, existing, registrar=registrar, continued=True)
                self._receipts[key] = receipt
                todo = self._ensure_todo(existing, receipt)
                receipt["todo"] = self._todo_ref(todo)
                return True, f"迟到事实 {record_id} 已登记，重投沿用原记录，不重复落库", existing

            try:
                bd = date.fromisoformat(business_date)
                ad = date.fromisoformat(arrival_date)
            except (ValueError, TypeError):
                return False, "日期格式无效", None
            lag = (ad - bd).days
            if lag <= 0:
                return False, f"迟到天数 {lag} <= 0，不视为迟到事实", None

            entry = {
                "记录编号": record_id, "所属事实表": fact_table,
                "业务发生日": business_date, "到达日": arrival_date,
                "滞后天数": lag, "受影响分区": affected_partition,
                "回补结论": "待回补", "登记时间": arrival_date,
                "status": "待回补", "pending": True, "abnormal": False,
            }
            rows = store.rows(self.MODULE)
            entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            key = (request_key or "").strip() or f"LF-NAT-{record_id}"
            entry["request_key"] = key
            entry["受理状态"] = "已受理"
            if registrar:
                entry["登记人"] = registrar
            rows.append(entry)

            receipt = self._build_receipt(key, entry, registrar=registrar)
            todo = self._ensure_todo(entry, receipt)
            receipt["todo"] = self._todo_ref(todo)
            self._receipts[key] = receipt
            return True, f"迟到事实 {record_id} 已登记，滞后 {lag} 天", entry

    # ------------------------------------------------------------------
    # 受理入口：通用资源路由与 /submissions 专用路由共用
    # ------------------------------------------------------------------
    def accept_submission(
        self,
        values: dict[str, Any],
        request_key: str | None = None,
        registrar: str | None = None,
    ) -> dict[str, Any]:
        """受理一份登记请求，返回完整结论。任何重投都不会产生第二条记录。"""
        values = dict(values or {})
        key = (
            str(request_key or "").strip()
            or str(values.pop("request_key", "") or "").strip()
            or str(values.pop("idempotency_key", "") or "").strip()
            or str(values.pop("request_id", "") or "").strip()
            or str(values.pop("client_token", "") or "").strip()
            or str(values.pop("submission_id", "") or "").strip()
            or str(values.get("请求凭证", "") or "").strip()
            or str(values.get("请求编号", "") or "").strip()
        )
        record_no = str(values.get("记录编号") or "").strip()

        with self._lock:
            # 1) 同一请求键：只续原凭证
            if key and key in self._receipts:
                receipt = self._receipts[key]
                entry = store.find(self.MODULE, int(receipt["entry_id"])) if receipt.get("entry_id") is not None else self.find_by_record_no(receipt.get("记录编号", ""))
                if entry is not None:
                    receipt["entry"] = entry
                    todo = self._ensure_todo(entry, receipt)
                    receipt["todo"] = self._todo_ref(todo)
                return {
                    "ok": True, "accepted": True, "persisted": True,
                    "continued": True, "duplicate": True, "idempotent": True,
                    "state": "accepted",
                    "message": f"同一请求 {key} 已受理，沿用原记录 {receipt.get('记录编号', '')}，未重复登记",
                    "entry": entry, "receipt": receipt,
                }

            # 2) 自然键已存在（历史登记）：沿用原记录并补挂凭证
            existing = self.find_by_record_no(record_no) if record_no else None
            if existing is not None:
                key = key or str(existing.get("request_key") or f"LF-NAT-{record_no}")
                existing["request_key"] = key
                receipt = self._receipts.get(key) or self._build_receipt(
                    key, existing, registrar=registrar, continued=True)
                self._receipts[key] = receipt
                todo = self._ensure_todo(existing, receipt)
                receipt["todo"] = self._todo_ref(todo)
                return {
                    "ok": True, "accepted": True, "persisted": True,
                    "continued": True, "duplicate": True, "idempotent": True,
                    "state": "accepted",
                    "message": f"记录编号 {record_no} 已有历史登记，重投只续原记录",
                    "entry": existing, "receipt": receipt,
                }

            missing = [
                f for f in self.REQUIRED_FIELDS
                if not str(values.get(f) or "").strip()
            ]
            if missing:
                return {
                    "ok": False, "accepted": False, "persisted": False,
                    "state": "failed",
                    "message": f"缺少必填字段：{'、'.join(missing)}",
                    "missing": missing, "entry": None, "receipt": None,
                }

            business_date = str(values["业务发生日"]).strip()
            arrival_date = str(values.get("到达日") or "").strip() or _today()
            try:
                bd = date.fromisoformat(business_date)
                ad = date.fromisoformat(arrival_date)
            except ValueError:
                return {
                    "ok": False, "accepted": False, "persisted": False,
                    "state": "failed", "message": "日期格式无效，应为 YYYY-MM-DD",
                    "entry": None, "receipt": None,
                }

            fact_table = str(values.get("所属事实表") or "").strip() or "销售事实表"
            lag = (ad - bd).days
            partition = str(values.get("受影响分区") or "").strip() or f"part-{business_date[:7].replace('-', '')}"

            # 3) 无键且无自然编号时，按请求体指纹识别完全相同的重投
            fingerprint = self._fingerprint(values, business_date, fact_table)
            if not key and fingerprint in self._fingerprints:
                key = self._fingerprints[fingerprint]
                receipt = self._receipts.get(key)
                entry = store.find(self.MODULE, int(receipt["entry_id"])) if receipt and receipt.get("entry_id") is not None else None
                if receipt is not None and entry is not None:
                    todo = self._ensure_todo(entry, receipt)
                    receipt["todo"] = self._todo_ref(todo)
                    return {
                        "ok": True, "accepted": True, "persisted": True,
                        "continued": True, "duplicate": True, "idempotent": True,
                        "state": "accepted",
                        "message": "同一请求体已受理，重投只续原记录",
                        "entry": entry, "receipt": receipt,
                    }

            record_id = record_no or f"LF-{business_date.replace('-', '')}-{uuid.uuid4().hex[:6]}"
            key = key or new_request_key()

            conclusion = str(values.get("回补结论") or "").strip()
            if not conclusion:
                conclusion = "待回补" if lag > 0 else "非迟到数据"

            entry = {
                "记录编号": record_id,
                "所属事实表": fact_table,
                "业务发生日": business_date,
                "到达日": arrival_date,
                "滞后天数": lag,
                "受影响分区": partition,
                "回补结论": conclusion,
                "登记时间": str(values.get("登记时间") or arrival_date),
                "status": "待回补" if lag > 0 else "已登记",
                "pending": lag > 0,
                "abnormal": False,
                "request_key": key,
                "受理状态": "已受理",
            }
            if registrar or values.get("登记人"):
                entry["登记人"] = registrar or str(values.get("登记人"))
            rows = store.rows(self.MODULE)
            entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append(entry)

            receipt = self._build_receipt(key, entry, registrar=registrar)
            self._receipts[key] = receipt
            self._fingerprints[fingerprint] = key
            # 实际受理结论跨模块写入重跑待办（仅迟到且需要回补的记录）
            if lag > 0:
                todo = self._ensure_todo(entry, receipt)
                receipt["todo"] = self._todo_ref(todo)
            return {
                "ok": True, "accepted": True, "persisted": True,
                "continued": False, "state": "accepted",
                "message": f"迟到事实 {record_id} 已受理落库，滞后 {lag} 天",
                "entry": entry, "receipt": receipt,
            }

    # 让通用 POST /api/late_fact 也具备幂等与跨模块能力
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        result = self.accept_submission(values)
        if not result.get("ok"):
            missing = result.get("missing") or []
            if missing:
                return None, missing
            return None, [result.get("message", "登记失败")]
        return result["entry"], []

    # ------------------------------------------------------------------
    # 移交
    # ------------------------------------------------------------------
    def handover(
        self, request_key: str, to_person: str,
        *, from_person: str | None = None, note: str | None = None,
    ) -> dict[str, Any]:
        key = str(request_key or "").strip()
        to_person = str(to_person or "").strip() or "接班重跑岗"
        with self._lock:
            record = self._handovers.get(key) or {"request_key": key}
            record.update({
                "request_key": key,
                "移交状态": "已移交",
                "移交人": from_person or record.get("移交人") or "原登记人",
                "接班人": to_person,
                "移交说明": note or record.get("移交说明") or "",
                "移交时间": _today(),
            })
            self._handovers[key] = record

            receipt = self._receipts.get(key)
            if receipt is not None:
                receipt["移交状态"] = "已移交"
                receipt["接班人"] = to_person
                entry = store.find(self.MODULE, int(receipt["entry_id"])) if receipt.get("entry_id") is not None else None
                if entry is not None:
                    entry["移交状态"] = "已移交"
                    entry["接班人"] = to_person
                    self._ensure_todo(entry, receipt)
            return {"ok": True, "handover": record}

    def list_handovers(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._handovers.values())

    # ------------------------------------------------------------------
    # 内部：凭证与跨模块待办
    # ------------------------------------------------------------------
    def _build_receipt(
        self, key: str, entry: dict[str, Any], *,
        registrar: str | None = None, continued: bool = False,
    ) -> dict[str, Any]:
        return {
            "request_key": key,
            "receipt_no": f"RCP-{entry.get('id', '')}-{str(entry.get('记录编号', ''))}",
            "记录编号": entry.get("记录编号"),
            "业务发生日": entry.get("业务发生日"),
            "所属事实表": entry.get("所属事实表"),
            "entry_id": entry.get("id"),
            "state": "accepted",
            "accepted": True,
            "persisted": True,
            "continued": continued,
            "registrar": registrar or entry.get("登记人") or "数据工程组",
            "移交状态": entry.get("移交状态", "未移交"),
            "接班人": entry.get("接班人", ""),
            "accepted_at": _today(),
        }

    def _ensure_todo(self, entry: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        # 延迟导入避免模块初始化环
        from app.services import service_for

        svc = service_for("reprocess_task")
        assert svc is not None
        return svc.ensure_late_fact_todo(entry, receipt)  # type: ignore[attr-defined]

    @staticmethod
    def _todo_ref(todo: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": todo.get("id"),
            "重跑编号": todo.get("重跑编号"),
            "重跑区间": todo.get("重跑区间"),
            "status": todo.get("status"),
            "可立即排期": todo.get("可立即排期", True),
        }

    def decide_backfill(
        self, record_id: str, loaded_watermark: str, strategy: str,
    ) -> tuple[bool, str]:
        """判定回补方式：整段重算或增量追加。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("记录编号") == record_id:
                lag = int(r.get("滞后天数", 0))
                if lag > 30:
                    r["回补结论"] = "整段重算"
                else:
                    r["回补结论"] = "增量追加"
                r["status"] = "已回补"
                r["pending"] = False
                return True, f"迟到事实 {record_id} 回补结论：{r['回补结论']}"
        return False, f"迟到记录 {record_id} 不存在"
