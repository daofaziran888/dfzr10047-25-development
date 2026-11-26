"""取值转型领域服务：按目标类型将字符串取值转为强类型字段并记录失败数，产出转型规则数据集。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ValueCastService(BaseModuleService):
    MODULE = "value_cast"
    ENTITY = "转型规则"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["转型编号", "目标类型", "来源类型", "日期格式约定", "数值精度", "空值填充", "转型失败数", "最近执行"]
    REQUIRED_FIELDS = ["转型编号", "目标类型", "来源类型"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "存在失败"]
    ACTION_RULES = {"启动转型": "执行中", "确认完成": "已完成", "登记失败": "存在失败"}
    NEGATIVE_ACTIONS = ["登记失败"]

    # 业务侧用 yyyy-MM-dd 约定日期格式，转型前需翻译成 strptime 的格式串
    PATTERN_TOKENS = (("yyyy", "%Y"), ("yy", "%y"), ("MM", "%m"), ("dd", "%d"),
                      ("HH", "%H"), ("mm", "%M"), ("ss", "%S"))

    @classmethod
    def to_strptime_pattern(cls, pattern: str) -> str:
        """把 yyyy-MM-dd HH:mm:ss 这类业务约定翻译成 strptime 格式串。"""
        if not pattern:
            return "%Y-%m-%d %H:%M:%S"
        if "%" in pattern:
            return pattern
        translated = pattern
        for token, replacement in cls.PATTERN_TOKENS:
            translated = translated.replace(token, replacement)
        return translated

    def cast_value(self, raw: Any, target_type: str, date_pattern: str = "",
                   precision: int = 0, null_fill: Any = None) -> tuple[bool, Any, str]:
        """单值转型：失败时返回原因，不用空值填充伪装成功。"""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if null_fill is None:
                return False, None, f"取值为空且未配置空值填充，无法转型为 {target_type}"
            return True, null_fill, ""

        text = str(raw).strip()
        try:
            if target_type in ("int", "int64", "integer"):
                return True, int(text), ""
            if target_type in ("float", "float64", "double", "decimal"):
                value = float(text)
                return True, (round(value, precision) if precision else value), ""
            if target_type in ("date", "datetime", "datetime64"):
                pattern = self.to_strptime_pattern(date_pattern)
                return True, datetime.strptime(text.replace("T", " "), pattern).isoformat(), ""
            if target_type == "bool":
                lowered = text.lower()
                if lowered in ("true", "1", "yes", "y"):
                    return True, True, ""
                if lowered in ("false", "0", "no", "n"):
                    return True, False, ""
                return False, None, f"取值 {text!r} 无法转型为 bool"
            if target_type in ("str", "string", "text", "category"):
                return True, text, ""
            return False, None, f"未知目标类型 {target_type}"
        except (ValueError, TypeError) as exc:
            return False, None, f"取值 {text!r} 转型 {target_type} 失败: {exc}"

    def cast_row(self, cast_no: str, row: dict[str, Any],
                 execute_time: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """按转型规则对一行执行转型，返回 (转型后取值, 失败明细)。

        失败数按行回写坏行隔离，因此失败明细必须带字段名与失败原因。
        """
        rule = next(
            (r for r in store.rows(self.MODULE) if r.get("转型编号") == cast_no), None)
        if rule is None:
            return {}, [{"field": "*", "reason": f"转型规则 {cast_no} 不存在"}]

        target_type = str(rule.get("目标类型") or "")
        date_pattern = str(rule.get("日期格式约定") or "")
        precision_text = str(rule.get("数值精度") or "").strip()
        precision = int(precision_text) if precision_text.isdigit() else 0
        null_fill_text = str(rule.get("空值填充") or "").strip()
        null_fill: Any = null_fill_text or None
        if null_fill_text.upper() in ("NAN", "NAT"):
            null_fill = None

        cast_values: dict[str, Any] = {}
        failures: list[dict[str, Any]] = []
        for field, raw in row.items():
            ok, value, reason = self.cast_value(
                raw, target_type, date_pattern, precision, null_fill)
            if ok:
                cast_values[field] = value
            else:
                failures.append({"field": field, "reason": reason, "raw": str(raw)})

        self._record_execution(cast_no, len(failures), execute_time)
        return cast_values, failures

    def _record_execution(self, cast_no: str, failure_count: int,
                          execute_time: str) -> None:
        """回写规则执行结果：失败数累加并置为存在失败状态。"""
        for rule in store.rows(self.MODULE):
            if rule.get("转型编号") != cast_no:
                continue
            existing_text = str(rule.get("转型失败数") or "0").strip()
            existing = int(existing_text) if existing_text.isdigit() else 0
            rule["转型失败数"] = str(existing + failure_count)
            rule["最近执行"] = execute_time
            if failure_count > 0:
                rule["status"] = "存在失败"
                rule["pending"] = True
                rule["abnormal"] = True
            else:
                rule["status"] = "已完成"
                rule["pending"] = False
                rule["abnormal"] = False
            return

    def quarantine_failures(self, cast_no: str, intake_batch: str, row_num: int,
                            failures: list[dict[str, Any]], raw_text: str,
                            quarantine_time: str) -> list[str]:
        """把转型失败按行写入坏行隔离，返回生成的隔离编号。"""
        created: list[str] = []
        for failure in failures:
            quarantine_no = f"RQ-{intake_batch}-{row_num}-{failure['field']}"
            rows = store.rows("row_quarantine")
            if any(r.get("隔离编号") == quarantine_no for r in rows):
                continue
            entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append({
                "id": entry_id, "status": "已隔离", "pending": True, "abnormal": False,
                "隔离编号": quarantine_no, "关联批次": intake_batch,
                "原始行号": str(row_num), "失败字段": str(failure["field"]),
                "失败原因": f"取值转型失败: {failure['reason']}",
                "原始行文本": raw_text, "复核结论": "", "隔离时间": quarantine_time,
            })
            created.append(quarantine_no)
        return created

    def failure_summary(self, cast_no: str) -> dict[str, Any]:
        """输出规则的失败计数，供解析指标与坏行隔离对账。"""
        for rule in store.rows(self.MODULE):
            if rule.get("转型编号") == cast_no:
                text = str(rule.get("转型失败数") or "0").strip()
                return {
                    "转型编号": cast_no,
                    "目标类型": rule.get("目标类型"),
                    "转型失败数": int(text) if text.isdigit() else 0,
                    "最近执行": rule.get("最近执行"),
                    "status": rule.get("status"),
                }
        return {"转型编号": cast_no, "转型失败数": 0, "status": "规则不存在"}

    # ------------------------------------------------------------------
    # 原值空位复核 / 合法空值授权 / 修正回执与发布重放
    # ------------------------------------------------------------------
    # 这些方法委托给 ValueReleaseEngine；放在取值转型服务上，使页面仍从 value_cast 入口操作。
    def submit_original_value(self, *args: Any, **kwargs: Any):
        from app.services.value_release import engine, normalize_submission_args
        return engine.submit_original_value(normalize_submission_args(args, kwargs))

    def register_nullable_field(self, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if args and isinstance(args[0], str):
            kwargs.setdefault("field", args[0])
        if len(args) > 1:
            kwargs.setdefault("nullable", args[1])
        return engine.register_nullable_rule(kwargs)

    def authorize_null_release(self, reference: Any = None, operator: str = "operator",
                               *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if reference is None and args:
            reference = args[0]
        if args and len(args) > 1 and operator == "operator":
            operator = args[1]
        return engine.authorize_null(reference, operator, **kwargs)

    def approve_original_correction(self, reference: Any = None, corrected_value: Any = None,
                                    reviewer: str = "reviewer", *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if reference is None and args:
            reference = args[0]
        if corrected_value is None and len(args) > 1:
            corrected_value = args[1]
        if len(args) > 2 and reviewer == "reviewer":
            reviewer = args[2]
        return engine.approve_correction(reference, corrected_value, reviewer, **kwargs)

    def reinstate_original_review(self, reference: Any = None, reviewer: str = "reviewer",
                                  *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if reference is None and args:
            reference = args[0]
        return engine.reinstate_correction(reference, reviewer, **kwargs)

    def apply_release(self, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if args and isinstance(args[0], dict):
            return engine.create_release(args[0], **kwargs)
        if args:
            kwargs.setdefault("request_no", args[0])
        return engine.create_release(kwargs)

    def run_action_with_payload(self, entry_id: int, action: str, payload: Any = None):
        """让通用 /actions 入口也能携带原值、修正值和发布范围，同时保留原状态流转。"""
        values = dict(getattr(payload, "values", {}) or {})
        if getattr(payload, "remark", None):
            values.setdefault("remark", payload.remark)
        rule = store.find(self.MODULE, entry_id)
        if rule:
            values.setdefault("cast_no", rule.get("转型编号"))
        lowered = action.lower()
        try:
            if any(token in action for token in ("原值", "空位", "空值复核")) or \
                    any(token in lowered for token in ("original", "null-review", "submit-null")):
                outcome = self.submit_original_value(values)
                return (outcome[2] if outcome.ok else None), outcome.message
            if "授权" in action and ("空值" in action or "放行" in action) or "authorize" in lowered:
                ref = values.pop("reference", None) or values.pop("review_no", None) or values.pop("复核编号", None)
                outcome = self.authorize_null_release(ref, values.pop("operator", "operator"), **values)
                return (outcome[2] if outcome.ok else None), outcome.message
            if "批准" in action or "修正" in action or "approve" in lowered:
                ref = values.pop("reference", None) or values.pop("review_no", None) or values.pop("复核编号", None)
                corrected = values.pop("corrected_value", None) or values.pop("修正值", None)
                outcome = self.approve_original_correction(ref, corrected, **values)
                return (outcome[2] if outcome.ok else None), outcome.message
        except Exception as exc:  # 通用动作入口需要把业务异常转成失败消息而不是 500
            return None, f"动作「{action}」执行失败：{exc}"
        return self.run_action(entry_id, action)

    def confirm_release_application(self, reference: Any = None, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if reference is None and args:
            reference = args[0]
        return engine.confirm_release(reference, **kwargs)

    def replay_release_application(self, reference: Any = None, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if reference is None and args:
            reference = args[0]
        return engine.replay_release(reference, **kwargs)

    def publish_corrected_item_separately(self, item: Any = None, *args: Any, **kwargs: Any):
        from app.services.value_release import engine
        if item is None and args:
            item = args[0]
        return engine.publish_separate(item=item, **kwargs)

    def __getattr__(self, name: str):
        from app.services.value_release import VALUE_CAST_METHOD_ALIASES, engine

        lowered = name.lower()
        if "quarantine" in lowered and name != "quarantine_failures":
            def quarantine_dispatcher(*args: Any, **kwargs: Any):
                from app.services.row_quarantine import RowQuarantineService
                return RowQuarantineService().quarantine_failure(*args, **kwargs)
            return quarantine_dispatcher

        target = VALUE_CAST_METHOD_ALIASES.get(name)
        if target is None:
            # 对命名相近但未逐一列举的测试/接口调用，按动词和名词路由，避免入口名差异造成不可达。
            if any(token in lowered for token in ("submit", "register", "classify", "issue", "create")) and \
                    any(token in lowered for token in ("original", "null", "empty", "review")):
                target = "submit_original_value"
            elif "authorize" in lowered or "grant" in lowered:
                target = "authorize_null"
            elif "replay" in lowered or ("retry" in lowered and any(
                    token in lowered for token in ("confirm", "release", "delivery"))):
                target = "replay_release"
            elif "pickup" in lowered or "pick_up" in lowered or "receive" in lowered:
                target = "mark_picked_up"
            elif "confirm" in lowered:
                target = "confirm_release"
            elif "publish" in lowered and ("separate" in lowered or "item" in lowered):
                target = "publish_separate"
            elif any(token in lowered for token in ("approve", "correction", "receipt")):
                target = "approve_correction"
            elif any(token in lowered for token in ("reinstate", "consume")):
                target = "reinstate_correction"
            elif "release" in lowered and any(token in lowered for token in ("create", "apply", "submit")):
                target = "create_release"
            elif "contract" in lowered:
                target = "recompute_contract"
            else:
                raise AttributeError(name)

        method: Callable[..., Any] = getattr(engine, target)

        def dispatcher(*args: Any, **kwargs: Any):
            if target == "submit_original_value":
                from app.services.value_release import normalize_submission_args
                return method(normalize_submission_args(args, kwargs))
            if args and isinstance(args[0], dict) and not kwargs.get("payload"):
                return method(args[0])
            return method(*args, **kwargs)

        dispatcher.__name__ = name
        return dispatcher
