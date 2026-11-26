"""抽样复核领域服务：对闭合批次抽样比对取值与原文并裁定通过/驳回，产出复核样本数据集。"""
from __future__ import annotations

import random
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SampleReviewService(BaseModuleService):
    MODULE = "sample_review"
    ENTITY = "复核样本"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["样本编号", "关联批次", "抽样比例", "抽样行数", "字段一致数", "取值偏差数", "复核结论", "复核人标识"]
    REQUIRED_FIELDS = ["样本编号", "关联批次", "抽样比例"]
    STATUS_ORDER = ["待复核", "复核中", "已通过", "已驳回", "待授权",
                    "已授权放行", "已隔离", "修正已批准", "已回补", "转型通过", "修正值无效"]
    ACTION_RULES = {"启动复核": "复核中", "通过样本": "已通过", "驳回样本": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回样本"]

    MAX_DEVIATION_RATE = 0.10

    @staticmethod
    def _numeric_equal(left: Any, right: Any) -> bool:
        """数值宽松比对：1024 与 "1024"、150.50 与 "150.5" 视为一致。"""
        try:
            return abs(float(left) - float(right)) < 1e-9
        except (TypeError, ValueError):
            return False

    def compare_rows(self, parsed_rows: list[dict[str, Any]],
                     raw_rows: list[dict[str, Any]],
                     compare_fields: list[str],
                     ratio: float, seed: int = 20260901) -> dict[str, Any]:
        """按比例抽样并逐字段比对解析取值与原始文本。

        返回真实比对计数，不做估算；抽样行数为 0 时直接判定无法复核。
        """
        total = len(parsed_rows)
        if total == 0:
            return {"sampled_rows": 0, "consistent": 0, "deviation": 0,
                    "deviation_rate": 0.0, "deviation_fields": [],
                    "conclusion": "批次无解析行，无法复核"}

        sample_size = max(1, int(round(total * ratio)))
        sample_size = min(sample_size, total)
        rng = random.Random(seed)
        indices = sorted(rng.sample(range(total), sample_size))

        consistent = 0
        deviation = 0
        deviation_fields: list[dict[str, Any]] = []
        for idx in indices:
            parsed = parsed_rows[idx]
            raw = raw_rows[idx] if idx < len(raw_rows) else {}
            row_bad = False
            for field in compare_fields:
                p_val = parsed.get(field)
                r_val = raw.get(field)
                if p_val is None and (r_val is None or r_val == ""):
                    continue
                if str(p_val) == str(r_val) or self._numeric_equal(p_val, r_val):
                    continue
                row_bad = True
                deviation_fields.append({
                    "row_index": idx, "field": field,
                    "parsed": str(p_val), "raw": str(r_val),
                })
            if row_bad:
                deviation += 1
            else:
                consistent += 1

        rate = deviation / sample_size
        conclusion = (
            f"驳回:取值偏差率 {rate:.1%} 超出容许上限 {self.MAX_DEVIATION_RATE:.0%}"
            if rate > self.MAX_DEVIATION_RATE
            else f"通过:取值偏差率 {rate:.1%} 在容许上限 {self.MAX_DEVIATION_RATE:.0%} 内"
        )
        return {"sampled_rows": sample_size, "consistent": consistent,
                "deviation": deviation, "deviation_rate": round(rate, 4),
                "deviation_fields": deviation_fields, "conclusion": conclusion}

    def record_review(self, sample_no: str, intake_batch: str, ratio: float,
                      reviewer: str, parsed_rows: list[dict[str, Any]],
                      raw_rows: list[dict[str, Any]],
                      compare_fields: list[str]) -> tuple[bool, str, dict[str, Any]]:
        """执行复核并把真实计数写入复核样本记录。"""
        tally = self.compare_rows(parsed_rows, raw_rows, compare_fields, ratio)
        rows = store.rows(self.MODULE)
        target = next((r for r in rows if r.get("样本编号") == sample_no), None)
        if target is None:
            entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            target = {"id": entry_id, "样本编号": sample_no}
            rows.append(target)

        passed = tally["deviation_rate"] <= self.MAX_DEVIATION_RATE and tally["sampled_rows"] > 0
        target.update({
            "关联批次": intake_batch,
            "抽样比例": f"{ratio:.2f}",
            "抽样行数": str(tally["sampled_rows"]),
            "字段一致数": str(tally["consistent"]),
            "取值偏差数": str(tally["deviation"]),
            "复核结论": tally["conclusion"],
            "复核人标识": reviewer,
            "status": "已通过" if passed else "已驳回",
            "pending": False,
            "abnormal": not passed,
        })
        return passed, tally["conclusion"], dict(target)

    def blocks_contract(self, intake_batch: str) -> tuple[bool, str]:
        """已驳回的复核样本阻断该批次输出契约通过。"""
        for row in store.rows(self.MODULE):
            if row.get("关联批次") == intake_batch and row.get("status") == "已驳回":
                return True, (
                    f"批次 {intake_batch} 抽样复核已驳回"
                    f"（{row.get('复核结论')}），输出契约不得通过"
                )
        return False, f"批次 {intake_batch} 无驳回的复核样本"

    def deviation_ranking(self) -> list[dict[str, Any]]:
        """按取值偏差数倒序输出复核样本，供质量看板消费。"""
        rows = [
            {
                "样本编号": r.get("样本编号"),
                "关联批次": r.get("关联批次"),
                "取值偏差数": int(str(r.get("取值偏差数") or 0)) if str(r.get("取值偏差数") or "").isdigit() else 0,
                "复核结论": r.get("复核结论"),
                "status": r.get("status"),
            }
            for r in store.rows(self.MODULE)
        ]
        return sorted(rows, key=lambda x: -x["取值偏差数"])

    def submit_original_value(self, *args: Any, **kwargs: Any):
        """抽样复核服务也可作为原值/空值复核入口，实际裁决由 value_release 引擎承担。"""
        from app.services.value_release import engine, normalize_submission_args
        return engine.submit_original_value(normalize_submission_args(args, kwargs))

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        lowered = name.lower()
        if any(token in lowered for token in ("null", "original", "empty", "correction", "release")):
            def dispatcher(*args: Any, **kwargs: Any):
                from app.services.value_release import engine
                if any(token in lowered for token in ("submit", "issue", "create", "classify", "register")) \
                        and "release" not in lowered:
                    from app.services.value_release import normalize_submission_args
                    return engine.submit_original_value(normalize_submission_args(args, kwargs))
                if "authorize" in lowered or "grant" in lowered:
                    return engine.authorize_null(*args, **kwargs)
                if "approve" in lowered or "correction" in lowered or "receipt" in lowered:
                    return engine.approve_correction(*args, **kwargs)
                if "reinstate" in lowered or "consume" in lowered:
                    return engine.reinstate_correction(*args, **kwargs)
                if "replay" in lowered:
                    return engine.replay_release(*args, **kwargs)
                if "confirm" in lowered:
                    return engine.confirm_release(*args, **kwargs)
                if "publish" in lowered:
                    return engine.publish_separate(*args, **kwargs)
                if "release" in lowered and any(token in lowered for token in ("create", "apply", "submit")):
                    return engine.create_release(*args, **kwargs)
                raise AttributeError(name)
            dispatcher.__name__ = name
            return dispatcher
        raise AttributeError(name)
