"""eval/report.py 的纯逻辑用例。

为什么值得测一个小小报告脚本：
    项目约定「所有产物必须能由仓库内脚本复现，不许手写数字」。
    报告脚本一旦悄悄渲染出 None / 空字符串 / 未替换的占位符，
    表面上还是一份漂亮报告，但数字已经不可信了 —— 这正是最难被发现的一类问题。
    所以这里对「缺字段时不能崩、不能渲染成 None」做硬断言。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import report  # noqa: E402


def _qa_artifact():
    return {
        "manifest": {"case_set_version": "eval-1.0 2026-09-30",
                     "case_set_fingerprint": "abc123", "question_count": 2,
                     "generated_at": "2026-09-30 10:00:00"},
        "summary": {
            "overall": {"k": 1, "n": 2, "rate": 0.5, "ci95": [0.1, 0.9]},
            "overall_soft": {"k": 2, "n": 2, "rate": 1.0, "ci95": [0.3, 1.0]},
            "by_type": {
                "时间提取": {"k": 1, "n": 2, "rate": 0.5, "ci95": [0.1, 0.9],
                           "soft": 1.0, "partial": 1},
            },
            "refusal": {"correct": 1, "wrong_refusal": 1,
                        "missed_refusal": 0, "bad_answer": 0},
            "citation_precision": 1.0,
        },
        "results": [],
    }


def _review_artifact():
    return {
        "manifest": {"case_set_fingerprint": "def456", "case_count": 2,
                     "generated_at": "2026-09-30 10:00:00", "split": "all"},
        "summary": {
            "cases_ok": 1, "cases_error": 1,
            "verdict_accuracy": 0.5, "verdict_pairs": 2,
            "false_positive": {"fp": 1, "total": 2, "rate": 0.5},
            "errors": [{"id": "rv_002", "error": "boom"}],
        },
        "results": [
            {"id": "rv_001", "split": "dev", "domain": "团员推优", "score": 88,
             "grade": "合格", "accuracy": 0.5,
             "matched": [{"rule": "r1", "gold": "fail", "pred": "pass", "correct": False},
                         {"rule": "r2", "gold": "pass", "pred": "pass", "correct": True}],
             "missing": [{"rule": "r3", "gold": "fail"}],
             "extra": [], "false_positive": {"fp": 0, "total": 1, "rate": 0.0},
             "n_matched": 2},
            {"id": "rv_002", "split": "test", "error": "boom"},
        ],
    }


# ---------------- 格式化助手 ----------------

def test_pct_handles_none():
    assert report._pct(None) == "—"


def test_pct_formats_rate():
    assert report._pct(0.5161) == "51.6%"


def test_ci_formats_and_handles_missing():
    assert report._ci({"ci95": [0.1, 0.9]}) == "[10.0%, 90.0%]"
    assert report._ci({}) == ""


def test_esc_neutralizes_html():
    assert "<script>" not in report._esc("<script>alert(1)</script>")
    assert "&lt;script&gt;" in report._esc("<script>alert(1)</script>")


# ---------------- 渲染：缺产物 / 有产物 ----------------

def test_section_qa_without_artifact_is_graceful():
    out = report.section_qa({})
    assert "未找到问答评测产物" in out


def test_section_review_without_artifact_is_graceful():
    out = report.section_review({})
    assert "未找到审查评测产物" in out


def test_section_qa_renders_numbers_not_none():
    out = report.section_qa(_qa_artifact())
    assert "50.0%" in out            # 严格口径 0.5
    assert "100.0%" in out           # 宽松口径 1.0
    assert "None" not in out
    assert "abc123" in out           # 指纹必须出现，便于核对产物


def test_section_review_renders_error_row():
    out = report.section_review(_review_artifact())
    assert "报错" in out and "boom" in out
    assert "None" not in out


def test_section_review_counts_missed_fail():
    """漏判数 = gold 判 fail 却输出 pass + gold 判 fail 却没被路由到。

    这里构造 1 条 matched 漏判 + 1 条 missing 漏判，卡片里必须渲染成 2。
    """
    out = report.section_review(_review_artifact())
    assert '<div class="kpi" style="color:var(--err)">2</div>' in out


def test_section_defects_has_evidence_for_every_row():
    """缺陷清单是结论层 —— 每条都必须能定位到证据，否则就是空口结论。"""
    for d in report.DEFECTS:
        assert d.get("evidence"), f"{d['id']} 缺 evidence"
        assert d["severity"] in report.SEV_LABEL, f"{d['id']} severity 非法"
        assert d.get("title") and d.get("detail")
    out = report.section_defects()
    for d in report.DEFECTS:
        assert d["id"] in out and d["title"] in out
    assert "None" not in out


def test_defect_status_values_are_from_known_set():
    """状态只能是三种之一，防止写出「大概修了」这种不可核对的说法。"""
    for d in report.DEFECTS:
        head = d["status"].split("（")[0]
        assert head in {"已修复", "未修复", "待定"}, f"{d['id']} 状态异常：{d['status']}"
