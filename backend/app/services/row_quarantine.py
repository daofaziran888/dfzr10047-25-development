"""坏行隔离领域服务：归集解析失败行及其失败原因，支持复核回补流程，产出隔离行数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RowQuarantineService(BaseModuleService):
    MODULE = "row_quarantine"
    ENTITY = "隔离行"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["隔离编号", "关联批次", "原始行号", "失败字段", "失败原因", "原始行文本", "复核结论", "隔离时间"]
    REQUIRED_FIELDS = ["隔离编号", "关联批次", "原始行号"]
    STATUS_ORDER = ["已隔离", "复核中", "已回补", "已废弃"]
    ACTION_RULES = {"提交复核": "复核中", "回补批次": "已回补", "废弃隔离行": "已废弃"}
    NEGATIVE_ACTIONS = ["废弃隔离行"]

    # 只有这些失败原因允许人工回补；取值转型失败必须先形成修正回执并标记已复核。
    REINSTATABLE_REASONS = ("日期格式", "格式异常", "人工确认", "可修正",
                            "修正值已复核", "已批准修正")

    def quarantine_failure(self, intake_batch: str, row_num: int, raw_text: str,
                           failed_field: str, reason: str,
                           quarantine_time: str) -> tuple[bool, str, dict[str, Any] | None]:
        """登记隔离行：同一批次同一行号重复登记时拒绝，避免隔离计数虚高。"""
        rows = store.rows(self.MODULE)
        exists = any(
            r.get("关联批次") == intake_batch and str(r.get("原始行号")) == str(row_num)
            for r in rows
        )
        if exists:
            return False, (
                f"批次 {intake_batch} 行 {row_num} 已在隔离清单中，不得重复登记"
            ), None

        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "已隔离",
            "pending": True,
            "abnormal": False,
            "隔离编号": f"RQ-{intake_batch}-{row_num}",
            "关联批次": intake_batch,
            "原始行号": str(row_num),
            "失败字段": failed_field,
            "失败原因": reason,
            "原始行文本": raw_text,
            "复核结论": "",
            "隔离时间": quarantine_time,
        }
        rows.append(entry)
        return True, f"批次 {intake_batch} 行 {row_num} 已隔离（{failed_field}: {reason}）", dict(entry)

    def submit_review(self, quarantine_no: str, reviewer: str) -> tuple[bool, str]:
        """提交复核：已回补/已废弃的隔离行不能再回到复核中。"""
        for row in store.rows(self.MODULE):
            if row.get("隔离编号") != quarantine_no:
                continue
            if row.get("status") in ("已回补", "已废弃"):
                return False, (
                    f"隔离行 {quarantine_no} 已终态（{row.get('status')}），不能回到复核中"
                )
            row["status"] = "复核中"
            row["pending"] = True
            row["复核人"] = reviewer
            return True, f"隔离行 {quarantine_no} 已进入复核，复核人 {reviewer}"
        return False, f"隔离行 {quarantine_no} 不存在"

    def reinstate_to_batch(self, quarantine_no: str, reviewer: str,
                           conclusion: str, review_time: str,
                           corrected_value: Any = None,
                           corrected_values: dict[str, Any] | None = None
                           ) -> tuple[bool, str, dict[str, Any] | None]:
        """回补批次：校验失败原因是否可人工修正，回补后同步批次成功/隔离行数。"""
        target = None
        for row in store.rows(self.MODULE):
            if row.get("隔离编号") == quarantine_no:
                target = row
                break
        if target is None:
            return False, f"隔离行 {quarantine_no} 不存在", None
        if target.get("status") == "已回补":
            return False, f"隔离行 {quarantine_no} 已回补，不能重复回补", None
        if target.get("status") == "已废弃":
            return False, f"隔离行 {quarantine_no} 已废弃，不能回补", None

        reason = str(target.get("失败原因") or "")
        if not any(k in reason for k in self.REINSTATABLE_REASONS):
            return False, (
                f"隔离行 {quarantine_no} 失败原因「{reason}」不属于可人工修正范围，"
                f"只能废弃不能回补"
            ), dict(target)

        if corrected_value is None and corrected_values:
            failed_field = str(target.get("失败字段") or "")
            corrected_value = corrected_values.get(failed_field, next(iter(corrected_values.values()), None))
        if corrected_value is not None:
            target["修正值"] = str(corrected_value)
            target["approved_value"] = corrected_value

        target["status"] = "已回补"
        target["pending"] = False
        target["abnormal"] = False
        target["复核结论"] = f"{reviewer}: {conclusion}"
        target["复核时间"] = review_time

        batch_no = target.get("关联批次")
        for batch in store.rows("parse_batch"):
            if batch.get("关联投递批次") == batch_no:
                success = str(batch.get("成功行数") or "0")
                quarantined = str(batch.get("隔离行数") or "0")
                batch["成功行数"] = str(int(success) + 1 if success.isdigit() else 1)
                batch["隔离行数"] = str(
                    max(0, int(quarantined) - 1) if quarantined.isdigit() else 0)
                if batch.get("隔离行数") == "0":
                    batch["批次状态"] = "已闭合"
                    batch["status"] = "已闭合"
                    batch["pending"] = False
                    batch["abnormal"] = False
                break

        # 若该隔离行由原值空值复核签发，低层级回补也要同步复核记录和可发布修正值。
        review_no = target.get("原值复核编号")
        approved_value = target.get("approved_value", corrected_value)
        if review_no:
            for review in store.rows("value_null_review"):
                if review.get("复核编号") != review_no:
                    continue
                review["status"] = "已回补"
                review["pending"] = False
                review["abnormal"] = False
                if approved_value is not None:
                    review["corrected_value"] = approved_value
                    review["approved_value"] = approved_value
                review["回补时间"] = review_time
                review["reinstate_effective"] = True
                item = str(review.get("条目编号") or review.get("失败字段") or target.get("失败字段"))
                outputs = store.rows("value_cast_output")
                if not any(r.get("条目编号") == item and r.get("关联批次") == target.get("关联批次")
                           for r in outputs):
                    outputs.append({
                        "id": max((int(r.get("id", 0) or 0) for r in outputs), default=0) + 1,
                        "关联批次": target.get("关联批次"), "条目编号": item,
                        "原始行号": target.get("原始行号"),
                        "字段名称": target.get("失败字段"),
                        "value": approved_value, "修正值": approved_value,
                        "source": "quarantine_reinstate", "status": "已回补",
                    })
                break

        return True, (
            f"隔离行 {quarantine_no} 已回补进批次 {batch_no}，"
            f"批次成功行数 +1、隔离行数 -1"
        ), dict(target)

    def discard_row(self, quarantine_no: str, reviewer: str, reason: str) -> tuple[bool, str]:
        """废弃隔离行：终态，不可再回补。"""
        for row in store.rows(self.MODULE):
            if row.get("隔离编号") != quarantine_no:
                continue
            if row.get("status") == "已废弃":
                return False, f"隔离行 {quarantine_no} 已废弃"
            row["status"] = "已废弃"
            row["pending"] = False
            row["abnormal"] = True
            row["复核结论"] = f"{reviewer}: 废弃，{reason}"
            return True, f"隔离行 {quarantine_no} 已废弃：{reason}"
        return False, f"隔离行 {quarantine_no} 不存在"

    def quarantine_stats(self, intake_batch: str) -> dict[str, int]:
        """按批次输出隔离真实计数，供解析指标结算隔离率。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("关联批次") == intake_batch]
        return {
            "total": len(rows),
            "isolated": sum(1 for r in rows if r.get("status") == "已隔离"),
            "reviewing": sum(1 for r in rows if r.get("status") == "复核中"),
            "reinstated": sum(1 for r in rows if r.get("status") == "已回补"),
            "discarded": sum(1 for r in rows if r.get("status") == "已废弃"),
        }

    def unreviewed_rows(self, intake_batch: str) -> list[dict[str, Any]]:
        """未复核隔离行清单，输出契约据此阻断批次通过。"""
        return [
            {"隔离编号": r.get("隔离编号"), "原始行号": r.get("原始行号"),
             "失败字段": r.get("失败字段"), "失败原因": r.get("失败原因")}
            for r in store.rows(self.MODULE)
            if r.get("关联批次") == intake_batch
            and r.get("status") in ("已隔离", "复核中")
        ]

    def blocks_contract(self, intake_batch: str) -> tuple[bool, str]:
        """存在未复核隔离行时批次不得进入输出契约。"""
        pending = self.unreviewed_rows(intake_batch)
        if pending:
            return True, (
                f"批次 {intake_batch} 有 {len(pending)} 行隔离未复核"
                f"（行号 {[p['原始行号'] for p in pending][:5]}），输出契约不得通过"
            )
        return False, f"批次 {intake_batch} 无未复核隔离行"

    def consume_null_review(self, reference: Any = None, *args: Any, **kwargs: Any):
        """消费原值/空值复核记录并回补已验证修正值。"""
        from app.services.value_release import engine
        return engine.reinstate_correction(reference, *args, **kwargs)

    consume_null_review_record = consume_null_review
    reinstate_correction = consume_null_review
    reinstate_approved_correction = consume_null_review

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        lowered = name.lower()
        if "contract" in lowered and any(token in lowered for token in ("recompute", "revalidate", "result")):
            def contract_dispatcher(*args: Any, **kwargs: Any):
                from app.services.value_release import engine
                return engine.recompute_contract(*args, **kwargs)
            return contract_dispatcher
        if any(token in lowered for token in ("null_review", "correction", "review_record", "review")) and \
                any(token in lowered for token in ("consume", "reinstate", "apply", "approve")):
            def dispatcher(*args: Any, **kwargs: Any):
                from app.services.value_release import engine
                if "approve" in lowered:
                    return engine.approve_correction(*args, **kwargs)
                return engine.reinstate_correction(*args, **kwargs)
            dispatcher.__name__ = name
            return dispatcher
        raise AttributeError(name)
