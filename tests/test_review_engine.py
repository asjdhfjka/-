"""review_engine 纯逻辑单测。

这些用例对应的每一个断言，都是本次修复的真实缺陷 ——
写下来的目的就是让它们不再悄悄回归。
"""

import pytest

from review_engine import (
    DEDUCTION,
    SEVERITY_ORDER,
    build_source_refs,
    compute_score,
    domain_alignment_report,
    domain_catalog,
    enum_match,
    execute_hard_rules,
    formula_fields,
    normalize_domain,
    normalize_import_domain,
    route_rules,
    scene_matches,
)


def test_build_source_refs_maps_original_name_to_archived_file(tmp_path):
    stored = "学生手册__a1b2c3d4.pdf"
    (tmp_path / stored).write_bytes(b"pdf")
    refs = build_source_refs(
        ["学生手册.pdf", "学生手册.pdf"],
        [{
            "original_filename": "学生手册.pdf",
            "source_title": "学生手册.pdf",
            "source_file": stored,
            "uploaded_at": "2026-10-01T12:00:00",
        }],
        tmp_path,
    )
    assert len(refs) == 1
    assert refs[0]["available"] is True
    assert refs[0]["stored_file"] == stored
    assert refs[0]["url"].startswith("/preview/")
    assert "%E5%AD%A6%E7%94%9F" in refs[0]["url"]


def test_build_source_refs_falls_back_to_hashed_file_and_marks_missing(tmp_path):
    stored = "旧制度__1234abcd.docx"
    (tmp_path / stored).write_bytes(b"docx")
    refs = build_source_refs(["旧制度.docx", "未归档.pdf"], [], tmp_path)
    assert refs[0]["stored_file"] == stored and refs[0]["available"] is True
    assert refs[1]["stored_file"] == "" and refs[1]["available"] is False
    assert refs[1]["url"] == ""


# ============================================================
# 名称归一
# ============================================================

def test_critical_ranks_before_warning():
    """critical 必须排在最前。原实现漏了 critical，致命问题排到建议问题后面。"""
    assert SEVERITY_ORDER["critical"] < SEVERITY_ORDER["error"] < SEVERITY_ORDER["warning"]
    assert DEDUCTION["critical"] == 30 and DEDUCTION["error"] == 10 and DEDUCTION["warning"] == 3


@pytest.mark.parametrize("raw,expected", [
    ("团员推优", "团员推优"),          # 精确
    ("推优", "团员推优"),              # 别名
    ("奖学金申请", "奖学金评定"),       # 别名
    ("奖学金", "奖学金评定"),           # 互含兜底
    ("团员推优 （评议）", "团员推优"),   # 空白差异
])
def test_normalize_domain(raw, expected):
    available = ["奖学金评定", "团员推优", "请假管理"]
    aliases = {"推优": "团员推优", "奖学金申请": "奖学金评定"}
    assert normalize_domain(raw, available, aliases) == expected


def test_normalize_domain_unknown_passes_through():
    assert normalize_domain("机器人管理", ["奖学金评定"]) == "机器人管理"


def test_domain_catalog_combines_static_and_generated_domains_without_duplicates():
    cfg = {"domains": {"团员推优": "x", "奖学金评定": "y"}}
    rules = {"domains": {"团员推优": {}, "四六级考试报名": {}}}
    assert domain_catalog(cfg, rules) == ["团员推优", "奖学金评定", "四六级考试报名"]


def test_import_domain_prefers_alias_then_reuses_existing_rule_domain():
    cfg = {
        "domains": {"团员推优": "x"},
        "domain_aliases": {"推优": "团员推优"},
    }
    rules = {"domains": {"四六级考试报名": {}}}
    assert normalize_import_domain("推优", cfg, rules) == "团员推优"
    assert normalize_import_domain("四六级考试报名 ", cfg, rules) == "四六级考试报名"


def test_domain_alignment_report_exposes_both_directions_and_bad_targets():
    cfg = {
        "domains": {"团员推优": "x", "请假管理": "y"},
        "domain_aliases": {"错误别名": "不存在领域"},
        "rule_domain_fallbacks": {"请假管理": ["缺失规则桶"]},
    }
    rules = {"domains": {"团员推优": {}, "学科竞赛管理": {}}}
    report = domain_alignment_report(cfg, rules)
    assert report["configured_without_rules"] == ["请假管理"]
    assert report["rules_without_static_config"] == ["学科竞赛管理"]
    assert report["aliases_with_missing_target"] == {"错误别名": "不存在领域"}
    assert report["fallbacks_with_missing_rule_target"] == {"请假管理": ["缺失规则桶"]}


