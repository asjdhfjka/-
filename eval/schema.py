"""题集数据结构与加载器 —— 纯逻辑，仅标准库。

题集从「三元组 (问题, 关键词, 类型)」升级为带完整标注的结构：
    · key_facts      必须覆盖的事实（不是逐字匹配）
    · gold_answer    人工参考答案（供人工/LLM 复核）
    · gold_sources   正确来源文档（算 Recall@k / MRR / nDCG 用）
    · expect_refusal 该不该拒答（算拒答混淆用）
    · split          dev / test 切分（防止拿 test 集调参 = 自己给自己打分）

审查题集额外带：
    · gold_rules     人工抽的规则（测规则抽取质量）
    · gold_verdicts  每条规则应有的三分类判定（pass / fail / na）
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases"

# 可用标签：dev 用于调参，test 冻结、最后才跑一次
VALID_SPLITS = {"dev", "test"}


# ============================================================
# 问答
# ============================================================

@dataclass
class QACase:
    id: str
    question: str
    type: str
    key_facts: list = field(default_factory=list)
    gold_answer: str = ""
    gold_sources: list = field(default_factory=list)
    expect_refusal: bool = False
    split: str = "dev"
    notes: str = ""

    def __post_init__(self):
        if self.split not in VALID_SPLITS:
            raise ValueError(f"未知 split：{self.split}（应为 dev/test）")
        if not self.key_facts and not self.expect_refusal:
            raise ValueError(f"用例 {self.id} 既无 key_facts 也不要求拒答")


def load_qa_cases(path=None) -> list:
    """读取问答题集 JSON，返回 [QACase]。"""
    path = Path(path) if path else CASES_DIR / "qa_cases.json"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [QACase(
        id=c["id"], question=c["question"], type=c["type"],
        key_facts=list(c.get("key_facts") or []),
        gold_answer=c.get("gold_answer", ""),
        gold_sources=list(c.get("gold_sources") or []),
        expect_refusal=bool(c.get("expect_refusal", False)),
        split=c.get("split", "dev"),
        notes=c.get("notes", ""),
    ) for c in data.get("cases", [])]
    _assert_unique([c.id for c in cases], "问答题集")
    return cases


# ============================================================
# 审查
# ============================================================

@dataclass
class ReviewCase:
    id: str
    material: str                 # 材料全文（文本）
    gold_domain: str = ""
    gold_scene: str = ""
    gold_rules: list = field(default_factory=list)
    gold_verdicts: list = field(default_factory=list)   # [{rule, verdict}]
    split: str = "dev"
    notes: str = ""

    def __post_init__(self):
        if self.split not in VALID_SPLITS:
            raise ValueError(f"未知 split：{self.split}（应为 dev/test）")


def load_review_cases(path=None) -> list:
    path = Path(path) if path else CASES_DIR / "review_cases.json"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [ReviewCase(
        id=c["id"], material=c["material"],
        gold_domain=c.get("gold_domain", ""), gold_scene=c.get("gold_scene", ""),
        gold_rules=list(c.get("gold_rules") or []),
        gold_verdicts=list(c.get("gold_verdicts") or []),
        split=c.get("split", "dev"), notes=c.get("notes", ""),
    ) for c in data.get("cases", [])]
    _assert_unique([c.id for c in cases], "审查题集")
    return cases


# ============================================================
# 通用
# ============================================================

def case_set_fingerprint(cases) -> str:
    """题集指纹：由用例内容决定，改任何一条用例都会变。

    「产物是用哪套题跑出来的」全靠这个指纹兜底，所以**执行器写产物**与
    **报告校验产物**必须用同一个实现 —— 两边算法一旦漂移，报告会把本来
    配套的产物误判成不配套（反之亦然），这道防线就废了。
    """
    payload = json.dumps([c.__dict__ for c in cases], ensure_ascii=False, sort_keys=True)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]


def qa_case_set_version(path=None) -> str:
    """题集版本号；每改题集必须递增，产物指纹依赖它。"""
    path = Path(path) if path else CASES_DIR / "qa_cases.json"
    return json.loads(Path(path).read_text(encoding="utf-8")).get("version", "")


def _assert_unique(ids, name):
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"{name}里存在重复 id：{sorted(dup)}")


def filter_split(cases, split: str) -> list:
    """按 dev/test 过滤。调参只用 dev，test 冻结。"""
    return [c for c in cases if c.split == split]
