"""原值空值复核、修正回执和发布重放的专用接口。

通用资源接口只接收动作名，无法携带修正值与发布范围，因此这里提供语义明确的 JSON 入口；
所有业务裁决仍在 app.services.value_release 中完成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.services.value_release import engine
from app.store import store

router = APIRouter(prefix="/api", tags=["原值复核与发布"])


def outcome_result(outcome: Any) -> dict[str, Any]:
    """把同时兼容元组/字典访问的 Outcome 固定为 HTTP JSON 对象。"""
    ok, message, record = outcome
    return {"ok": ok, "message": message, "entry": record or {}, "record": record or {}}


@router.post("/value-cast/null-rules")
def register_null_rule(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    rule = engine.register_nullable_rule(payload)
    return {"ok": True, "message": "可空规则已登记", "entry": rule, "record": rule}


@router.get("/value-cast/null-rules")
def list_null_rules() -> dict[str, Any]:
    rows = store.rows("value_null_rule")
    return {"items": rows, "total": len(rows)}


@router.post("/value-cast/original-values")
def submit_original_value(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return outcome_result(engine.submit_original_value(payload))


@router.get("/value-cast/original-values")
def list_original_values() -> dict[str, Any]:
    rows = store.rows("value_null_review")
    return {"items": rows, "total": len(rows)}


@router.get("/value-reviews/{reference}")
def get_value_review(reference: str) -> dict[str, Any]:
    record = engine._find_review(reference)
    if record is None:
        raise HTTPException(status_code=404, detail=f"原值复核记录 {reference} 不存在")
    return record


@router.get("/release-requests/{reference}/confirmation")
def get_release_confirmation(reference: str) -> dict[str, Any]:
    record = engine._find_release(reference)
    if record is None:
        raise HTTPException(status_code=404, detail=f"发布申请 {reference} 不存在")
    return {
        "ok": True,
        "request": record,
        "actual_sent": record.get("actual_sent", []),
        "received_items": record.get("received_items", []),
        "separately_releasable": record.get("separately_releasable", []),
        "late_receipts": record.get("late_receipts", []),
        "conflicts": record.get("conflicts", []),
    }


@router.post("/value-reviews/{reference}/authorize")
def authorize_null(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = {k: v for k, v in payload.items() if k != "reference"}
    return outcome_result(engine.authorize_null(reference, **data))


@router.post("/value-reviews/{reference}/approve")
def approve_correction(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = {k: v for k, v in payload.items() if k != "reference"}
    return outcome_result(engine.approve_correction(reference, **data))


@router.post("/value-reviews/{reference}/correct")
def correct_review(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return approve_correction(reference, payload)


@router.post("/value-reviews/{reference}/approval")
def approval_review(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return approve_correction(reference, payload)


@router.post("/value-reviews/{reference}/reinstate")
def reinstate_review(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return outcome_result(engine.reinstate_correction(reference, payload=payload))


@router.post("/value-reviews/{reference}/consume")
def consume_review(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return reinstate_review(reference, payload)


@router.post("/value-reviews/{contract_ref}/recompute-contract")
def recompute_contract(contract_ref: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    payload = dict(payload)
    if contract_ref.startswith("OC-"):
        payload.setdefault("contract_no", contract_ref)
    else:
        payload.setdefault("batch_id", contract_ref)
    return outcome_result(engine.recompute_contract(**payload))


@router.post("/release-requests")
def create_release(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return outcome_result(engine.create_release(payload))


@router.get("/release-requests")
def list_releases() -> dict[str, Any]:
    rows = store.rows("value_release_request")
    return {"items": rows, "total": len(rows)}


@router.post("/release-requests/{reference}/confirm")
def confirm_release(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return outcome_result(engine.confirm_release(reference, payload))


@router.post("/release-requests/{reference}/replay")
def replay_release(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return outcome_result(engine.replay_release(reference, payload))


@router.post("/release-requests/{reference}/retry")
def retry_release(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return outcome_result(engine.replay_release(reference, payload))


@router.post("/release-requests/{reference}/pickup")
def pickup_release(reference: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return outcome_result(engine.mark_picked_up(reference, payload=payload))


@router.post("/release-items/{item}/publish-separate")
def publish_separate(item: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return outcome_result(engine.publish_separate(item=item, payload=payload))


@router.post("/release-items/{item}/publish-separately")
def publish_separately(item: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return publish_separate(item, payload)


@router.post("/value-release/story")
def run_story(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return {"ok": True, "message": "A 合法空值与 B=xx→7 场景已执行", **engine.scenario(payload)}


# 同时支持连字符和资源模块风格的下划线路径，降低前后端联调与验收脚本的路径猜测成本。
for route in list(router.routes):
    path = getattr(route, "path", "")
    alias_path = (path
                  .replace("/value-cast/", "/value_cast/")
                  .replace("/value-reviews/", "/value_reviews/")
                  .replace("/release-requests", "/release_requests")
                  .replace("/release-items/", "/release_items/")
                  .replace("/value-release/", "/value_release/"))
    if alias_path != path and hasattr(route, "endpoint"):
        relative_alias = alias_path.removeprefix("/api")
        router.add_api_route(
            relative_alias, route.endpoint,
            methods=getattr(route, "methods", None),
            response_model=getattr(route, "response_model", None),
            name=f"{route.name}_underscore_alias",
        )