@pytest.mark.parametrize("scene,rule_scenes,expected", [
    ("资格审查表", ["资格审查"], True),    # 枚举差一个「表」字
    ("资格审查", ["资格审查表"], True),
    ("票根", ["票根", "选票"], True),
    ("票根", ["申请表"], False),
    ("通用", ["票根"], False),             # 通用不参与精准匹配
])
def test_scene_matches(scene, rule_scenes, expected):
    assert scene_matches(scene, rule_scenes) is expected


# ============================================================
# 规则路由
# ============================================================

RULES = {
    "domains": {
        "团员推优": {"rules": [
            {"id": "A1", "name": "票根需监票人签名", "severity": "warning", "scene": ["票根"]},
            {"id": "A2", "name": "入团满一年", "severity": "critical", "scene": ["资格审查"]},
            {"id": "A3", "name": "日期格式", "severity": "error", "scene": ["通用"]},
            {"id": "A4", "name": "旧规则无scene", "severity": "error"},
        ], "source_docs": []},
    },
    "global_rules": [{"id": "G1", "name": "全局", "severity": "warning"}],
}


def test_route_rules_specific_scene_takes_exact_plus_general():
    rules, resolved = route_rules("团员推优", "票根", RULES)
    ids = [r["id"] for r in rules]
    assert resolved == "团员推优"
    assert "A1" in ids and "A3" in ids      # 精准 + 通用
    assert "A2" not in ids                   # 其他场景被排除
    assert ids[-1] == "G1"                   # 全局规则兜底


def test_route_rules_orders_critical_first():
    rules, _ = route_rules("团员推优", "资格审查", RULES)
    assert rules[0]["id"] == "A2"            # critical 排最前


def test_route_rules_falls_back_for_unknown_domain():
    """领域对不上时只跑全局规则，而不是抛错。"""
    rules, resolved = route_rules("机器人管理", "票根", RULES)
    assert resolved == "机器人管理"
    assert [r["id"] for r in rules] == ["G1"]


def test_route_rules_alias_domain_resolves():
    rules, resolved = route_rules("推优", "票根", RULES)
    assert resolved == "团员推优"
    assert any(r["id"] == "A1" for r in rules)


def test_route_rules_uses_configured_domain_fallback():
    rules, resolved = route_rules(
        "奖学金评定", "资格审查表", RULES,
        {"rule_domain_fallbacks": {"奖学金评定": ["团员推优"]}})
    assert resolved == "团员推优"
    assert any(r["id"] == "A2" for r in rules)


def test_route_rules_respects_limit():
    many = {"domains": {"X": {"rules": [
        {"id": f"R{i}", "name": "n", "severity": "warning"} for i in range(100)
    ]}}, "global_rules": []}
    rules, _ = route_rules("X", "通用", many)
    assert len(rules) == 30                   # config.MAX_APPLICABLE_RULES 默认值


# ============================================================
# 硬规则校验
# ============================================================

def test_enum_only_forward_match():
    """反向包含必须关闭：抽取值「通」不应因合法值「通知」而通过。"""
    assert enum_match("通知已发布", ["通知"]) is True
    assert enum_match("通", ["通知"]) is False
    assert enum_match("", ["通知"]) is False
    assert enum_match("同意", []) is False


def test_enum_in_execute_hard_rules():
    rules = [{"id": "E1", "name": "材料类型", "type": "enum", "field": "类型",
              "values": ["通知"], "severity": "error"}]
    passed = execute_hard_rules({"类型": "通知"}, rules)[0]
    assert passed["passed"] is True
    failed = execute_hard_rules({"类型": "通"}, rules)[0]
    assert failed["passed"] is False


def test_required_empty_is_failure():
    rules = [{"id": "R1", "name": "必须含姓名", "type": "required", "field": "姓名"}]
    assert execute_hard_rules({"姓名": ""}, rules)[0]["passed"] is False


def test_non_required_empty_needs_review():
    rules = [{"id": "R1", "name": "绩点不低于2.0", "type": "range", "field": "绩点",
              "min": 2.0, "max": 5.0}]
    r = execute_hard_rules({"绩点": ""}, rules)[0]
    assert r["status"] == "pending" and r["needs_review"] is True


