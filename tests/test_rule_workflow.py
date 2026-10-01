"""规则草稿 → 人工确认 → 发布工作流的纯逻辑测试。"""

from rule_workflow import (
    create_draft_batch,
    load_draft_store,
    precheck_rule,
    public_batch,
    repair_conflicting_rule_ids,
    refresh_batch_status,
    save_draft_store,
)


SOURCE_PAGES = [{
    "page": 3,
    "text": "申请人本学年应无挂科记录。存在挂科课程的，不得参加奖学金评定。",
}]


def valid_status_rule():
    return {
        "id": "奖学金评定_R001",
        "name": "无挂科方可参评奖学金",
        "type": "status",
        "field": "挂科记录",
        "pass_values": ["无挂科"],
        "fail_values": ["存在挂科"],
        "severity": "critical",
        "message": "存在挂科课程，不得参评奖学金",
        "scene": ["资格审查"],
        "source_excerpt": "申请人本学年应无挂科记录。存在挂科课程的，不得参加奖学金评定。",
    }


def test_precheck_accepts_complete_rule_and_verifies_source_page():
    rule = valid_status_rule()
    issues = precheck_rule(rule, [], SOURCE_PAGES)
    assert not [issue for issue in issues if issue["blocking"]]
    assert rule["source_page"] == 3


def test_precheck_blocks_incomplete_status_and_missing_evidence():
    rule = valid_status_rule()
    rule.pop("fail_values")
    rule["pass_values"] = ["不存在状态"]
    rule["name"] = "虚构资格规则"
    rule["field"] = "虚构字段"
    rule["source_excerpt"] = "制度中不存在的句子"
    issues = precheck_rule(rule, [], SOURCE_PAGES)
    codes = {issue["code"] for issue in issues if issue["blocking"]}
    assert {"incomplete_status", "missing_evidence"} <= codes


def test_precheck_blocks_duplicate_name_in_official_rules():
    rule = valid_status_rule()
    existing = [{**valid_status_rule(), "id": "奖学金评定_R099"}]
    issues = precheck_rule(rule, existing, SOURCE_PAGES)
    assert any(issue["code"] == "duplicate_name" and issue["blocking"] for issue in issues)


def test_create_batch_assigns_new_ids_and_starts_pending():
    existing = [{**valid_status_rule(), "id": "奖学金评定_R007", "name": "旧规则"}]
    candidate = valid_status_rule()
    candidate["id"] = "模型随便生成的编号"
    batch = create_draft_batch(
        "奖学金评定", [candidate], "制度.pdf", "制度__abcd1234.pdf",
        SOURCE_PAGES, existing,
    )
    assert batch["status"] == "pending_review"
    assert batch["rules"][0]["status"] == "pending"
    assert batch["rules"][0]["rule"]["id"] == "奖学金评定_R008"
    assert batch["source_url"].startswith("/preview/")


def test_batch_becomes_ready_only_after_every_rule_is_decided():
    batch = {"rules": [{"status": "approved"}, {"status": "pending"}]}
    assert refresh_batch_status(batch) == "pending_review"
    batch["rules"][1]["status"] = "rejected"
    assert refresh_batch_status(batch) == "ready_to_publish"


def test_publish_repairs_id_collision_from_parallel_draft_batches():
    existing = [{**valid_status_rule(), "id": "奖学金评定_R008", "name": "正式规则"}]
    candidate = {**valid_status_rule(), "id": "奖学金评定_R008", "name": "另一条规则"}
    repaired = repair_conflicting_rule_ids("奖学金评定", [candidate], existing)
    assert repaired[0]["id"] == "奖学金评定_R009"


def test_store_round_trip_and_public_response_hides_source_snapshot(tmp_path):
    batch = create_draft_batch(
        "奖学金评定", [valid_status_rule()], "制度.pdf", "制度__abcd1234.pdf",
        SOURCE_PAGES, [],
    )
    path = tmp_path / "drafts.json"
    save_draft_store(path, {"version": "1.0", "batches": [batch], "audit": []})
    loaded = load_draft_store(path)
    assert loaded["batches"][0]["source_pages"] == SOURCE_PAGES
    assert "source_pages" not in public_batch(loaded["batches"][0])
