"""eval.judges 纯逻辑单测。"""

import pytest

from eval import judges


# ============================================================
# 拒答识别
# ============================================================

def test_is_refusal_short_refusal_true():
    assert judges.is_refusal("根据现有资料，我无法回答这个问题。") is True


def test_is_refusal_long_answer_is_not_refusal():
    """长回答里顺带出现「未找到」不算拒答 —— 原实现会误判。"""
    long_answer = "资料中未找到直接规定，但根据报名通知，你需要准备学号并登录报名网站核对信息。" * 10
    assert judges.is_refusal(long_answer) is False


def test_is_refusal_empty_and_error():
    assert judges.is_refusal("") is False
    assert judges.is_refusal("[ERROR] boom") is False


# ============================================================
# 关键事实覆盖
# ============================================================

def test_key_fact_coverage_full_partial_miss():
    facts = ["报名时间", "3月", "上半年"]
    assert judges.key_fact_coverage("报名时间是3月，上半年进行", facts)["verdict"] == "full"
    part = judges.key_fact_coverage("报名时间在3月", facts)
    assert part["verdict"] == "partial" and part["hits"] == 2 and part["missed"] == ["上半年"]
    assert judges.key_fact_coverage("完全无关", facts)["verdict"] == "miss"


def test_key_fact_coverage_no_facts():
    r = judges.key_fact_coverage("随便", [])
    assert r["total"] == 0 and r["verdict"] == "miss"


# ============================================================
# 拒答混淆
# ============================================================

@pytest.mark.parametrize("answer,expect,label", [
    ("根据现有资料，我无法回答这个问题。", True, "correct"),
    ("报名时间是3月15日。", True, "missed_refusal"),
    ("根据现有资料，我无法回答这个问题。", False, "wrong_refusal"),
    ("报名时间是3月15日。", False, "correct"),
    ("", False, "bad_answer"),
    ("[ERROR] timeout", True, "bad_answer"),
])
def test_judge_refusal(answer, expect, label):
    assert judges.judge_refusal(answer, expect) == label


# ============================================================
# 引用
# ============================================================

def test_extract_citations_dedup_and_order():
    assert judges.extract_citations("见 [来源1] 和 [来源3]，还有 [来源1]") == [1, 3]
    assert judges.extract_citations("没有引用") == []


def test_judge_citations_precision():
    r = judges.judge_citations("见 [来源1] [来源3]", [1, 2])
    assert r["valid"] == [1] and r["invalid"] == [3]
    assert r["precision"] == 0.5 and r["n"] == 2


def test_judge_citations_none_when_no_citation():
    r = judges.judge_citations("无引用", [1, 2])
    assert r["n"] == 0 and r["precision"] is None


# ============================================================
# 审查三分类
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("pass", "pass"), ("PASSED", "pass"), ("通过", "pass"),
    ("fail", "fail"), ("不通过", "fail"), ("未通过", "fail"),
    ("na", "na"), ("not_applicable", "na"), ("不适用", "na"),
])
def test_normalize_verdict(raw, expected):
    assert judges.normalize_verdict(raw) == expected


def test_judge_verdict_三分类区分不适用与失败():
    """「不适用」与「失败」必须分开 —— 把不适用当失败会冤枉合格材料。"""
    assert judges.judge_verdict("na", "na") is True
    assert judges.judge_verdict("fail", "na") is False
    assert judges.judge_verdict(True, "pass") is True


# ============================================================
# 规则集合 P/R/F1
# ============================================================

def test_judge_rule_sets_prf1():
    pred = [{"name": "A"}, {"name": "B"}]
    gold = [{"name": "A"}, {"name": "C"}]
    r = judges.judge_rule_sets(pred, gold)
    assert r["tp"] == 1 and r["fp"] == 1 and r["fn"] == 1
    assert r["precision"] == 0.5 and r["recall"] == 0.5 and r["f1"] == 0.5


def test_judge_rule_sets_normalizes_whitespace():
    r = judges.judge_rule_sets([{"name": " A B "}], [{"name": "AB"}])
    assert r["tp"] == 1


def test_judge_rule_sets_both_empty_is_perfect():
    r = judges.judge_rule_sets([], [])
    assert r["f1"] == 1.0


# ============================================================
# 假阳性率
# ============================================================

def test_false_positive_rate():
    pairs = [("fail", "pass"), ("pass", "pass"), ("na", "pass"), ("fail", "fail")]
    r = judges.false_positive_rate(pairs)
    # gold=pass 的有 3 条，其中被判非 pass 的 2 条
    assert r["total"] == 3 and r["fp"] == 2
    assert r["rate"] == pytest.approx(0.6667, abs=1e-4)


def test_false_positive_rate_no_positive():
    r = judges.false_positive_rate([("fail", "fail")])
    assert r["rate"] is None
