"""eval.schema 题集加载与校验的单测。

这些断言守的是「题集本身是不是合格」——
题集一旦注水（重复 id、split 写错、既无 key_facts 又不要求拒答），
后续所有指标都失去意义，所以要在加载层就挡住。
"""

import json

import pytest

from eval.schema import (
    CASES_DIR, QACase, ReviewCase,
    filter_split, load_qa_cases, load_review_cases, qa_case_set_version,
)


def test_qa_case_set_loads_and_has_31_cases():
    cases = load_qa_cases()
    assert len(cases) == 31


def test_qa_case_ids_unique():
    ids = [c.id for c in load_qa_cases()]
    assert len(ids) == len(set(ids))


def test_qa_splits_are_valid_and_both_present():
    cases = load_qa_cases()
    assert all(c.split in ("dev", "test") for c in cases)
    assert filter_split(cases, "dev") and filter_split(cases, "test")


def test_qa_refusal_case_is_marked():
    refusal = [c for c in load_qa_cases() if c.expect_refusal]
    assert refusal, "至少要有一个 expect_refusal=True 的用例来测拒答"


def test_qa_version_present():
    assert qa_case_set_version().strip()


def test_review_case_loads():
    cases = load_review_cases()
    assert cases, "审查题集不能为空"
    for c in cases:
        assert c.material.strip(), f"{c.id} 材料为空"
        assert c.gold_verdicts, f"{c.id} 缺 gold_verdicts"
        for v in c.gold_verdicts:
            assert v["verdict"] in ("pass", "fail", "na")


def test_review_json_declares_known_issues():
    """已知缺陷要在题集里留档，避免修好后又被改回去。"""
    data = json.loads((CASES_DIR / "review_cases.json").read_text(encoding="utf-8"))
    ids = {i["id"] for i in data.get("known_issues", [])}
    assert {"BUG-1", "BUG-2"} <= ids


# ============================================================
# 构造期校验
# ============================================================

def test_qa_case_rejects_bad_split():
    with pytest.raises(ValueError):
        QACase(id="x", question="q", type="t", key_facts=["a"], split="prod")


def test_qa_case_rejects_no_facts_and_no_refusal():
    with pytest.raises(ValueError):
        QACase(id="x", question="q", type="t", key_facts=[], expect_refusal=False)


def test_review_case_rejects_bad_split():
    with pytest.raises(ValueError):
        ReviewCase(id="x", material="m", split="staging")
