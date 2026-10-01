"""评测产物与题集的配套性校验（新评测体系版）。

为什么需要这道防线：
    改造前 test_results.json 是用一段**当前已被注释掉**的题目集跑出来的，
    报告引用的数据用仓库里的脚本复现不出来 —— 「用旧数据冒充新结论」。
    现在题集固定在 eval/cases/*.json，产物由 eval/runners/* 生成并带题集指纹。
    本文件守两道：
      1. 题集本身结构正确、id 唯一、版本号非空，且指纹确实是「内容的函数」；
      2. 产物里的指纹必须等于按当前题集重算的指纹 —— 由 report.artifact_fingerprint_mismatch
         判定，报告脚本 main() 也用它拒绝出报告。

（旧版校验的是 test_all.py 的 test_results.json；那套题集与脚本已废弃，
 故本文件整体改为针对 eval/ 体系。见改动说明第八节。）
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import report  # noqa: E402
from eval.schema import (case_set_fingerprint, load_qa_cases,  # noqa: E402
                         load_review_cases, qa_case_set_version)


# ---------------- 题集结构 ----------------

def test_case_sets_load_and_have_unique_ids():
    qa = load_qa_cases()
    rv = load_review_cases()
    assert qa, "问答/审查题集为空"
    assert rv, "审查题集为空"
    assert len({c.id for c in qa}) == len(qa), "问答题集存在重复 id"
    assert len({c.id for c in rv}) == len(rv), "审查题集存在重复 id"


def test_case_set_version_is_present():
    assert qa_case_set_version().strip(), "题集缺少版本号，改题集必须递增版本"


def test_fingerprint_is_content_addressed():
    """指纹必须是「内容的函数」：改一条用例，指纹就要变，否则形同虚设。"""
    cases = load_qa_cases()
    before = case_set_fingerprint(cases)
    cases[0].question += "（测试改动）"
    assert case_set_fingerprint(cases) != before


# ---------------- 产物与题集配套 ----------------

def test_matching_artifact_passes():
    cases = load_qa_cases()
    artifact = {"manifest": {"case_set_fingerprint": case_set_fingerprint(cases)}}
    assert report.artifact_fingerprint_mismatch("qa", artifact) == ""


def test_stale_artifact_is_rejected():
    artifact = {"manifest": {"case_set_fingerprint": "deadbeefcafe"}}
    msg = report.artifact_fingerprint_mismatch("qa", artifact)
    assert msg and "deadbeefcafe" in msg


def test_artifact_without_fingerprint_is_rejected():
    assert report.artifact_fingerprint_mismatch("qa", {"manifest": {}})
    assert report.artifact_fingerprint_mismatch("review", {})


@pytest.mark.parametrize("name,path", [
    ("qa", report.QA_ARTIFACT),
    ("review", report.REVIEW_ARTIFACT),
])
def test_committed_artifact_is_in_sync(name, path):
    """仓库里现存的产物必须与当前题集配套；不配套就让 CI 红。"""
    if not path.exists():
        pytest.skip(f"{path.name} 不存在（尚未跑过对应评测）")
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert report.artifact_fingerprint_mismatch(name, artifact) == ""
