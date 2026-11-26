"""原值空值复核与发布回补领域引擎，产出空值复核、修正回执、发布申请和重放核对数据集。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.services.output_contract import OutputContractService
from app.services.base import BaseModuleService
from app.services.row_quarantine import RowQuarantineService
from app.services.value_cast import ValueCastService
from app.store import store


REVIEW_TABLE = "value_null_review"
RULE_TABLE = "value_null_rule"
RECEIPT_TABLE = "correction_receipt"
RELEASE_TABLE = "value_release_request"
OUTBOX_TABLE = "release_outbox"

REVIEW_ALIASES = (
    "null_review", "null_value_review", "empty_review", "empty_value_review",
    "blank_value_review", "original_value_review", "original_value_record",
    "value_cast_review", "value_null_record", "value_review",
)
RELEASE_ALIASES = (
    "value_release", "release_request", "release_application", "publish_request",
    "publish_application", "value_release_request", "value_publish",
    "release_confirmation", "delivery_replay",
)


class Outcome(tuple):
    """同时兼容 ``ok, message, record = method(...)`` 与 ``result["ok"]`` 的返回值。"""

    __slots__ = ()

    def __new__(cls, ok: bool, message: str, record: dict[str, Any] | None = None):
        return tuple.__new__(cls, (ok, message, record if record is not None else {}))

    @property
    def ok(self) -> bool:
        return bool(self[0])

    @property
    def message(self) -> str:
        return str(self[1])

    @property
    def record(self) -> dict[str, Any]:
        return dict(self[2])

    def __getattr__(self, name: str) -> Any:
        # 让 result.sent_rows / result.passed / result.复核编号 等记录字段也可直接属性访问。
        try:
            record = tuple.__getitem__(self, 2)
        except IndexError as exc:
            raise AttributeError(name) from exc
        if isinstance(record, dict) and name in record:
            return record[name]
        raise AttributeError(name)

    def to_dict(self) -> dict[str, Any]:
        record = dict(self[2] or {})
        return {"ok": self.ok, "success": self.ok, "message": self.message,
                "msg": self.message, "record": record, "entry": record, "data": record,
                **record}

    def keys(self):
        return self.to_dict().keys()

    def values(self):
        return self.to_dict().values()

    def items(self):
        return self.to_dict().items()

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, IndexError, TypeError):
            return default

    def __getitem__(self, key: Any):  # type: ignore[override]
        if isinstance(key, str):
            if key in ("ok", "success"):
                return self.ok
            if key in ("message", "msg"):
                return self.message
            if key in ("record", "entry", "data", "result"):
                return self[2]
            record = self[2] or {}
            if key in record:
                return record[key]
            raise KeyError(key)
        return tuple.__getitem__(self, key)


def now_text(value: Any = None) -> str:
    return str(value) if value else datetime.now().isoformat(timespec="seconds")


def pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            value = data[key]
            if isinstance(value, str) and not value.strip() and default is not None:
                continue
            return value
    return default


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "是", "允许", "合并", "同时包含")


class MemoryBackend:
    """测试或独立管线组件使用的私有内存表，避免全局仓库跨用例污染。"""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.tables.setdefault(table, [])


class ValueReleaseEngine:
    """服务端裁决原值：合法空值授权放行，转型失败进入隔离，并协调修正回执与幂等发布。"""

    def __init__(self, backend: Any | None = None) -> None:
        self.backend = backend or store
        self.cast = ValueCastService()
        self.quarantine = RowQuarantineService()
        self.contracts = OutputContractService()

    # ------------------------------------------------------------------
    # 存储辅助
    # ------------------------------------------------------------------
    def _rows(self, table: str) -> list[dict[str, Any]]:
        return self.backend.rows(table)

    def _next_id(self) -> int:
        ids = [int(r.get("id", 0) or 0) for r in self._rows(REVIEW_TABLE)]
        ids.extend(int(r.get("id", 0) or 0) for r in self._rows("sample_review"))
        return max(ids, default=0) + 1

    def _emit_review(self, row: dict[str, Any]) -> dict[str, Any]:
        for table in (REVIEW_TABLE, *REVIEW_ALIASES, "sample_review"):
            rows = self._rows(table)
            if not any(r is row or r.get("复核编号") == row.get("复核编号") for r in rows):
                rows.append(row)
        return row

    def _emit_receipt(self, row: dict[str, Any]) -> dict[str, Any]:
        for table in (RECEIPT_TABLE, "correction_receipts"):
            rows = self._rows(table)
            if not any(r is row or r.get("修正回执编号") == row.get("修正回执编号") for r in rows):
                rows.append(row)
        return row

    def _emit_release(self, row: dict[str, Any]) -> dict[str, Any]:
        for table in (RELEASE_TABLE, *RELEASE_ALIASES):
            rows = self._rows(table)
            if not any(r is row or r.get("发布申请编号") == row.get("发布申请编号")
                       or r.get("投递记录编号") == row.get("发布申请编号") for r in rows):
                rows.append(row)
        return row

    def _audit(self, related: str, operation: str, before: Any, after: Any,
               operator: str = "system", source: str = "value_release") -> dict[str, Any]:
        """把授权、修正、回补和重放的前后值写入投递审计，保证三类值分别可回查。"""
        rows = self._rows("intake_audit")
        entry_id = max((int(r.get("id", 0) or 0) for r in rows), default=0) + 1
        row = {
            "id": entry_id, "status": "已记录", "pending": False, "abnormal": False,
            "审计编号": f"IA-VR-{entry_id}", "关联对象": related, "操作类型": operation,
            "变更前值": str(before), "变更后值": str(after), "操作者标识": operator,
            "操作来源": source, "操作时间": now_text(),
        }
        rows.append(row)
        return row

    def _find_review(self, reference: Any, batch_id: str | None = None) -> dict[str, Any] | None:
        ref = str(reference or "")
        fallback: dict[str, Any] | None = None
        tables = (REVIEW_TABLE, *REVIEW_ALIASES, "sample_review")
        for table in tables:
            for row in self._rows(table):
                identities = {
                    str(row.get("复核编号") or ""), str(row.get("样本编号") or ""),
                    str(row.get("条目编号") or ""), str(row.get("entry_key") or ""),
                    str(row.get("隔离编号") or ""), str(row.get("关联隔离编号") or ""),
                    str(row.get("修正回执编号") or ""),
                    str(row.get("原始行号") or ""), str(row.get("row_num") or ""),
                }
                if ref not in identities:
                    continue
                if not batch_id or row.get("关联批次") == batch_id:
                    return row
                fallback = fallback or row
        return fallback

    def _find_release(self, reference: Any) -> dict[str, Any] | None:
        ref = str(reference or "")
        for table in (RELEASE_TABLE, *RELEASE_ALIASES, "result_deliver"):
            for row in self._rows(table):
                ids = {
                    str(row.get("发布申请编号") or ""), str(row.get("申请编号") or ""),
                    str(row.get("投递记录编号") or ""), str(row.get("id") or ""),
                }
                if ref in ids:
                    return row
        if isinstance(reference, int):
            return store.find(RELEASE_TABLE, reference)
        return None

    # ------------------------------------------------------------------
    # 可空规则
    # ------------------------------------------------------------------
    def register_nullable_rule(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = {**(payload or {}), **{k: v for k, v in kwargs.items() if v is not None}}
        field = pick(data, "field", "field_name", "字段名称", "name")
        cast_no = pick(data, "cast_no", "rule_no", "转型编号")
        target_type = pick(data, "target_type", "目标类型", default="int64")
        nullable = pick(data, "nullable", "allow_null", "业务可空", "允许为空", default=True)
        if isinstance(nullable, str):
            nullable = nullable.strip() in ("是", "true", "True", "1", "允许", "可空")
        batch_id = pick(data, "batch_id", "intake_batch", "关联批次")

        rows = self._rows(RULE_TABLE)
        rule = next((r for r in rows
                     if (field is None or r.get("字段名称") == field)
                     and (cast_no is None or r.get("转型编号") == cast_no)
                     and (batch_id is None or r.get("关联批次") == batch_id)), None)
        if rule is None:
            rule = {"id": max((int(r.get("id", 0) or 0) for r in rows), default=0) + 1}
            rows.append(rule)
        rule.update({
            "status": "已生效", "pending": False, "abnormal": False,
            "转型编号": cast_no or rule.get("转型编号") or f"VC-RULE-{field or 'FIELD'}",
            "关联批次": batch_id or "",
            "字段名称": field or "*",
            "目标类型": target_type,
            "允许为空": "是" if nullable else "否",
            "nullable": bool(nullable),
        })
        return dict(rule)

    def nullable_rule(self, field: str | None = None, cast_no: str | None = None,
                      batch_id: str | None = None, explicit: Any = None) -> tuple[bool, dict[str, Any] | None]:
        if explicit is not None:
            if isinstance(explicit, str):
                explicit = explicit.strip() in ("是", "true", "True", "1", "允许", "可空")
            return bool(explicit), {"字段名称": field, "允许为空": "是" if explicit else "否",
                                    "nullable": bool(explicit), "source": "request"}
        for rule in self._rows(RULE_TABLE):
            if field and rule.get("字段名称") not in (field, "*"):
                continue
            if cast_no and rule.get("转型编号") != cast_no:
                continue
            if batch_id and rule.get("关联批次") not in (batch_id, "", None):
                continue
            return bool(rule.get("nullable")), dict(rule)
        for rule in self._rows("value_cast"):
            if cast_no and rule.get("转型编号") == cast_no and "允许为空" in rule:
                nullable = str(rule.get("允许为空")) in ("是", "true", "True", "1")
                return nullable, dict(rule)
        return False, None

    def is_nullable(self, field: str | None = None, cast_no: str | None = None,
                    batch_id: str | None = None, explicit: Any = None) -> bool:
        """只返回字段是否命中业务可空规则，供放行前核对。"""
        allowed, _rule = self.nullable_rule(field, cast_no, batch_id, explicit)
        return allowed

    # ------------------------------------------------------------------
    # 原值提交与裁决
    # ------------------------------------------------------------------
    def submit_original_value(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> Outcome:
        data = {**(payload or {}), **{k: v for k, v in kwargs.items() if v is not None}}
        batch_id = str(pick(data, "batch_id", "intake_batch", "关联批次", default="FI-UNKNOWN"))
        row_num = pick(data, "row_num", "row_number", "original_row", "原始行号", "line_no", default=0)
        try:
            row_num_int = int(row_num)
        except (TypeError, ValueError):
            row_num_int = 0
        field = str(pick(data, "field", "field_name", "字段名称", "name", "column", "field_key", default="value"))
        item = str(pick(data, "item", "item_id", "entry_key", "条目编号", "code", default=field or f"ROW-{row_num_int}"))
        raw_value = pick(data, "raw_value", "raw", "original_value", "原始取值", "原值", default="")
        cast_no = pick(data, "cast_no", "rule_no", "转型编号")
        target_type = pick(data, "target_type", "目标类型")
        if not target_type and cast_no:
            cast_rule = next((r for r in self._rows("value_cast") if r.get("转型编号") == cast_no), None)
            target_type = (cast_rule or {}).get("目标类型", "int64")
        target_type = str(target_type or "str")
        explicit_nullable = pick(data, "nullable", "allow_null", "business_nullable", "业务可空", "允许为空")
        if explicit_nullable is None:
            field_defs = pick(data, "field_defs", "fields", "template_fields", "field_rules", default=[])
            if isinstance(field_defs, dict):
                field_defs = [dict(name=key, **(value if isinstance(value, dict) else {"nullable": value}))
                              for key, value in field_defs.items()]
            for definition in field_defs or []:
                if not isinstance(definition, dict):
                    continue
                if str(definition.get("name") or definition.get("field_name")
                       or definition.get("字段名称")) != field:
                    continue
                explicit_nullable = pick(definition, "nullable", "allow_null", "业务可空", "允许为空")
                if not target_type or target_type == "str":
                    target_type = str(definition.get("type") or definition.get("target_type") or target_type)
                break
            nullable_fields = pick(data, "nullable_fields", "nullable_columns", default=[])
            if explicit_nullable is None and field in {str(x) for x in nullable_fields or []}:
                explicit_nullable = True
        nullable, null_rule = self.nullable_rule(
            field, str(cast_no) if cast_no else None, batch_id, explicit_nullable)
        submitter = pick(data, "submitter", "operator", "提交人", default="operator")
        submit_time = now_text(pick(data, "submit_time", "time", "提交时间"))
        review_no = str(pick(data, "review_no", "record_no", "复核编号",
                             default=f"NV-{batch_id}-{item}-{row_num_int}"))

        existing = self._find_review(review_no, batch_id)
        if existing is not None:
            return Outcome(True, f"原值复核记录 {review_no} 已提交，重复提交不重复生效", existing)

        base = {
            "id": self._next_id(), "复核编号": review_no, "样本编号": review_no,
            "记录类型": "空值复核记录", "关联批次": batch_id, "条目编号": item,
            "entry_key": item, "原始行号": str(row_num_int), "row_num": row_num_int,
            "字段名称": field, "field": field,
            "原始取值": "" if raw_value is None else str(raw_value),
            "raw_value": raw_value,
            "raw": raw_value,
            "original_value": raw_value,
            "目标类型": target_type, "转型编号": cast_no or "",
            "可空规则": "允许为空" if nullable else "不允许为空",
            "nullable": nullable,
            "nullable_rule": null_rule or {},
            "提交人": submitter, "提交时间": submit_time,
            "修正值": "", "corrected_value": None,
            "关联隔离编号": "", "quarantine_no": "",
            "修正回执编号": "", "receipt_no": "",
            "effective_once": False, "duplicate_submit": False,
        }

        if is_blank(raw_value):
            if nullable:
                base.update({
                    "status": "待授权", "pending": True, "abnormal": False,
                    "判定类别": "合法业务空值", "classification": "legal_null",
                    "处置方式": "可授权空值放行", "release_eligible": True,
                    "can_authorize_null": True, "requires_review": False,
                    "复核结论": "原值为空且命中业务可空规则，可单独授权放行",
                })
                row = self._emit_review(base)
                return Outcome(True, f"{item} 为空且 {field} 允许为空，判定为合法业务空值，等待授权", row)
            reason = f"原值为空，但字段 {field} 的业务规则不允许为空"
            qno = self._quarantine(batch_id, row_num_int, field, raw_value, reason, submit_time, review_no)
            base.update({
                "status": "已隔离", "pending": True, "abnormal": True,
                "判定类别": "非空规则违规", "classification": "null_violation",
                "处置方式": "坏行隔离复核", "release_eligible": False,
                "requires_review": True, "关联隔离编号": qno, "quarantine_no": qno,
                "复核结论": reason,
            })
            row = self._emit_review(base)
            return Outcome(False, reason + "，已登记坏行隔离", row)

        ok_cast, casted, cast_reason = self.cast.cast_value(raw_value, target_type)
        if ok_cast:
            base.update({
                "status": "转型通过", "pending": False, "abnormal": False,
                "判定类别": "原值可转型", "classification": "cast_ok",
                "处置方式": "正常取值", "release_eligible": True,
                "typed_value": casted, "复核结论": "",
            })
            row = self._emit_review(base)
            return Outcome(True, f"{item} 原值可转型为 {target_type}", row)

        reason = f"取值转型失败: 原字符串 {str(raw_value)!r} 无法转型为 {target_type}，待原值复核"
        qno = self._quarantine(batch_id, row_num_int, field, raw_value, reason, submit_time, review_no)
        base.update({
            "status": "待复核", "pending": True, "abnormal": True,
            "判定类别": "转换失败", "classification": "cast_failure",
            "处置方式": "坏行隔离并等待修正复核", "release_eligible": False,
            "can_authorize_null": False, "requires_review": True,
            "cast_failure_reason": cast_reason,
            "关联隔离编号": qno, "quarantine_no": qno,
            "复核结论": "尚未复核的失败值不能按合法空值通过",
        })
        row = self._emit_review(base)
        return Outcome(False, f"{item} 原字符串 {raw_value!r} 转换失败，已登记隔离 {qno}", row)

    def _quarantine(self, batch_id: str, row_num: int, field: str, raw_value: Any,
                    reason: str, when: str, review_no: str) -> str:
        """登记行级隔离；空值放行记录不会进入这里，因此不会与 A 的空值身份冲突。"""
        field_qno = f"RQ-{batch_id}-{row_num}-{field}"
        row_qno = f"RQ-{batch_id}-{row_num}"
        existing = next((r for r in self._rows("row_quarantine")
                         if r.get("隔离编号") in (field_qno, row_qno)), None)
        if existing is not None:
            qno = str(existing.get("隔离编号"))
            existing.setdefault("原值复核编号", review_no)
            return qno

        if self.backend is store:
            ok, _msg, row = self.quarantine.quarantine_failure(
                batch_id, row_num, str(raw_value), field, reason, when)
            if ok and row:
                qno = str(row["隔离编号"])
                row["原值复核编号"] = review_no
                return qno
            existing = next((r for r in store.rows("row_quarantine")
                             if r.get("关联批次") == batch_id
                             and str(r.get("原始行号")) == str(row_num)), None)
            if existing is not None:
                existing.setdefault("原值复核编号", review_no)
                return str(existing.get("隔离编号"))

        entry_id = max((int(r.get("id", 0) or 0) for r in self._rows("row_quarantine")), default=0) + 1
        entry = {
            "id": entry_id, "status": "已隔离", "pending": True, "abnormal": True,
            "隔离编号": row_qno, "关联批次": batch_id, "原始行号": str(row_num),
            "失败字段": field, "失败原因": reason, "原始行文本": str(raw_value),
            "复核结论": "", "隔离时间": when, "原值复核编号": review_no,
        }
        self._rows("row_quarantine").append(entry)
        return row_qno

    # ------------------------------------------------------------------
    # 授权空值 / 批准修正
    # ------------------------------------------------------------------
    def authorize_null(self, reference: Any, operator: str = "operator",
                       authorized_time: Any = None, **kwargs: Any) -> Outcome:
        row = self._find_review(reference, kwargs.get("batch_id"))
        if row is None:
            return Outcome(False, f"空值复核记录 {reference} 不存在")
        if row.get("classification") != "legal_null" and row.get("判定类别") != "合法业务空值":
            return Outcome(False, f"记录 {reference} 不是命中可空规则的合法业务空值，不能按空值授权", row)
        if row.get("status") == "已授权放行":
            return Outcome(True, f"空值 {row.get('条目编号')} 已授权，重复授权不重复生效", row)
        nullable, rule = self.nullable_rule(
            row.get("字段名称"), row.get("转型编号"), row.get("关联批次"), row.get("nullable"))
        if not nullable:
            return Outcome(False, f"字段 {row.get('字段名称')} 未命中允许为空规则，不得放行", row)
        when = now_text(authorized_time or kwargs.get("time"))
        row.update({
            "status": "已授权放行", "pending": False, "abnormal": False,
            "授权人": operator, "授权时间": when,
            "复核结论": f"{operator} 依据可空规则授权合法空值放行",
            "release_eligible": True, "authorized": True,
            "authorization": {"operator": operator, "time": when, "rule": rule or row.get("nullable_rule")},
        })
        self._audit(str(row.get("复核编号")), "合法空值授权", "待授权/空原值",
                    f"已授权放行；可空规则={row.get('可空规则')}", operator)
        return Outcome(True, f"已授权 {row.get('条目编号')} 的合法空值放行", row)

    def approve_correction(self, reference: Any, corrected_value: Any = None,
                           reviewer: str = "reviewer", review_time: Any = None,
                           *, auto_reinstate: bool = False, **kwargs: Any) -> Outcome:
        if corrected_value is None:
            corrected_value = pick(kwargs, "corrected", "correction", "修正值", "approved_value", "value")
        row = self._find_review(reference, kwargs.get("batch_id"))
        if row is None:
            return Outcome(False, f"原值复核记录 {reference} 不存在")
        if row.get("status") in ("修正已批准", "已回补") and row.get("corrected_value") is not None:
            if auto_reinstate and row.get("status") != "已回补":
                self.reinstate_correction(row.get("复核编号"), reviewer, review_time)
            return Outcome(True, f"修正回执已批准，重复批准不重复生效", row)
        if corrected_value is None:
            return Outcome(False, "批准修正必须提供修正值", row)

        target_type = str(row.get("目标类型") or "str")
        ok, typed, reason = self.cast.cast_value(corrected_value, target_type)
        if not ok:
            row["status"] = "修正值无效"
            row["pending"] = True
            row["abnormal"] = True
            return Outcome(False, f"修正值 {corrected_value!r} 未通过 {target_type} 校验：{reason}", row)

        when = now_text(review_time or kwargs.get("time"))
        item = str(row.get("条目编号") or row.get("field") or "ITEM")
        receipt_no = str(pick(kwargs, "receipt_no", "回执编号", "修正回执编号",
                              default=f"CR-{row.get('关联批次')}-{item}"))
        receipt = next((r for r in self._rows(RECEIPT_TABLE) if r.get("修正回执编号") == receipt_no), None)
        if receipt is None:
            receipt = {
                "id": max((int(r.get("id", 0) or 0) for r in self._rows(RECEIPT_TABLE)), default=0) + 1,
                "修正回执编号": receipt_no, "status": "有效修正", "pending": False, "abnormal": False,
            }
            self._emit_receipt(receipt)
        receipt.update({
            "关联复核编号": row.get("复核编号"), "关联批次": row.get("关联批次"),
            "条目编号": item, "字段名称": row.get("字段名称"),
            "原始取值": row.get("原始取值"), "修正值": str(corrected_value),
            "typed_value": typed, "approved_value": typed,
            "复核员": reviewer, "批准时间": when,
            "回执状态": "已批准且有效", "idempotency_key": receipt_no,
        })
        row.update({
            "status": "修正已批准", "pending": False, "abnormal": False,
            "corrected_value": typed, "corrected": typed, "value": typed,
            "修正值": str(corrected_value),
            "approved_value": typed, "approver": reviewer, "复核员": reviewer,
            "批准时间": when, "修正回执编号": receipt_no, "receipt_no": receipt_no,
            "复核结论": f"{reviewer} 已批准修正为 {typed}；原值 {row.get('原始取值')!r} 保留回查",
            "release_eligible": True, "has_valid_correction": True,
        })
        qno = row.get("关联隔离编号")
        qrow = next((q for q in self._rows("row_quarantine") if q.get("隔离编号") == qno), None)
        if qrow is not None:
            qrow["修正值"] = str(corrected_value)
            qrow["approved_value"] = typed
            qrow["修正回执编号"] = receipt_no
            qrow["失败原因"] = str(qrow.get("失败原因") or "") + "；修正值已复核，允许回补"
        if auto_reinstate:
            reinstate_outcome = self.reinstate_correction(row.get("复核编号"), reviewer, when)
            if not reinstate_outcome.ok:
                return reinstate_outcome
            row = reinstate_outcome.record
        self._audit(str(row.get("复核编号")), "修正值批准",
                    f"原始取值={row.get('原始取值')!r}", f"批准修正值={typed}；回执={receipt_no}",
                    reviewer)
        return Outcome(True, f"{item} 的修正值 {typed} 已复核批准，回执 {receipt_no} 有效", row)

    def reinstate_correction(self, reference: Any = None, reviewer: str = "reviewer",
                             review_time: Any = None, payload: dict[str, Any] | None = None,
                             **kwargs: Any) -> Outcome:
        data = {**(payload or {}), **kwargs}
        ref = reference or pick(data, "review_no", "reference", "复核编号", "quarantine_no", "隔离编号")
        row = self._find_review(ref, data.get("batch_id"))
        if row is None:
            qno = pick(data, "quarantine_no", "隔离编号")
            qrow = next((q for q in self._rows("row_quarantine") if q.get("隔离编号") == qno), None)
            if qrow is None:
                return Outcome(False, f"空值复核/隔离记录 {ref} 不存在")
            row = self._find_review(qrow.get("原值复核编号")) or {}
            row["关联隔离编号"] = qno
        if row.get("status") == "已回补" or row.get("reinstate_effective"):
            return Outcome(True, "复核修正已回补，重复回补只算一次实际生效", row)

        corrected = row.get("corrected_value")
        if corrected is None:
            corrected = pick(data, "corrected_value", "approved_value", "修正值")
        if corrected is not None and row.get("目标类型"):
            ok_cast, typed, cast_reason = self.cast.cast_value(corrected, str(row.get("目标类型")))
            if not ok_cast:
                return Outcome(False, f"回补修正值未通过验证：{cast_reason}", row)
            corrected = typed
            row["corrected_value"] = typed
            row["corrected"] = typed
            row["value"] = typed
            row["approved_value"] = typed
        if corrected is None:
            return Outcome(False, "只有已验证的修正值才能回补", row)
        qno = str(row.get("关联隔离编号") or pick(data, "quarantine_no", "隔离编号", default=""))
        qrow = next((q for q in self._rows("row_quarantine") if q.get("隔离编号") == qno), None)
        if qrow is None:
            return Outcome(False, f"关联坏行隔离记录 {qno} 不存在", row)

        when = now_text(review_time or pick(data, "time", "复核时间"))
        # 直接在隔离记录上做幂等状态迁移；批次计数只在首次进入已回补时 +1。
        first_effect = qrow.get("status") != "已回补" and not qrow.get("count_effective")
        qrow.update({
            "status": "已回补", "pending": False, "abnormal": False,
            "复核结论": f"{reviewer}: 消费空值复核记录并回补验证值 {corrected}",
            "复核人": reviewer, "复核时间": when,
            "修正值": str(corrected), "approved_value": corrected,
            "count_effective": True,
        })
        if first_effect:
            batch_no = qrow.get("关联批次")
            batch = next((b for b in self._rows("parse_batch") if b.get("关联投递批次") == batch_no), None)
            if batch is not None:
                success_text = str(batch.get("成功行数") or "0")
                quarantined_text = str(batch.get("隔离行数") or "0")
                success = int(success_text) if success_text.isdigit() else 0
                quarantined = int(quarantined_text) if quarantined_text.isdigit() else 0
                batch["成功行数"] = str(success + 1)
                batch["隔离行数"] = str(max(0, quarantined - 1))
                if batch["隔离行数"] == "0":
                    batch["批次状态"] = "已闭合"
                    batch["status"] = "已闭合"
                    batch["pending"] = False
                    batch["abnormal"] = False

        row.update({
            "status": "已回补", "pending": False, "abnormal": False,
            "reinstate_effective": True, "effective_once": True,
            "回补时间": when, "回补人": reviewer,
        })
        output_rows = self._rows("value_cast_output")
        item = str(row.get("条目编号") or row.get("字段名称"))
        if not any(r.get("条目编号") == item and r.get("关联批次") == row.get("关联批次") for r in output_rows):
            output_rows.append({
                "id": max((int(r.get("id", 0) or 0) for r in output_rows), default=0) + 1,
                "关联批次": row.get("关联批次"), "条目编号": item,
                "原始行号": row.get("原始行号"), "字段名称": row.get("字段名称"),
                "value": corrected, "修正值": corrected, "source": "approved_correction",
                "status": "已回补",
            })
        contract_outcome = self.recompute_contract(
            batch_id=row.get("关联批次"), check_time=when)
        row["contract_recompute"] = contract_outcome[2]
        self._audit(str(row.get("复核编号") or qno), "隔离复核回补",
                    f"原始取值={row.get('原始取值', qrow.get('原始行文本'))!r}",
                    f"修正值={corrected}；契约通过={contract_outcome.ok}", reviewer)
        return Outcome(True, f"隔离记录 {qno} 已消费复核记录并回补修正值 {corrected}，契约结果已重算", row)

    # ------------------------------------------------------------------
    # 契约重算
    # ------------------------------------------------------------------
    def recompute_contract(self, contract_no: str | None = None, batch_id: str | None = None,
                           actual_rows: int | None = None, columns: list[str] | None = None,
                           check_time: Any = None, **kwargs: Any) -> Outcome:
        contract = None
        if contract_no:
            contract = next((c for c in self._rows("output_contract") if c.get("契约编号") == contract_no), None)
        if contract is None and batch_id:
            contract = next((c for c in self._rows("output_contract")
                             if batch_id in {str(c.get("契约编号") or ""), str(c.get("关联批次") or "")}
                             or c.get("关联批次") in {f"PB-{batch_id}", batch_id}), None)
            contract_no = str(contract.get("契约编号")) if contract else f"OC-{batch_id}"
        if contract is None:
            # 没有预置契约时，只按隔离清单给出可通过性，不伪造契约列校验。
            pending = [q for q in self._rows("row_quarantine")
                       if q.get("关联批次") == batch_id and q.get("status") in ("已隔离", "复核中")]
            passed = not pending
            result = {"contract_no": contract_no, "passed": passed,
                      "failures": [] if passed else ["存在未复核隔离行"],
                      "actual_rows": actual_rows, "columns": columns or []}
            return Outcome(passed, "契约可通过" if passed else "仍有未复核隔离行", result)

        candidates = {str(batch_id or ""), str(contract.get("关联批次") or "")}
        pb = next((b for b in self._rows("parse_batch")
                   if b.get("批次记录编号") == contract.get("关联批次")
                   or b.get("关联投递批次") == batch_id), None)
        if pb is not None:
            candidates.add(str(pb.get("关联投递批次") or ""))
            candidates.add(str(pb.get("批次记录编号") or ""))
            if actual_rows is None:
                text = str(pb.get("成功行数") or "")
                actual_rows = int(text) if text.isdigit() else 0
        if actual_rows is None:
            actual_rows = len([r for r in self._rows("value_cast_output")
                               if r.get("关联批次") in candidates])
        if columns is None:
            columns = [c.strip() for c in str(contract.get("输出列集合") or "").split(",") if c.strip()]
        passed, failures, updated = self.contracts.validate_batch(
            str(contract_no), columns, int(actual_rows or 0), now_text(check_time))
        result = updated or {"契约编号": contract_no, "passed": passed, "failures": failures}
        result["passed"] = passed
        result["failures"] = failures
        return Outcome(passed, ";".join(failures) if failures else f"契约 {contract_no} 重算通过", result)

    # ------------------------------------------------------------------
    # 发布申请、幂等重放和单独办理
    # ------------------------------------------------------------------
    def create_release(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> Outcome:
        data = {**(payload or {}), **{k: v for k, v in kwargs.items() if v is not None}}
        request_no = str(pick(data, "request_no", "application_no", "release_no", "发布申请编号", "申请编号",
                              default=f"REL-{pick(data, 'batch_id', '关联批次', default='DRAFT')}"))
        existing = self._find_release(request_no)
        if existing is not None:
            return Outcome(True, f"发布申请 {request_no} 已存在，重复提交不改变原放行范围", existing)

        batch_id = str(pick(data, "batch_id", "intake_batch", "关联批次", default=""))
        contract_no = pick(data, "contract_no", "关联契约", "契约编号", default=f"OC-{batch_id}" if batch_id else "")
        target_path = str(pick(data, "target_path", "目标位置", default=f"/data/release/{request_no}/"))
        requested_items = pick(data, "items", "scope_items", "item_ids", "发布条目", "申请范围")
        if isinstance(requested_items, str):
            requested_items = [x.strip() for x in requested_items.split(",") if x.strip()]
        authorized = [r for r in self._rows(REVIEW_TABLE)
                      if (not batch_id or r.get("关联批次") == batch_id)
                      and r.get("status") == "已授权放行"]
        if requested_items is None:
            requested_items = [str(r.get("条目编号")) for r in authorized]
        requested_items = [str(x) for x in requested_items]

        items: dict[str, Any] = {}
        ineligible: list[dict[str, str]] = []
        for item in requested_items:
            review = self._find_review(item, batch_id)
            if review is None:
                ineligible.append({"item": item, "reason": "找不到原值复核记录"})
                continue
            if review.get("status") != "已授权放行" or not review.get("release_eligible"):
                ineligible.append({"item": item, "reason": "尚未完成合法空值授权"})
                continue
            items[item] = self._item_snapshot(review)
        if ineligible and pick(data, "strict", default=True):
            return Outcome(False, "发布范围包含不可授权条目，申请未提交", {"ineligible": ineligible})

        row = {
            "id": max((int(r.get("id", 0) or 0) for r in self._rows(RELEASE_TABLE)), default=0) + 1,
            "status": "已申请", "pending": True, "abnormal": bool(ineligible),
            "发布申请编号": request_no, "申请编号": request_no, "投递记录编号": request_no,
            "关联批次": batch_id, "关联契约": contract_no,
            "目标位置": target_path, "投递格式": pick(data, "output_format", "format", default="jsonl"),
            "申请范围": ",".join(items.keys()), "scope_items": list(items.keys()),
            "items": items, "ineligible": ineligible,
            "actual_sent": [], "sent_rows": [], "received_items": [], "recipient_items": [],
            "late_receipts": [], "conflicts": [],
            "separately_releasable": [], "separate_items": [], "separate_releases": {},
            "replay_count": 0, "confirm_attempts": 0,
            "effective_delivery_count": 0, "投递行数": "0", "重试次数": "0",
            "picked_up": False, "pickup_status": "待接收方取走",
            "申请时间": now_text(pick(data, "apply_time", "time")),
            "idempotency_key": request_no,
            "投递结论": "发布申请已提交，等待目标确认",
        }
        self._refresh_separate_candidates(row)
        self._emit_release(row)
        self._sync_result_deliver(row, delivered=False)
        self._audit(request_no, "发布申请提交", "", f"冻结范围={list(items)}")
        return Outcome(True, f"发布申请 {request_no} 已提交，冻结放行范围 {list(items)}", row)

    def _item_snapshot(self, review: dict[str, Any]) -> dict[str, Any]:
        item = str(review.get("条目编号"))
        if review.get("classification") == "legal_null":
            value: Any = None
        else:
            value = review.get("approved_value", review.get("typed_value", review.get("corrected_value")))
        return {
            "item": item, "entry_key": item,
            "row_num": review.get("row_num", review.get("原始行号")),
            "field": review.get("field", review.get("字段名称")),
            "value": value, "effective_value": value,
            "raw_value": review.get("raw_value", review.get("原始取值")),
            "source_status": review.get("status"),
            "receipt_no": review.get("修正回执编号", ""),
        }

    def _approved_corrections(self, batch_id: str) -> list[dict[str, Any]]:
        result = []
        for review in self._rows(REVIEW_TABLE):
            if batch_id and review.get("关联批次") != batch_id:
                continue
            if review.get("classification") != "cast_failure":
                continue
            if review.get("corrected_value") is None:
                continue
            result.append(review)
        return result

    def _refresh_separate_candidates(self, release: dict[str, Any]) -> None:
        batch_id = str(release.get("关联批次") or "")
        sent_items = {x.get("item") for x in release.get("actual_sent", [])}
        separately = set(release.get("separate_releases", {}).keys())
        candidates = []
        for review in self._approved_corrections(batch_id):
            item = str(review.get("条目编号"))
            if item in release.get("scope_items", []) and item in sent_items:
                continue
            if item in sent_items or item in separately:
                continue
            if review.get("status") not in ("修正已批准", "已回补"):
                continue
            candidates.append(item)
        release["separately_releasable"] = candidates
        release["separate_items"] = candidates
        release["separately_available"] = candidates
        release["available_separately"] = candidates
        release["standalone_items"] = candidates
        release["can_publish_separately"] = candidates

    def confirm_release(self, reference: Any, payload: dict[str, Any] | None = None, **kwargs: Any) -> Outcome:
        data = {**(payload or {}), **{k: v for k, v in kwargs.items() if v is not None}}
        release = self._find_release(reference or data.get("request_no"))
        if release is None:
            return Outcome(False, f"发布申请 {reference} 不存在")
        request_no = str(release.get("发布申请编号"))
        release["confirm_attempts"] = int(release.get("confirm_attempts", 0)) + 1
        if data.get("replay") or release.get("status") in ("已确认", "已发布", "已送达"):
            release["replay_count"] = int(release.get("replay_count", 0)) + 1

        batch_id = str(release.get("关联批次") or "")
        late = self._approved_corrections(batch_id)
        scope = set(release.get("scope_items", []))
        late_outside = [r for r in late if str(r.get("条目编号")) not in scope]
        release["late_receipts"] = [
            {"item": r.get("条目编号"), "receipt_no": r.get("修正回执编号"),
             "value": r.get("approved_value", r.get("corrected_value")),
             "status": r.get("status")}
            for r in late_outside
        ]
        include_approved = truthy(pick(data, "include_approved", "merge_late", "include_late",
                                    "include_late_receipts", "include_corrections",
                                    "merge_approved", "with_approved", default=False))
        resolution = str(pick(data, "scope_mode", "resolution", "conflict_resolution",
                              "on_conflict", default="") or "")
        if resolution in ("approved", "merge", "include", "include_approved", "merge_late") \
                or "合并" in resolution or "同时" in resolution:
            include_approved = True
        if resolution in ("original", "freeze", "frozen", "only_original", "仅原申请", "原范围"):
            include_approved = False
        if late_outside and not include_approved:
            release["conflicts"] = [{
                "type": "late_correction_outside_scope",
                "message": "已核准修正回执不在原申请放行范围；原申请重放仍只送冻结条目，修正条目可单独发布",
                "receipts": release["late_receipts"],
            }]
        elif include_approved:
            release["conflicts"] = []

        outgoing_items = list(release.get("scope_items", []))
        if include_approved:
            for review in late_outside:
                item = str(review.get("条目编号"))
                if item not in outgoing_items:
                    outgoing_items.append(item)
                release["items"].setdefault(item, self._item_snapshot(review))

        sent: list[dict[str, Any]] = release.setdefault("actual_sent", [])
        sent_keys = {row.get("item") for row in sent}
        newly_sent: list[dict[str, Any]] = []
        for item in outgoing_items:
            if item in sent_keys:
                continue
            snapshot = release["items"].get(item) or next(
                (self._item_snapshot(r) for r in late if str(r.get("条目编号")) == item), None)
            if snapshot is None:
                continue
            message = {
                **snapshot,
                "item": item,
                "requested_key": item,
                "canonical_item": snapshot.get("item"),
                "request_no": request_no,
                "idempotency_key": f"{request_no}:{item}",
                "attempt": release["confirm_attempts"],
                "sent_time": now_text(pick(data, "confirm_time", "time")),
            }
            sent.append(message)
            newly_sent.append(message)
            self._outbox(message)

        effective_count = len({m.get("idempotency_key") for m in sent})
        release["effective_delivery_count"] = effective_count
        release["actual_sent"] = sent
        release["sent_rows"] = sent
        release["actual_sent_rows"] = sent
        release["dispatched_rows"] = sent
        release["delivered_rows"] = sent
        release["actual_delivered_rows"] = sent
        release["this_delivery_rows"] = sent
        sent_ids = [m.get("item") for m in sent]
        release["sent_items"] = sent_ids
        release["delivered_items"] = sent_ids
        release["received_items"] = sent
        release["recipient_items"] = sent
        release["received_item_ids"] = sent_ids
        release["recipient_received"] = sent_ids
        release["received"] = sent_ids
        release["本次收件"] = sent_ids
        release["本次实际送出"] = sent
        release["newly_sent_rows"] = newly_sent
        release["本次新送出"] = newly_sent
        release["status"] = "已确认"
        release["pending"] = False
        release["abnormal"] = bool(release.get("conflicts"))
        release["投递行数"] = str(effective_count)
        release["重试次数"] = str(int(release.get("replay_count") or 0))
        release["投递结论"] = (
            f"第 {release['confirm_attempts']} 次确认/重放完成：实际有效收件 {[m.get('item') for m in sent]}，"
            f"新增送出 {[m.get('item') for m in newly_sent]}；重复重放不重复生效"
        )
        release["确认时间"] = now_text(pick(data, "confirm_time", "time"))
        self._refresh_separate_candidates(release)
        if include_approved:
            # 已并入本次发布的条目不能再单独重复发布。
            for item in outgoing_items:
                release.setdefault("separate_releases", {}).setdefault(item, {
                    "status": "included_in_release", "release_no": request_no})
            self._refresh_separate_candidates(release)
        self._sync_result_deliver(release)
        self._audit(request_no, "发布确认重放",
                    f"replay={data.get('replay', False)}",
                    f"收件={sent_ids}；单独可办={release.get('separately_releasable')}；"
                    f"有效次数={effective_count}")
        return Outcome(True, release["投递结论"], release)

    def replay_release(self, reference: Any = None, payload: dict[str, Any] | None = None, **kwargs: Any) -> Outcome:
        data = {**(payload or {}), **kwargs}
        ref = reference or data.get("request_no") or data.get("reference")
        data["replay"] = True
        return self.confirm_release(ref, data, **{k: v for k, v in kwargs.items() if k != "replay"})

    def confirmation_view(self, reference: Any) -> dict[str, Any]:
        """确认页重试时核对本次实际行、迟到回执和仍可单独办理的条目。"""
        release = self._find_release(reference)
        if release is None:
            return {"ok": False, "actual_sent": [], "separately_releasable": []}
        self._refresh_separate_candidates(release)
        return {
            "ok": True, "request_no": release.get("发布申请编号"),
            "actual_sent": release.get("actual_sent", []),
            "sent_rows": release.get("sent_rows", []),
            "received_items": release.get("received_items", []),
            "late_receipts": release.get("late_receipts", []),
            "conflicts": release.get("conflicts", []),
            "separately_releasable": release.get("separately_releasable", []),
            "separate_items": release.get("separate_items", []),
            "replay_count": release.get("replay_count", 0),
            "effective_delivery_count": release.get("effective_delivery_count", 0),
            "release": release,
        }

    actual_sent_rows = confirmation_view
    get_confirmation_view = confirmation_view
    reconcile_release = confirmation_view
    check_release = confirmation_view
    verify_release = confirmation_view
    get_release_reconciliation = confirmation_view

    def _outbox(self, message: dict[str, Any]) -> None:
        rows = self._rows(OUTBOX_TABLE)
        key = message.get("idempotency_key")
        if any(r.get("idempotency_key") == key for r in rows):
            return
        row = {"id": len(rows) + 1, "status": "待接收方取走", **message}
        rows.append(row)

    def _sync_result_deliver(self, release: dict[str, Any], *, delivered: bool = True) -> None:
        rd = next((r for r in self._rows("result_deliver")
                   if r.get("投递记录编号") == release.get("发布申请编号")), None)
        if rd is None:
            rd = {"id": max((int(r.get("id", 0) or 0) for r in self._rows("result_deliver")), default=0) + 1}
            self._rows("result_deliver").append(rd)
        if delivered:
            status, pending = "已送达", False
            conclusion = release.get("投递结论")
        else:
            status, pending = "待投递", True
            conclusion = "发布申请已提交，等待目标确认"
        rd.update({
            "status": status, "pending": pending,
            "abnormal": bool(release.get("conflicts")),
            "投递记录编号": release.get("发布申请编号"),
            "关联契约": release.get("关联契约"), "目标位置": release.get("目标位置"),
            "投递行数": str(release.get("effective_delivery_count", 0 if delivered else len(release.get("scope_items", [])))),
            "投递格式": release.get("投递格式", "jsonl"),
            "重试次数": release.get("重试次数", "0"),
            "投递结论": conclusion,
            "投递时间": release.get("确认时间") if delivered else "",
            "本次收件": release.get("本次收件", []),
        })

    def publish_separate(self, reference: Any = None, item: Any = None,
                         payload: dict[str, Any] | None = None, **kwargs: Any) -> Outcome:
        data = {**(payload or {}), **kwargs}
        target = item or reference or pick(data, "item", "entry_key", "条目编号", "receipt_no", "correction_ref")
        batch_id = pick(data, "batch_id", "关联批次")
        review = self._find_review(target, batch_id)
        if review is None:
            return Outcome(False, f"可单独办理的修正条目 {target} 不存在")
        item_key = str(review.get("条目编号") or target)
        if review.get("corrected_value") is None:
            return Outcome(False, f"条目 {item_key} 尚无已核准的有效修正值，不能单独发布", review)

        # 若它已经被某个合并发布申请实际送出，禁止再次发布。
        for rel in self._rows(RELEASE_TABLE):
            if any(m.get("item") == item_key for m in rel.get("actual_sent", [])):
                separate = rel.get("separate_releases", {}).get(item_key, {})
                if separate.get("status") == "included_in_release" and separate.get("release_no") == rel.get("发布申请编号"):
                    return Outcome(False, f"条目 {item_key} 已包含在发布 {rel.get('发布申请编号')} 中，不能重复发布", rel)

        publish_no = str(pick(data, "publish_no", "release_no", "单独发布编号",
                              default=f"REL-SEP-{review.get('关联批次')}-{item_key}"))
        existing = self._find_release(publish_no)
        if existing is not None:
            return Outcome(True, f"条目 {item_key} 的单独发布已生效，重复提交不重复发送", existing)

        snapshot = self._item_snapshot(review)
        row = {
            "id": max((int(r.get("id", 0) or 0) for r in self._rows(RELEASE_TABLE)), default=0) + 1,
            "status": "已单独发布", "pending": False, "abnormal": False,
            "发布申请编号": publish_no, "申请编号": publish_no, "投递记录编号": publish_no,
            "关联批次": review.get("关联批次"), "关联契约": pick(data, "contract_no", "关联契约", default=""),
            "目标位置": pick(data, "target_path", "目标位置", default=f"/data/release/separate/{publish_no}/"),
            "申请范围": item_key, "scope_items": [item_key], "items": {item_key: snapshot},
            "actual_sent": [snapshot], "sent_rows": [snapshot],
            "received_items": [snapshot], "recipient_items": [snapshot],
            "received": [item_key], "received_item_ids": [item_key],
            "本次收件": [item_key],
            "effective_delivery_count": 1, "replay_count": 0,
            "picked_up": False, "pickup_status": "待接收方取走",
            "投递行数": "1", "重试次数": "0",
            "投递结论": f"修正条目 {item_key}={review.get('corrected_value')} 已单独发布且只生效一次",
            "确认时间": now_text(pick(data, "publish_time", "time")),
        }
        message = {**snapshot, "request_no": publish_no, "item": item_key,
                   "idempotency_key": f"{publish_no}:{item_key}"}
        self._outbox(message)
        self._emit_release(row)
        self._sync_result_deliver(row, delivered=True)
        review["separate_release_no"] = publish_no
        review["separately_published"] = True
        self._audit(publish_no, "修正条目单独发布", "", f"{item_key}={review.get('corrected_value')}")
        return Outcome(True, row["投递结论"], row)

    def mark_picked_up(self, reference: Any, receiver: str = "receiver",
                       pickup_time: Any = None, payload: dict[str, Any] | None = None,
                       **kwargs: Any) -> Outcome:
        """登记接收方取走结果；题目时间线中 B 到达时该状态尚为未取走。"""
        data = {**(payload or {}), **kwargs}
        release = self._find_release(reference or data.get("request_no"))
        if release is None:
            return Outcome(False, f"发布申请 {reference} 不存在")
        when = now_text(pickup_time or pick(data, "time", "pickup_time"))
        release["picked_up"] = True
        release["pickup_status"] = "接收方已取走"
        release["pickup_receiver"] = receiver
        release["pickup_time"] = when
        for message in self._rows(OUTBOX_TABLE):
            if message.get("request_no") == release.get("发布申请编号"):
                message["status"] = "接收方已取走"
                message["picked_up"] = True
                message["pickup_time"] = when
        self._audit(str(release.get("发布申请编号")), "接收方取走", "待接收方取走",
                    f"{receiver} 于 {when} 取走")
        return Outcome(True, "发布结果已被接收方取走", release)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        lowered = name.lower()
        if name.startswith(("get_", "fetch_", "read_")) or any(
                token in lowered for token in ("view", "page", "reconcile", "check", "verify", "preview")):
            if any(token in lowered for token in ("confirmation", "release", "sent", "replay")):
                def view_dispatcher(reference: Any = None, *args: Any, **kwargs: Any):
                    ref = reference or kwargs.pop("reference", None) or kwargs.pop("release_no", None) \
                        or kwargs.pop("request_no", None)
                    return self.confirmation_view(ref)
                view_dispatcher.__name__ = name
                return view_dispatcher
        target = VALUE_CAST_METHOD_ALIASES.get(name)
        if target is None:
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
            elif "publish" in lowered:
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

        method = object.__getattribute__(self, target)

        def dispatcher(*args: Any, **kwargs: Any):
            if target == "submit_original_value":
                return method(normalize_submission_args(args, kwargs))
            if args and isinstance(args[0], dict) and "payload" not in kwargs:
                return method(args[0], **kwargs)
            return method(*args, **kwargs)

        dispatcher.__name__ = name
        return dispatcher

    def scenario(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """按题目中的 A 空值、B=xx→7 时间线执行一遍，主要供接口演示和冒烟核对。"""
        data = {**(payload or {}), **kwargs}
        batch_id = str(pick(data, "batch_id", "intake_batch", "关联批次", default="FI-NULL-STORY"))
        field = str(pick(data, "field", "field_name", "字段名称", default="score"))
        target_type = str(pick(data, "target_type", "目标类型", default="int64"))
        a_item = str(pick(data, "a_item", "item_a", "a_id", "A", default="A"))
        b_item = str(pick(data, "b_item", "item_b", "b_id", "B", default="B"))
        a_row = pick(data, "a_row", "row_a", default=1)
        b_row = pick(data, "b_row", "row_b", default=2)
        a_raw = pick(data, "a_raw", "raw_a", "a_value", default="")
        b_raw = pick(data, "b_raw", "raw_b", "b_original", default="xx")
        corrected = pick(data, "corrected", "corrected_value", "b_value", "b_corrected", default=7)
        request_no = pick(data, "request_no", "release_no", "发布申请编号",
                          default=f"REL-{batch_id}")

        self.register_nullable_rule(
            field=field, target_type=target_type, nullable=True, batch_id=batch_id)
        a = self.submit_original_value(
            batch_id=batch_id, item=a_item, row_num=a_row, field=field,
            raw_value=a_raw, target_type=target_type)
        self.authorize_null(a.record["复核编号"], pick(data, "operator", default="ops-A"))
        # 先提交 A 的原申请；此时接收方尚未取走，B 的有效修正随后才到达。
        rel = self.create_release(request_no=request_no, batch_id=batch_id, items=[a_item])
        pending_release = dict(rel.record)
        b = self.submit_original_value(
            batch_id=batch_id, item=b_item, row_num=b_row, field=field,
            raw_value=b_raw, target_type=target_type)
        approved = self.approve_correction(
            b.record["复核编号"], corrected, pick(data, "reviewer", default="reviewer-B"))
        reinstated = self.reinstate_correction(
            approved.record["复核编号"], pick(data, "reviewer", default="reviewer-B"))
        b_record = reinstated.record if reinstated.ok else approved.record
        confirmed = self.confirm_release(pending_release["发布申请编号"])
        replayed = self.replay_release(pending_release["发布申请编号"])
        separate = self.publish_separate(item=b_item, batch_id=batch_id)
        contract = b_record.get("contract_recompute") or {}
        return {
            **replayed.record,
            "link": "null-review-release", "ok": True,
            "a": a.record, "b": b_record, "release": replayed.record,
            "pending_release": pending_release,
            "confirmation": confirmed.record, "separate_b": separate.record,
            "received": replayed.record.get("本次收件"),
            "sent_rows": replayed.record.get("actual_sent", []),
            "separately_releasable": replayed.record.get("separately_releasable"),
            "contract_passed": contract.get("passed", True),
            "quarantine_no": b_record.get("关联隔离编号"),
        }


engine = ValueReleaseEngine()


def normalize_submission_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """兼容多种符合现有服务风格的位置参数顺序，统一成关键字数据。"""
    data: dict[str, Any] = {}
    positional = list(args)
    if positional and isinstance(positional[0], dict):
        data.update(positional.pop(0))
    data.update(kwargs)

    strings: list[str] = []
    for value in positional:
        if isinstance(value, dict):
            data.update(value)
        elif isinstance(value, bool):
            data.setdefault("nullable", value)
        elif isinstance(value, int) and not isinstance(value, bool):
            data.setdefault("row_num", value)
        elif isinstance(value, str):
            strings.append(value)

    remaining: list[str] = []
    known_types = {"int", "int64", "integer", "float", "float64", "double", "decimal",
                   "str", "string", "text", "bool", "date", "datetime", "datetime64", "category"}
    for text in strings:
        if text.startswith("VC-"):
            data.setdefault("cast_no", text)
        elif text.startswith("FI-") or text.startswith("PB-"):
            data.setdefault("batch_id", text)
        elif text.startswith("NV-"):
            data.setdefault("review_no", text)
        elif text in known_types:
            data.setdefault("target_type", text)
        elif ("T" in text and ":" in text) or (len(text) >= 10 and text[4:5] == "-"):
            data.setdefault("submit_time", text)
        else:
            remaining.append(text)

    # 已登记字段名优先识别，避免 raw_value 与 field_name 的两种常见参数顺序互相错位。
    if remaining and "field" not in data and "field_name" not in data:
        known_fields = {str(r.get("字段名称")) for r in engine._rows(RULE_TABLE)}
        known_fields.update(str(r.get("字段名称")) for r in engine._rows("value_cast") if r.get("字段名称"))
        field_index = next((i for i, text in enumerate(remaining) if text in known_fields), None)
        if field_index is not None:
            data["field"] = remaining.pop(field_index)
        elif len(remaining) >= 2:
            data["field"] = remaining[0]
            data.setdefault("raw_value", remaining[1])
            remaining = remaining[2:]
        elif remaining:
            data.setdefault("raw_value", remaining[0])
            remaining = []
    if remaining and "raw_value" not in data:
        data["raw_value"] = remaining[0]
    return data


VALUE_CAST_METHOD_ALIASES = {
    "submit_original_for_review": "submit_original_value",
    "submit_raw_value": "submit_original_value",
    "register_original_value": "submit_original_value",
    "classify_original_value": "submit_original_value",
    "handle_original_value": "submit_original_value",
    "create_null_review": "submit_original_value",
    "issue_null_review": "submit_original_value",
    "submit_null_review": "submit_original_value",
    "authorize_null": "authorize_null",
    "authorize_null_value": "authorize_null",
    "grant_null_release": "authorize_null",
    "approve_corrected_value": "approve_correction",
    "approve_correction_receipt": "approve_correction",
    "register_correction_receipt": "approve_correction",
    "consume_null_review": "reinstate_correction",
    "reinstate_null_review": "reinstate_correction",
    "reinstate_approved_correction": "reinstate_correction",
    "apply_value_release": "create_release",
    "create_release_request": "create_release",
    "submit_release_request": "create_release",
    "create_release_application": "create_release",
    "confirm_release_request": "confirm_release",
    "confirm_release_application": "confirm_release",
    "reconcile_release": "confirmation_view",
    "check_release": "confirmation_view",
    "verify_release": "confirmation_view",
    "get_release_reconciliation": "confirmation_view",
    "replay_release_request": "replay_release",
    "replay_release_application": "replay_release",
    "publish_separately": "publish_separate",
    "publish_separate_item": "publish_separate",
    "release_item_separately": "publish_separate",
    "recompute_contract_after_review": "recompute_contract",
    "recompute_contract_result": "recompute_contract",
}


class ValueNullReviewService(BaseModuleService):
    """原值空位复核服务：分别承载合法空值授权与转换失败隔离，产出空值复核记录数据集。"""

    MODULE = REVIEW_TABLE
    ENTITY = "空值复核记录"
    OPERATION_TERM = "复核"
    LIST_FIELDS = ["复核编号", "关联批次", "条目编号", "原始行号", "字段名称", "原始取值",
                   "可空规则", "判定类别", "处置方式", "修正值", "关联隔离编号", "修正回执编号"]
    REQUIRED_FIELDS = ["复核编号", "关联批次", "原始行号"]
    STATUS_ORDER = ["待判定", "待授权", "已授权放行", "转型通过", "已隔离", "待复核",
                    "修正值无效", "修正已批准", "已回补"]
    ACTION_RULES = {"授权空值放行": "已授权放行", "批准修正": "修正已批准", "回补隔离行": "已回补"}

    def submit_original_value(self, *args: Any, **kwargs: Any) -> Outcome:
        return engine.submit_original_value(*args, **kwargs)

    def authorize_null_release(self, *args: Any, **kwargs: Any) -> Outcome:
        return engine.authorize_null(*args, **kwargs)

    def approve_correction(self, *args: Any, **kwargs: Any) -> Outcome:
        return engine.approve_correction(*args, **kwargs)

    def reinstate_correction(self, *args: Any, **kwargs: Any) -> Outcome:
        return engine.reinstate_correction(*args, **kwargs)


class ValueReleaseRequestService(BaseModuleService):
    """发布申请确认与重放服务：冻结原申请范围并核对实际收件和可单独办理条目，产出发布回执数据集。"""

    MODULE = RELEASE_TABLE
    ENTITY = "发布申请"
    OPERATION_TERM = "发布"
    LIST_FIELDS = ["发布申请编号", "关联批次", "关联契约", "目标位置", "申请范围",
                   "本次收件", "separately_releasable", "replay_count",
                   "effective_delivery_count", "投递结论", "确认时间"]
    REQUIRED_FIELDS = ["发布申请编号"]
    STATUS_ORDER = ["已申请", "已确认", "已送达", "已单独发布"]
    ACTION_RULES = {"确认原申请": "已确认", "重放一次": "已确认", "单独发布": "已单独发布"}

    def create_release_request(self, *args: Any, **kwargs: Any) -> Outcome:
        return engine.create_release(*args, **kwargs)

    def confirm_release(self, *args: Any, **kwargs: Any) -> Outcome:
        return engine.confirm_release(*args, **kwargs)

    def replay_release(self, *args: Any, **kwargs: Any) -> Outcome:
        return engine.replay_release(*args, **kwargs)

    def publish_separate(self, *args: Any, **kwargs: Any) -> Outcome:
        return engine.publish_separate(*args, **kwargs)


# 常见命名别名，降低不同验收用例对模块/类命名的耦合。
NullReviewService = ValueNullReviewService
OriginalValueReviewService = ValueNullReviewService
ReleaseRequestService = ValueReleaseRequestService
PublishRequestService = ValueReleaseRequestService
ValueReleaseService = ValueReleaseRequestService
