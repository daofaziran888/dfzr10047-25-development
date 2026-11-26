"""原值空位授权、xx→7 修正回补与发布幂等重放链路验收。"""
from __future__ import annotations

import pytest

from app.services import SERVICES
from app.store import store


@pytest.fixture
def clean_store():
    store.reload_seed()
    yield store
    store.reload_seed()


def test_null_a_quarantine_b_correction_and_idempotent_replay(clean_store):
    vc = SERVICES["value_cast"]
    rq = SERVICES["row_quarantine"]

    vc.register_nullable_field(
        field="score", nullable=True, target_type="int64", batch_id="FI-NULL-ACCEPT")

    a = vc.submit_original_value(
        batch_id="FI-NULL-ACCEPT", item="A", row_num=1, field="score",
        raw_value="", target_type="int64")
    assert a.ok is True
    assert a["classification"] == "legal_null"
    assert a["判定类别"] == "合法业务空值"
    assert not any(r.get("关联批次") == "FI-NULL-ACCEPT"
                   for r in store.rows("row_quarantine"))

    authorized = vc.authorize_null_release(a["复核编号"], "ops")
    assert authorized.ok is True
    assert authorized["status"] == "已授权放行"

    b = vc.submit_original_value(
        batch_id="FI-NULL-ACCEPT", item="B", row_num=2, field="score",
        raw_value="xx", target_type="int64")
    assert b.ok is False
    assert b["classification"] == "cast_failure"
    assert b["原始取值"] == "xx"
    qno = b["关联隔离编号"]
    assert qno

    store.rows("parse_batch").append({
        "id": 991, "status": "存在隔离", "pending": True, "abnormal": True,
        "批次记录编号": "PB-NULL-ACCEPT", "关联投递批次": "FI-NULL-ACCEPT",
        "总行数": "2", "成功行数": "1", "隔离行数": "1", "批次状态": "存在隔离",
    })
    store.rows("output_contract").append({
        "id": 991, "status": "未通过", "pending": True, "abnormal": True,
        "契约编号": "OC-NULL-ACCEPT", "关联批次": "PB-NULL-ACCEPT",
        "输出列集合": "score", "列顺序约定": "score", "必填列": "score",
        "行数下限": "2", "契约校验结论": "", "校验时间": "",
    })

    approved = vc.approve_original_correction(b["复核编号"], "7", "reviewer")
    assert approved.ok is True
    assert approved["corrected_value"] == 7
    assert approved["原始取值"] == "xx"

    ok, _msg, quarantine = rq.reinstate_to_batch(qno, "reviewer", "修正为7", "t2")
    assert ok is True
    assert quarantine["status"] == "已回补"
    batch = next(r for r in store.rows("parse_batch") if r["批次记录编号"] == "PB-NULL-ACCEPT")
    assert batch["成功行数"] == "2"
    assert batch["隔离行数"] == "0"

    contract_svc = SERVICES["output_contract"]
    passed, failures, _contract = contract_svc.validate_batch(
        "OC-NULL-ACCEPT", ["score"], 2, "t3")
    assert passed is True
    assert failures == []

    release = vc.apply_release(
        request_no="REL-NULL-ACCEPT", batch_id="FI-NULL-ACCEPT", items=["A"])
    assert release.ok is True
    confirmed = vc.confirm_release_application("REL-NULL-ACCEPT")
    assert confirmed["本次收件"] == ["A"]
    assert confirmed["separately_releasable"] == ["B"]

    replayed = vc.replay_release_application("REL-NULL-ACCEPT")
    assert replayed["本次收件"] == ["A"]
    assert replayed["effective_delivery_count"] == 1
    assert replayed["replay_count"] == 1

    separate = vc.publish_corrected_item_separately("B", batch_id="FI-NULL-ACCEPT")
    assert separate.ok is True
    assert separate["本次收件"] == ["B"]
    assert separate["effective_delivery_count"] == 1

    duplicate = vc.publish_corrected_item_separately("B", batch_id="FI-NULL-ACCEPT")
    assert duplicate.ok is True
    assert "重复" in duplicate.message
    assert len([r for r in store.rows("release_outbox")
                if r.get("item") == "B"]) == 1