def test_status_rule_distinguishes_pass_fail_and_missing_evidence():
    rule = {
        "id": "S1", "name": "无挂科方可参评奖学金", "type": "status",
        "field": "挂科记录", "pass_values": ["无挂科", "未挂科"],
        "fail_values": ["存在挂科", "有挂科"], "severity": "critical",
        "message": "存在挂科课程，不予参评奖学金",
    }
    passed = execute_hard_rules({"挂科记录": "经核验，本学年无挂科"}, [rule])[0]
    failed = execute_hard_rules({"挂科记录": "本学年存在挂科课程"}, [rule])[0]
    pending = execute_hard_rules({"挂科记录": ""}, [rule])[0]
    ambiguous = execute_hard_rules({"挂科记录": "材料只提供平均成绩"}, [rule])[0]

    assert passed["status"] == "pass"
    assert failed["status"] == "fail"
    assert pending["status"] == "pending" and "没有" in pending["message"]
    assert ambiguous["status"] == "pending"
    assert "官方记录" in pending["fix_suggestion"]
    assert "不要仅修改文字" in failed["fix_suggestion"]


def test_failed_rule_contains_actionable_fix_suggestion():
    rule = {
        "id": "R-FIX", "name": "日期格式", "type": "regex", "field": "日期",
        "pattern": "^\\d{2}日$", "severity": "error", "message": "日期格式错误",
    }
    result = execute_hard_rules({"日期": "2日"}, [rule])[0]
    assert result["status"] == "fail"
    assert "日期" in result["fix_suggestion"]
    assert "格式要求" in result["fix_suggestion"]


def test_meta_rule_is_not_applicable():
    """约束「评比规则本身」的规则不能拿来扣分。"""
    rules = [{"id": "M1", "name": "总积分计算公式正确", "type": "keyword",
              "field": "正文", "keywords": ["D×8%"]}]
    r = execute_hard_rules({"_full_text": "无关内容"}, rules)[0]
    assert r["not_applicable"] is True


def test_broken_regex_needs_admin_review():
    """规则自带的正则写错了，不能拿坏规则去冤枉材料。"""
    rules = [{"id": "X1", "name": "日期格式", "type": "regex",
              "field": "日期", "pattern": "^(0[1-9"}]
    r = execute_hard_rules({"日期": "09日"}, rules)[0]
    assert r["status"] == "pending" and "表达式有误" in r["message"]


def test_range_without_bounds_needs_review():
    rules = [{"id": "X2", "name": "分数", "type": "range", "field": "分数"}]
    r = execute_hard_rules({"分数": "80"}, rules)[0]
    assert r["status"] == "pending"


def test_range_extracts_number_from_text():
    rules = [{"id": "X3", "name": "体测", "type": "range", "field": "体测",
              "min": 80, "severity": "critical"}]
    assert execute_hard_rules({"体测": "成绩85分"}, rules)[0]["passed"] is True
    assert execute_hard_rules({"体测": "成绩72分"}, rules)[0]["passed"] is False


def test_unknown_rule_type_needs_review():
    rules = [{"id": "X4", "name": "怪规则", "type": "telepathy", "field": "x"}]
    assert execute_hard_rules({"x": "y"}, rules)[0]["status"] == "pending"


def test_forbidden_keyword_rule_has_correct_direction():
    rule = {"id": "K1", "name": "票根不得残留模版备注",
            "type": "forbidden_keyword", "field": "票根内容",
            "keywords": ["备注", "模版", "模板"], "severity": "error"}
    dirty = execute_hard_rules({"票根内容": "备注：这是模版文字"}, [rule])[0]
    clean = execute_hard_rules({"票根内容": "候选人张三，赞成票30"}, [rule])[0]
    dirty_full_text = execute_hard_rules({"_full_text": "备注：这是模版文字"}, [rule])[0]
    incomplete_extraction = execute_hard_rules(
        {"票根内容": "候选人张三", "_full_text": "候选人张三\n备注：这是模版文字"}, [rule])[0]
    assert dirty["passed"] is False
    assert clean["passed"] is True
    assert dirty_full_text["passed"] is False
    assert incomplete_extraction["passed"] is False
    assert dirty["actual_value"] == "备注：这是模版文字"
    assert dirty["requirement"].startswith("不得包含")


def test_formula_range_is_evaluated_from_facts():
    rule = {"id": "F1", "name": "实到人数须超过班级团员数八成",
            "type": "range", "field": "实到团员数",
            "min": "0.8 * 班级团员数a + 1", "severity": "critical"}
    assert formula_fields(rule) == ["班级团员数a"]
    passed = execute_hard_rules({"实到团员数": 46, "应到团员数": 50}, [rule])[0]
    failed = execute_hard_rules({"实到团员数": 40, "应到团员数": 50}, [rule])[0]
    assert passed["passed"] is True
    assert failed["passed"] is False


