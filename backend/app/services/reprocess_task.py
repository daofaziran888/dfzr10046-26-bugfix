"""重跑任务领域服务：覆盖策略与血缘影响传递，调度重跑状态机和水位推进，产出重跑任务数据集。"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ReprocessTaskService(BaseModuleService):
    MODULE = "reprocess_task"
    ENTITY = "重跑任务"
    LIST_FIELDS = ["重跑编号", "触发原因", "影响事实表", "重跑区间", "覆盖策略", "依赖批次", "重跑水位", "重跑耗时"]
    REQUIRED_FIELDS = ["重跑编号", "触发原因", "影响事实表"]
    STATUS_ORDER = ["待排期", "重跑中", "已完成", "重跑失败"]
    ACTION_RULES = {"排期重跑": "重跑中", "确认完成": "已完成", "标记失败": "重跑失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    # ------------------------------------------------------------------
    # 跨模块：迟到事实受理结论 -> 重跑待办（按受理凭证幂等）
    # ------------------------------------------------------------------
    def ensure_late_fact_todo(
        self, late_entry: dict[str, Any], receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        request_key = str(
            (receipt or {}).get("request_key") or late_entry.get("request_key") or ""
        ).strip()
        record_no = str(late_entry.get("记录编号", "")).strip()

        # 同一受理凭证 / 同一迟到记录只产生一条待办
        for r in rows:
            if request_key and str(r.get("受理凭证", "")) == request_key:
                if receipt is not None:
                    r["移交状态"] = receipt.get("移交状态", r.get("移交状态", "未移交"))
                    if receipt.get("接班人"):
                        r["接班人"] = receipt["接班人"]
                return r
            if record_no and str(r.get("来源记录", "")) == record_no and r.get("来源模块") == "late_fact":
                if request_key:
                    r["受理凭证"] = request_key
                return r

        business_date = str(late_entry.get("业务发生日", "")).strip()
        fact_table = str(late_entry.get("所属事实表", "")).strip()
        task_no = f"RP-LATE-{record_no or business_date.replace('-', '')}"
        existing_numbers = {str(r.get("重跑编号", "")) for r in rows}
        suffix = 2
        unique_no = task_no
        while unique_no in existing_numbers:
            unique_no = f"{task_no}-{suffix}"
            suffix += 1

        entry = {
            "重跑编号": unique_no,
            "触发原因": "迟到事实回补",
            "影响事实表": fact_table,
            # 单日迟到事实默认精确到业务发生日，页面与接口都可核对具体日期
            "重跑区间": f"{business_date}至{business_date}" if business_date else "",
            "覆盖策略": "并存留痕",
            "依赖批次": record_no,
            "重跑水位": "",
            "重跑耗时": "",
            "status": "待排期",
            "pending": True,
            "abnormal": False,
            # ---- 跨模块关联与排期就绪结论 ----
            "来源模块": "late_fact",
            "来源记录": record_no,
            "来源业务日": business_date,
            "受理凭证": request_key,
            "受理状态": "已受理",
            "实际受理结论": str(late_entry.get("回补结论", "待回补")),
            "可立即排期": True,
            "排期说明": "受理结论已落库，重跑工作台可立即排期，无需等待原登记人在线",
            "登记人": (receipt or {}).get("registrar") or late_entry.get("登记人") or "数据工程组",
            "移交状态": (receipt or {}).get("移交状态", "未移交"),
            "接班人": (receipt or {}).get("接班人", ""),
            "request_key": request_key,
        }
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        return entry

    def list_late_fact_todos(self) -> list[dict[str, Any]]:
        return [
            r for r in store.rows(self.MODULE)
            if r.get("来源模块") == "late_fact" or str(r.get("触发原因", "")) == "迟到事实回补"
        ]

    def find_todo(self, ref: str) -> dict[str, Any] | None:
        """按数字 id / 受理凭证 request_key / 重跑编号定位待办。"""
        ref = str(ref).strip()
        rows = store.rows(self.MODULE)
        if ref.isdigit():
            found = store.find(self.MODULE, int(ref))
            if found is not None:
                return found
        for r in rows:
            if str(r.get("受理凭证", "")) == ref:
                return r
            if str(r.get("request_key", "")) == ref:
                return r
            if str(r.get("重跑编号", "")) == ref:
                return r
            if str(r.get("来源记录", "")) == ref:
                return r
        return None

    def schedule_todo(
        self, ref: str,
        start: str | None = None, end: str | None = None,
        strategy: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """试排期：核对区间日期与覆盖策略，成功后进入重跑中。"""
        entry = self.find_todo(ref)
        if entry is None:
            return False, f"重跑待办 {ref} 不存在", None
        if entry.get("受理状态") not in (None, "", "已受理"):
            return False, "该待办尚无已落库的受理结论，不能排期；请先核实受理结果或办理移交", entry

        start = (start or "").strip()
        end = (end or "").strip()
        if not start and not end and entry.get("来源业务日"):
            start = end = str(entry["来源业务日"])
        if not start:
            rng = str(entry.get("重跑区间", ""))
            if "至" in rng:
                start, end = rng.split("至", 1)
        end = end or start
        try:
            sd = date.fromisoformat(start)
            ed = date.fromisoformat(end)
        except (ValueError, TypeError):
            return False, "排期日期无效，应为 YYYY-MM-DD", entry
        if ed < sd:
            return False, f"重跑区间结束日 {end} 早于开始日 {start}", entry

        if strategy:
            if strategy not in ("整段替换", "并存留痕"):
                return False, f"覆盖策略 {strategy} 无效", entry
            entry["覆盖策略"] = strategy

        entry["重跑区间"] = f"{start}至{end}"
        entry["status"] = "重跑中"
        entry["pending"] = True
        entry["排期说明"] = f"已排期：{start} 至 {end}，可由重跑岗直接执行"
        return True, f"重跑任务 {entry.get('重跑编号')} 已排期，区间 {start}至{end}", entry

    # ------------------------------------------------------------------
    # 既有方法
    # ------------------------------------------------------------------
    def schedule_reprocess(
        self, task_id: str, reason: str, target_table: str,
        reprocess_range: str, strategy: str, depends_on: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """排期重跑任务：覆盖策略决定旧区间是整段替换还是并存留痕。"""
        if strategy not in ("整段替换", "并存留痕"):
            return False, f"覆盖策略 {strategy} 无效", {}

        entry = {
            "重跑编号": task_id, "触发原因": reason,
            "影响事实表": target_table, "重跑区间": reprocess_range,
            "覆盖策略": strategy, "依赖批次": depends_on,
            "重跑水位": "", "重跑耗时": "",
            "status": "待排期", "pending": True, "abnormal": False,
        }
        rows = store.rows(self.MODULE)
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        return True, f"重跑任务 {task_id} 已排期，策略：{strategy}", entry

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """通用行动作：排期重跑对跨模块待办补充日期核对。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "排期重跑" and entry.get("来源模块") == "late_fact":
            ok, message, updated = self.schedule_todo(str(entry_id))
            return updated, message
        return super().run_action(entry_id, action)

    def propagate_lineage_impact(self, target_table: str) -> dict[str, list[str]]:
        """沿血缘边传递重跑影响范围。"""
        lineage_rows = store.rows("lineage_edge")
        direct: list[str] = []
        for r in lineage_rows:
            if r.get("上游对象") == target_table:
                direct.append(str(r.get("下游对象", "")))
        seen = set(direct)
        indirect: list[str] = []
        for d in direct:
            for r in lineage_rows:
                if r.get("上游对象") == d and r.get("下游对象") not in seen:
                    indirect.append(str(r.get("下游对象", "")))
                    seen.add(str(r.get("下游对象", "")))
        return {"direct_impact": direct, "indirect_impact": indirect}

    def complete_reprocess(self, task_id: str, watermark: str) -> tuple[bool, str]:
        """确认重跑完成：更新重跑水位。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("重跑编号") == task_id:
                r["status"] = "已完成"
                r["重跑水位"] = watermark
                r["pending"] = False
                return True, f"重跑任务 {task_id} 已完成，水位 {watermark}"
        return False, f"重跑任务 {task_id} 不存在"
