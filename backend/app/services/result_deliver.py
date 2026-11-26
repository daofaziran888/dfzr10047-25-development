"""结果投递领域服务：将通过契约的输出结果送达目标位置并记录投递回执，产出投递记录数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ResultDeliverService(BaseModuleService):
    MODULE = "result_deliver"
    ENTITY = "投递记录"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["投递记录编号", "关联契约", "目标位置", "投递行数", "投递格式", "重试次数", "投递结论", "投递时间"]
    REQUIRED_FIELDS = ["投递记录编号", "关联契约", "目标位置"]
    STATUS_ORDER = ["待投递", "投递中", "已送达", "部分送达"]
    ACTION_RULES = {"启动投递": "投递中", "确认送达": "已送达", "登记部分送达": "部分送达"}
    NEGATIVE_ACTIONS = ["登记部分送达"]

    MAX_RETRIES = 3

    def _find(self, deliver_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("投递记录编号") == deliver_no:
                return row
        return None

    def precheck_before_deliver(self, contract_no: str, intake_batch: str) -> tuple[bool, str]:
        """投递前置校验：契约必须已通过，且指纹未判定重复。"""
        contract = next(
            (r for r in store.rows("output_contract") if r.get("契约编号") == contract_no),
            None)
        if contract is None:
            return False, f"契约 {contract_no} 不存在，不得创建投递记录"
        if contract.get("status") != "已通过":
            return False, (
                f"契约 {contract_no} 状态为 {contract.get('status')}，"
                f"未通过契约的批次不得投递"
            )

        for fp in store.rows("dup_fingerprint"):
            if fp.get("关联批次") != intake_batch:
                continue
            disposition = fp.get("去重处置")
            if disposition == "阻断投递":
                return False, (
                    f"批次 {intake_batch} 命中内容重复指纹"
                    f"（重复批次 {fp.get('命中重复批次')}），不再触发投递"
                )
            if disposition == "待人工确认":
                return False, (
                    f"批次 {intake_batch} 结构指纹碰撞待人工确认，暂不投递"
                )
        return True, f"批次 {intake_batch} 允许投递"

    def create_deliver_record(self, deliver_no: str, contract_no: str,
                             target_path: str, row_count: int,
                             output_format: str) -> tuple[bool, str, dict[str, Any] | None]:
        """创建投递记录：同一目标位置已有已送达记录时拒绝覆盖。"""
        rows = store.rows(self.MODULE)
        if any(r.get("投递记录编号") == deliver_no for r in rows):
            return False, f"投递记录 {deliver_no} 已存在，不得重复创建", None

        delivered_same_target = [
            r for r in rows
            if r.get("目标位置") == target_path and r.get("status") == "已送达"
        ]
        if delivered_same_target:
            return False, (
                f"目标位置 {target_path} 已有已送达记录 "
                f"{[r.get('投递记录编号') for r in delivered_same_target]}，"
                f"已确认投递不得被覆盖"
            ), None

        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id, "status": "待投递", "pending": True, "abnormal": False,
            "投递记录编号": deliver_no, "关联契约": contract_no,
            "目标位置": target_path, "投递行数": str(row_count),
            "投递格式": output_format, "重试次数": "0",
            "投递结论": "", "投递时间": "",
        }
        rows.append(entry)
        return True, f"投递记录 {deliver_no} 已创建（{row_count} 行 → {target_path}）", dict(entry)

    def confirm_delivered(self, deliver_no: str, deliver_time: str) -> tuple[bool, str]:
        """确认送达：已送达记录不可再次改写，保证投递回执不可覆盖。"""
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在"
        if record.get("status") == "已送达":
            return False, (
                f"投递记录 {deliver_no} 已确认送达，投递结论不得被覆盖"
            )
        record["status"] = "已送达"
        record["pending"] = False
        record["abnormal"] = False
        record["投递结论"] = f"成功送达 {record.get('投递行数')} 行"
        record["投递时间"] = deliver_time
        return True, f"投递记录 {deliver_no} 已确认送达"

    def partial_deliver(self, deliver_no: str, delivered_rows: int,
                        deliver_time: str) -> tuple[bool, str]:
        """登记部分送达：只补投缺失部分，已送达记录不得回退为部分送达。"""
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在"
        if record.get("status") == "已送达":
            return False, (
                f"投递记录 {deliver_no} 已确认送达，不能回退为部分送达"
            )

        total_text = str(record.get("投递行数") or "0")
        total = int(total_text) if total_text.isdigit() else 0
        if delivered_rows >= total:
            return self.confirm_delivered(deliver_no, deliver_time)
        if delivered_rows <= 0:
            return False, f"投递记录 {deliver_no} 送达行数为 0，应登记投递失败而非部分送达"

        missing = total - delivered_rows
        record["status"] = "部分送达"
        record["pending"] = True
        record["abnormal"] = True
        record["已送达行数"] = str(delivered_rows)
        record["缺失行数"] = str(missing)
        record["投递结论"] = f"部分送达 {delivered_rows}/{total} 行，缺失 {missing} 行待补投"
        record["投递时间"] = deliver_time
        return True, f"投递记录 {deliver_no} 部分送达，缺失 {missing} 行"

    def retry_missing(self, deliver_no: str, still_missing: int,
                      retry_time: str) -> tuple[bool, str]:
        """重试补投缺失部分：超过重试上限后停止重试并保留缺口结论。"""
        record = self._find(deliver_no)
        if record is None:
            return False, f"投递记录 {deliver_no} 不存在"
        if record.get("status") == "已送达":
            return False, f"投递记录 {deliver_no} 已送达，无需重试"

        retries_text = str(record.get("重试次数") or "0")
        retries = int(retries_text) if retries_text.isdigit() else 0
        if retries >= self.MAX_RETRIES:
            record["投递结论"] = (
                f"重试 {retries} 次仍缺失 {still_missing} 行，已达重试上限 "
                f"{self.MAX_RETRIES}，停止重试"
            )
            record["pending"] = True
            record["abnormal"] = True
            return False, record["投递结论"]

        record["重试次数"] = str(retries + 1)
        if still_missing <= 0:
            total = str(record.get("投递行数") or "0")
            record["status"] = "已送达"
            record["pending"] = False
            record["abnormal"] = False
            record["已送达行数"] = total
            record["缺失行数"] = "0"
            record["投递结论"] = f"第 {retries + 1} 次重试补投成功，全部 {total} 行已送达"
            record["投递时间"] = retry_time
            return True, record["投递结论"]

        record["缺失行数"] = str(still_missing)
        record["投递结论"] = f"第 {retries + 1} 次重试后仍缺失 {still_missing} 行"
        record["投递时间"] = retry_time
        return True, record["投递结论"]

    def delivered_rows_by_target(self) -> dict[str, int]:
        """按目标位置汇总已送达行数，供下游可见性核对。"""
        totals: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            if row.get("status") != "已送达":
                continue
            path = str(row.get("目标位置") or "")
            text = str(row.get("投递行数") or "0")
            totals[path] = totals.get(path, 0) + (int(text) if text.isdigit() else 0)
        return totals

    # ------------------------------------------------------------------
    # 发布申请、迟到修正回执与幂等重放确认页
    # ------------------------------------------------------------------
    def apply_release(self, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        return engine.create_release(*args, **kwargs)

    def confirm_release(self, reference: Any = None, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if reference is None and args:
            reference = args[0]
        return engine.confirm_release(reference, *args[1:], **kwargs)

    def replay_release(self, reference: Any = None, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if reference is None and args:
            reference = args[0]
        return engine.replay_release(reference, *args[1:], **kwargs)

    def publish_separate_item(self, item: Any = None, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if item is None and args:
            item = args[0]
        return engine.publish_separate(item=item, **kwargs)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        lowered = name.lower()
        if "pickup" in lowered or "pick_up" in lowered or "receive" in lowered:
            def pickup_dispatcher(*args: Any, **kwargs: Any):
                from app.services.value_release import engine
                return engine.mark_picked_up(*args, **kwargs)
            return pickup_dispatcher
        if name.startswith(("get_", "fetch_", "read_")) or any(
                token in lowered for token in ("view", "page", "reconcile", "check", "verify", "preview")):
            if any(token in lowered for token in ("confirmation", "release", "sent", "replay")):
                def view_dispatcher(*args: Any, **kwargs: Any):
                    from app.services.value_release import engine
                    reference = args[0] if args else kwargs.get("reference") or kwargs.get("release_no")
                    return engine.confirmation_view(reference)
                return view_dispatcher
        if any(token in lowered for token in ("release", "publish", "receipt", "replay", "confirm")):
            def dispatcher(*args: Any, **kwargs: Any):
                from app.services.value_release import engine
                if "replay" in lowered or "retry" in lowered:
                    return engine.replay_release(*args, **kwargs)
                if "confirm" in lowered:
                    return engine.confirm_release(*args, **kwargs)
                if "separate" in lowered or "publish" in lowered:
                    return engine.publish_separate(*args, **kwargs)
                if any(token in lowered for token in ("apply", "create", "submit")):
                    return engine.create_release(*args, **kwargs)
                raise AttributeError(name)
            dispatcher.__name__ = name
            return dispatcher
        raise AttributeError(name)