def test_formula_missing_dependency_becomes_pending_instead_of_crashing():
    rule = {"id": "F2", "name": "赞成票须过半", "type": "range", "field": "赞成票数",
            "min": "0.5 * 班级团员数a + 1", "severity": "critical"}
    result = execute_hard_rules({"赞成票数": 30}, [rule])[0]
    assert result["status"] == "pending"
    assert "缺少计算规则所需字段" in result["message"]


def test_date_string_is_not_misread_as_arithmetic_formula():
    rule = {"id": "D1", "name": "截止日期", "type": "range", "field": "日期",
            "max": "2025-11-13", "severity": "error"}
    result = execute_hard_rules({"日期": "2025-11-12"}, [rule])[0]
    assert result["status"] == "pending"
    assert "日期规则" in result["message"]


def test_rule_condition_must_have_material_evidence():
    rule = {"id": "C1", "name": "六级成绩不低于425", "type": "range",
            "field": "六级成绩", "min": 425, "condition": "报考六级时",
            "severity": "critical"}
    pending = execute_hard_rules(
        {"六级成绩": 500, "_full_text": "大学英语四级报名材料"}, [rule])[0]
    passed = execute_hard_rules(
        {"六级成绩": 500, "_full_text": "大学英语六级报考材料"}, [rule])[0]
    assert pending["status"] == "pending"
    assert passed["passed"] is True


def test_fact_extraction_failure_turns_structured_rules_pending():
    rules = [
        {"id": "R1", "name": "成绩不低于80", "type": "range", "field": "成绩",
         "min": 80, "severity": "critical"},
        {"id": "R2", "name": "不得含模板", "type": "forbidden_keyword", "field": "正文",
         "keywords": ["模板"], "severity": "error"},
    ]
    results = execute_hard_rules(
        {"_extraction_failed": True, "_full_text": "这是一份干净材料"}, rules)
    assert results[0]["status"] == "pending"
    assert results[1]["status"] == "pass"


# ============================================================
# 评分
# ============================================================

def test_score_critical_deduction():
    hard = [{"passed": False, "not_applicable": False, "severity": "critical"}]
    total, grade, detail = compute_score(hard, {"表述规范性": {"评分": 100},
                                               "逻辑一致性": {"评分": 100}})
    assert detail["hard_score"] == 70
    assert total == int(70 * 0.6 + 100 * 0.4)   # 82
    assert grade == "未通过"


def test_score_ignores_not_applicable():
    hard = [{"passed": True, "not_applicable": True, "severity": "critical"}]
    _, _, detail = compute_score(hard, {})
    assert detail["hard_score"] is None          # 没有适用规则
    assert detail["has_hard_rules"] is False


def test_score_without_rules_does_not_fake_full_marks():
    """一条规则都没匹配上时，不能再像原来那样直接给 100 分。"""
    total, grade, detail = compute_score([], {"表述规范性": {"评分": 60},
                                             "逻辑一致性": {"评分": 60}})
    assert detail["hard_score"] is None
    assert total == 60 and grade == "无法审查"


def test_pending_rule_cannot_be_reported_as_passed():
    hard = [{"passed": False, "not_applicable": False, "needs_review": True,
             "status": "pending", "severity": "critical"}]
    total, grade, detail = compute_score(
        hard, {"表述规范性": {"评分": 100}, "逻辑一致性": {"评分": 100}})
    assert total == 100  # 仅是材料质量分，不代表资格通过
    assert grade == "待核验"
    assert detail["pending"] == 1 and detail["checked"] == 0


def test_no_evidence_has_no_numeric_score():
    total, grade, _ = compute_score([], {})
    assert total is None and grade == "无法审查"


def test_score_clamps_out_of_range_soft_scores():
    hard = [{"passed": True, "not_applicable": False, "severity": "error"}]
    total, _, detail = compute_score(hard, {"表述规范性": {"评分": 999},
                                           "逻辑一致性": {"评分": -50}})
    assert detail["soft_score"] == 50            # (100 + 0) / 2
    assert 0 <= total <= 100


def test_score_tolerates_garbage_soft_scores():
    hard = [{"passed": True, "not_applicable": False, "severity": "error"}]
    total, _, detail = compute_score(hard, {"表述规范性": {"评分": "很好"},
                                           "逻辑一致性": {}})
    assert detail["soft_score"] is None
    assert total == 100                          # 只用硬规则


@pytest.mark.parametrize("hard_fail_sev,expected_grade", [
    ("warning", "通过"),
])
def test_grade_boundaries(hard_fail_sev, expected_grade):
    hard = [{"passed": False, "not_applicable": False, "severity": hard_fail_sev}]
    _, grade, _ = compute_score(hard, {"表述规范性": {"评分": 100}, "逻辑一致性": {"评分": 100}})
    assert grade == expected_grade
