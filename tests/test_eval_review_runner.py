"""run_review 的纯逻辑部分（compare_verdicts）单测。

执行器要连服务，但「把系统输出与 gold 对齐、算准确率与假阳性」这段是纯逻辑，
必须能脱离服务单测 —— 否则指标算错了也没人发现。
"""

from eval.runners.run_review import compare_verdicts, to_verdict
from eval import judges


def test_to_verdict_three_classes():
    assert to_verdict({"not_applicable": True, "passed": False}) == judges.VERDICT_NA
    assert to_verdict({"not_applicable": False, "passed": True}) == judges.VERDICT_PASS
    assert to_verdict({"not_applicable": False, "passed": False}) == judges.VERDICT_FAIL
    assert to_verdict({"status": "pending", "needs_review": True,
                       "not_applicable": False, "passed": False}) == "pending"


def test_compare_verdicts_accuracy_and_false_positive():
    predicted = [
        {"rule_name": "R1", "passed": True, "not_applicable": False},   # gold fail → 错
        {"rule_name": "R2", "passed": False, "not_applicable": False},  # gold pass → 错（假阳性）
        {"rule_name": "R3", "passed": True, "not_applicable": False},   # gold pass → 对
    ]
    gold = [
        {"rule": "R1", "verdict": "fail"},
        {"rule": "R2", "verdict": "pass"},
        {"rule": "R3", "verdict": "pass"},
    ]
    r = compare_verdicts(predicted, gold)
    assert r["n_matched"] == 3
    assert r["accuracy"] == 0.3333
    # gold=pass 两条，其中 R2 被判 fail → 假阳性 1/2
    assert r["false_positive"] == {"fp": 1, "total": 2, "rate": 0.5}
    assert r["missing"] == [] and r["extra"] == []


def test_compare_verdicts_missing_and_extra():
    predicted = [{"rule_name": "A", "passed": True, "not_applicable": False},
                 {"rule_name": "多余", "passed": True, "not_applicable": False}]
    gold = [{"rule": "A", "verdict": "pass"}, {"rule": "未输出", "verdict": "pass"}]
    r = compare_verdicts(predicted, gold)
    assert [m["rule"] for m in r["missing"]] == ["未输出"]
    assert [e["rule"] for e in r["extra"]] == ["多余"]
    assert r["matched_accuracy"] == 1.0
    assert r["accuracy"] == 0.5       # 漏掉的 gold 规则必须计入分母
    assert r["coverage"] == 0.5


def test_compare_verdicts_no_gold_match_returns_none_accuracy():
    r = compare_verdicts([{"rule_name": "A", "passed": True, "not_applicable": False}], [])
    assert r["n_matched"] == 0 and r["accuracy"] is None
