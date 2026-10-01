"""eval.metrics 纯逻辑单测。"""

import pytest

from eval import metrics


def test_accuracy_and_empty_denominator():
    assert metrics.accuracy(3, 4) == 0.75
    assert metrics.accuracy(0, 0) is None


def test_precision_recall_f1():
    r = metrics.precision_recall_f1(tp=1, fp=1, fn=1)
    assert r["precision"] == 0.5 and r["recall"] == 0.5 and r["f1"] == 0.5


def test_precision_recall_f1_no_positives():
    assert metrics.precision_recall_f1(0, 0, 0)["f1"] is None


def test_confusion_summary_per_label():
    pairs = [("pass", "pass"), ("fail", "pass"), ("fail", "fail"), ("pass", "fail")]
    s = metrics.confusion_summary(pairs, labels=["pass", "fail"])
    assert s["n"] == 4 and s["accuracy"] == 0.5
    assert s["per_label"]["pass"]["tp"] == 1 and s["per_label"]["pass"]["fn"] == 1
    assert s["per_label"]["fail"]["tp"] == 1 and s["per_label"]["fail"]["fp"] == 1


# ============================================================
# Wilson 置信区间：小样本 + 极端比例必须落在 [0,1] 内
# ============================================================

def test_wilson_interval_bounds():
    ci = metrics.wilson_interval(29, 30)
    assert 0.0 <= ci["low"] < ci["p"] < ci["high"] <= 1.0


def test_wilson_interval_extreme_does_not_exceed_one():
    ci = metrics.wilson_interval(30, 30)
    assert ci["high"] <= 1.0 and ci["low"] > 0.8


def test_wilson_interval_small_sample_is_wide():
    """30 题上的区间必须足够宽 —— 这正是「83% vs 96% 是噪声」的量化依据。"""
    ci = metrics.wilson_interval(25, 30)
    assert (ci["high"] - ci["low"]) > 0.2


def test_wilson_interval_zero_n():
    assert metrics.wilson_interval(0, 0)["p"] is None


# ============================================================
# Kappa
# ============================================================

def test_cohen_kappa_perfect_agreement():
    assert metrics.cohen_kappa(["a", "b", "a"], ["a", "b", "a"])["kappa"] == 1.0


def test_cohen_kappa_known_value():
    # p_o=2/3, p_e=4/9 → kappa=0.4
    r = metrics.cohen_kappa(["a", "b", "a"], ["a", "b", "b"])
    assert r["kappa"] == pytest.approx(0.4, abs=1e-4)


def test_cohen_kappa_length_mismatch():
    with pytest.raises(ValueError):
        metrics.cohen_kappa(["a"], ["a", "b"])


# ============================================================
# 检索指标
# ============================================================

def test_recall_at_k():
    assert metrics.recall_at_k(["d1", "d2", "d3"], ["d1", "d3"], 2) == 0.5
    assert metrics.recall_at_k(["d1", "d3"], ["d1", "d3"], 5) == 1.0
    assert metrics.recall_at_k(["x"], []) is None


def test_reciprocal_rank():
    assert metrics.reciprocal_rank(["x", "d1"], ["d1"]) == 0.5
    assert metrics.reciprocal_rank(["d1"], ["d1"]) == 1.0
    assert metrics.reciprocal_rank(["x", "y"], ["d1"]) == 0.0


def test_mean_reciprocal_rank():
    assert metrics.mean_reciprocal_rank([(["d1"], ["d1"]), (["x", "d1"], ["d1"])]) == 0.75


def test_ndcg_at_k():
    assert metrics.ndcg_at_k(["d1", "x"], ["d1"]) == 1.0
    assert metrics.ndcg_at_k(["x", "d1"], ["d1"]) == pytest.approx(0.6309, abs=1e-4)
    assert metrics.ndcg_at_k(["x"], []) is None


# ============================================================
# 聚合
# ============================================================

def test_group_by():
    g = metrics.group_by([{"t": "a"}, {"t": "b"}, {"t": "a"}], lambda x: x["t"])
    assert set(g) == {"a", "b"} and len(g["a"]) == 2


def test_rate_with_ci_shape():
    r = metrics.rate_with_ci(8, 10)
    assert r["k"] == 8 and r["n"] == 10 and r["rate"] == 0.8
    assert len(r["ci95"]) == 2 and r["ci95"][0] < 0.8 < r["ci95"][1]
