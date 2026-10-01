"""真实规则库（auto_rules.json）的数据质量守卫。

为什么需要这组用例：
    规则抽取阶段会把「制度条文」错编成可执行规则，而这类错误**不会让引擎报错**，
    只会静默地把合格材料判成不合格（用户实测：一份没有挂科的综测材料被判「存在挂科」）。
    其余单测覆盖的是引擎内部逻辑，用的是合成配置，所以全绿；
    缺陷长在「规则数据 ↔ 引擎语义」的接缝上，因此这里直接对真实规则库设防。

守卫的四类（对应 eval/report.py 缺陷清单 D11 / D12 / D13）：
    D11 否定语义写成正向关键词 —— 或落到 required 上
    D12 关键词只存在于制度里，对材料永远不成立（应判「不适用」）
    D13 加分条款被抽成 critical 资格条款
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from review_engine import META_KEYWORDS, execute_hard_rules   # noqa: E402

RULES_PATH = ROOT / "auto_rules.json"

ALLOWED_TYPES = {
    "required", "regex", "range", "keyword", "forbidden_keyword", "enum", "status",
}
ALLOWED_SEVERITIES = {"critical", "error", "warning"}

# 否定式禁令的判别标记。
# 刻意不收录「无 / 未」这类单字：它们会把「未来」「无线电」等正常词误伤
# （实测「艺起向未来」就会被裸「未」命中）。
NEGATION_MARKERS = ("不得", "禁止", "严禁", "不能", "不允许", "免于")

# 这些场景核验的是「学生/材料自身的事实」，不是「文档是否载明某条禁令」。
# 否定式 keyword（要求文档写明确禁令）落到这些场景上，必然对材料不成立。
FACT_SCENES = {"资格审查", "票根", "选票"}


def _load_rules():
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    out = []
    for domain, bucket in (data.get("domains") or {}).items():
        for rule in bucket.get("rules") or []:
            out.append((domain, rule))
    for rule in data.get("global_rules") or []:
        out.append(("__global__", rule))
    return out


ALL_RULES = _load_rules()


def _scenes(rule):
    scene = rule.get("scene") or []
    if isinstance(scene, str):
        scene = [scene]
    return {str(s).strip() for s in scene if str(s).strip()}


def test_rule_library_is_non_trivial():
    """守卫本身要覆盖面足够大，否则「全绿」没有意义。"""
    assert len(ALL_RULES) > 100, f"只读到 {len(ALL_RULES)} 条规则，路径或结构可能变了"


def test_every_rule_has_supported_type_and_severity():
    bad = [(f"{d}/{r.get('id')}", r.get("type"))
           for d, r in ALL_RULES if r.get("type") not in ALLOWED_TYPES]
    assert not bad, f"存在引擎不支持的规则类型：{bad}"

    bad = [(f"{d}/{r.get('id')}", r.get("severity"))
           for d, r in ALL_RULES if r.get("severity") not in ALLOWED_SEVERITIES]
    assert not bad, f"存在未知严重度：{bad}"


def test_negation_is_not_encoded_as_keyword_on_fact_scenes():
    """D12 续：要求文档「载明某条禁令」的 keyword，不能挂到核验学生事实的场景上。

    用户实测：`四六级考试报名_R004` 关键词是通知原文「英语四级与英语六级不得同时报考」，
    这条本身没错，但它被挂在 scene=["资格审查"] —— 审学生报考资格时当然找不到这句通知用语，
    于是必然判违规。正确做法是把 scene 改成「通知公告」。
    """
    offenders = []
    for domain, rule in ALL_RULES:
        if rule.get("type") != "keyword":
            continue
        hits = [str(kw) for kw in (rule.get("keywords") or [])
                if any(m in str(kw) for m in NEGATION_MARKERS)]
        if not hits:
            continue
        wrong = _scenes(rule) & FACT_SCENES
        if wrong:
            offenders.append(
                f"{domain}/{rule.get('id')} scene={sorted(wrong)} 关键词={hits}")
    assert not offenders, (
        "否定式 keyword 落在了核验学生事实的场景上，必然误判：\n  "
        + "\n  ".join(offenders)
    )


def test_negation_is_not_encoded_as_required():
    """D11：required 的语义是「材料必须提供该字段」，与禁令语义相冲突。

    required 在字段抽不到时会直接判失败，于是一条「选票打勾不得碰框涂改」的禁令，
    会因为「选票勾记没被抽取出来」把合格材料判成不合格 —— 与「无挂科」误判同族。
    """
    offenders = []
    for domain, rule in ALL_RULES:
        if rule.get("type") != "required":
            continue
        name = str(rule.get("name") or "")
        if any(m in name for m in NEGATION_MARKERS):
            offenders.append(f"{domain}/{rule.get('id')} {name}")
    assert not offenders, (
        "required 型规则名含禁令语义，字段缺失即误判，应改用 status / forbidden_keyword：\n  "
        + "\n  ".join(offenders)
    )


def test_status_rules_are_fully_configured():
    """status 型必须同时给出正反状态词，否则引擎只能退回「不适用/待核验」。"""
    offenders = []
    for domain, rule in ALL_RULES:
        if rule.get("type") != "status":
            continue
        if not (rule.get("pass_values") and rule.get("fail_values")):
            offenders.append(f"{domain}/{rule.get('id')} {rule.get('name')}")
    assert not offenders, (
        "status 型规则缺少 pass_values / fail_values：\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("rule_id", [
    "综合素质测评_R004",   # 总积分权重计算正确：约束对象写在 field="总积分计算公式" 里
])
def test_meta_rules_are_not_applicable(rule_id):
    """D12：约束「评比口径本身」的规则，对材料应判「不适用」而不是违规。

    引擎原先只在 rule_name 里找 META_KEYWORDS，而这条规则名叫「…计算正确」、
    「公式」写在 field 上，于是兜底失效 → 必然扣分。现在判定范围含 field。
    """
    matched = [r for _d, r in ALL_RULES if r.get("id") == rule_id]
    assert matched, f"{rule_id} 不在规则库里，用例已过期"
    result = execute_hard_rules({}, matched)[0]
    assert result["not_applicable"] is True, (
        f"{rule_id} 约束的是评比口径本身，应判「不适用」，实际 status={result['status']}"
    )


def test_meta_keyword_rules_are_all_not_applicable():
    """把上一条推广到全库：凡是 name/field 命中 META_KEYWORDS 的规则都必须是不适用。"""
    offenders = []
    for domain, rule in ALL_RULES:
        blob = f"{rule.get('name') or ''}{rule.get('field') or ''}"
        if not any(kw in blob for kw in META_KEYWORDS):
            continue
        result = execute_hard_rules({}, [rule])[0]
        if result["not_applicable"] is not True:
            offenders.append(
                f"{domain}/{rule.get('id')} {rule.get('name')} → status={result['status']}")
    assert not offenders, (
        "命中 META_KEYWORDS 的规则没有被判为「不适用」，会把材料误扣分：\n  "
        + "\n  ".join(offenders)
    )
