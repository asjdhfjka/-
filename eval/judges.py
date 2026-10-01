"""评测判定器 —— 纯逻辑，不依赖模型 / 网络 / 向量库。

设计原则：
    每个判定函数都是「输入字符串 / 列表 → 输出结构化结论」的纯函数，
    因此可以独立单测，也能随时替换判定口径而不影响执行器。

本模块替代原 test_all.py 里的两段外挂判定：
    evaluate_strict / evaluate_loose（关键词逐字命中 + 宽松命中）。
新增三种更接近真实质量的判定：
    · 关键事实覆盖（key_facts）—— 不要求逐字，要求「事实被覆盖」；
    · 拒答混淆（该拒的拒了？不该拒的没拒？）；
    · 引用准确（[来源X] 是否落在有效编号内）。
审查侧还有「三分类判定」与「规则集合 P/R/F1」。
"""

import re

# ============================================================
# 常量：拒答话术
# ============================================================
# 与 eval_cases.REFUSAL_PHRASES 口径一致，但本模块不 import 它 ——
# 保持纯逻辑可独立测试，不引入任何项目内依赖。
REFUSAL_PHRASES = [
    "无法回答", "无法准确回答", "未找到", "资料中没有", "未提及",
    "没有找到", "未明确", "未说明", "没有相关",
]

# 拒答判定的长度上限：长回答里顺带出现「未找到」不算拒答。
DEFAULT_REFUSAL_MAX_LEN = 200

# 三分类判定的标签口径（与 review_engine 的语义对齐）
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_NA = "na"
VALID_VERDICTS = {VERDICT_PASS, VERDICT_FAIL, VERDICT_NA}

_CITE_RE = re.compile(r"\[来源\s*(\d+)\]")


# ============================================================
# 一、问答判定
# ============================================================

def is_refusal(answer: str, phrases=None, max_len: int = DEFAULT_REFUSAL_MAX_LEN) -> bool:
    """短回答 + 明确拒答话术，才算「拒答」。

    【为什么加长度限制】原实现只看有没有「未找到」等词，于是
    「资料里没有明确写 X，但根据 Y 可以推断 …（很长的正面回答）」会被误判成拒答。
    加长度上限后，只有「真的没答上来」才算拒答。
    """
    text = (answer or "").strip()
    if not text or text.startswith("[ERROR]"):
        return False
    if len(text) > max_len:
        return False
    return any(p in text for p in (phrases or REFUSAL_PHRASES))


def key_fact_coverage(answer: str, key_facts) -> dict:
    """关键事实覆盖：命中了几个 / 共几个。

    比「全部关键词逐字命中」宽松、比「命中一半」严谨 ——
    判定的是「事实有没有覆盖」，而不是「字符串有没有出现」。
    返回 full / partial / miss 三档，不给单点的布尔值，
    以便在汇总时保留「部分正确」这个中间态。
    """
    facts = [f for f in (key_facts or []) if str(f).strip()]
    text = str(answer or "")
    if not facts:
        return {"hits": 0, "total": 0, "ratio": 0.0, "verdict": "miss", "missed": []}

    missing = [f for f in facts if str(f) not in text]
    hits = len(facts) - len(missing)
    ratio = hits / len(facts)

    if hits == len(facts):
        verdict = "full"
    elif hits == 0:
        verdict = "miss"
    else:
        verdict = "partial"
    return {"hits": hits, "total": len(facts), "ratio": round(ratio, 4),
            "verdict": verdict, "missed": missing}


def judge_refusal(answer: str, expect_refusal: bool, phrases=None,
                  max_len: int = DEFAULT_REFUSAL_MAX_LEN) -> str:
    """拒答混淆：返回 correct / wrong_refusal / missed_refusal / bad_answer。

        expect_refusal=True  该拒答
        expect_refusal=False 该作答

        correct          判定正确（该拒的拒了 / 该答的答了）
        wrong_refusal    过度拒答（应作答却拒答）—— 最伤体验，单独标记
        missed_refusal   该拒未拒（答了一堆可能不靠谱的内容）
        bad_answer       报错或空
    """
    text = (answer or "").strip()
    if not text or text.startswith("[ERROR]"):
        return "bad_answer"
    refused = is_refusal(text, phrases, max_len)
    if expect_refusal:
        return "correct" if refused else "missed_refusal"
    return "wrong_refusal" if refused else "correct"


def extract_citations(answer: str) -> list:
    """抽取答案里出现的 [来源X] 编号（去重、保序）。"""
    seen, out = set(), []
    for m in _CITE_RE.finditer(str(answer or "")):
        idx = int(m.group(1))
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def judge_citations(answer: str, valid_indices) -> dict:
    """引用准确率：答案引用的编号里，有多少落在「本次实际给出的来源」范围内。

    用途：模型可能凭记忆写 [来源6]，而本次只给了 3 条来源 —— 这属于「编造引用」。
    """
    cited = extract_citations(answer)
    valid = {int(i) for i in (valid_indices or [])}
    if not cited:
        return {"cited": [], "valid": [], "invalid": [], "precision": None, "n": 0}
    ok = [i for i in cited if i in valid]
    bad = [i for i in cited if i not in valid]
    return {"cited": cited, "valid": ok, "invalid": bad,
            "precision": round(len(ok) / len(cited), 4), "n": len(cited)}


# ============================================================
# 二、审查判定
# ============================================================

def normalize_verdict(value) -> str:
    """把各种写法归一到 pass / fail / na。"""
    v = str(value or "").strip().lower()
    if v in ("pass", "passed", "通过", "true", "1"):
        return VERDICT_PASS
    if v in ("fail", "failed", "不通过", "未通过", "false", "0"):
        return VERDICT_FAIL
    if v in ("na", "not_applicable", "n/a", "不适用"):
        return VERDICT_NA
    return v


def judge_verdict(predicted, gold) -> bool:
    """单条判定是否与 gold 一致（三分类：pass / fail / na）。

    【为什么是三分类而不是布尔】review_engine 的核心语义就是
    「不适用 ≠ 失败」——把「材料里没有这一项」当成「不合格」会让合格材料被冤枉。
    评测必须把这个中间态单拎出来，否则测不出这一类错误。
    """
    return normalize_verdict(predicted) == normalize_verdict(gold)


def judge_rule_sets(predicted_rules, gold_rules, key: str = "name") -> dict:
    """规则抽取质量：predicted 规则集合 vs gold 规则集合的 P/R/F1。

    以 `key` 字段（默认规则名）做匹配键 —— 抽取出的规则名可能带噪声，
    所以比较前先做去空白归一。
    """
    def norm(s):
        return re.sub(r"\s+", "", str(s or ""))

    pred = {norm(r.get(key)) for r in (predicted_rules or []) if r.get(key)}
    gold = {norm(r.get(key)) for r in (gold_rules or []) if r.get(key)}

    if not pred and not gold:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "tp": 0, "fp": 0, "fn": 0}
    tp = len(pred & gold)
    fp = len(pred - gold)
    fn = len(gold - pred)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4), "tp": tp, "fp": fp, "fn": fn}


def false_positive_rate(verdict_pairs, positive_label=VERDICT_PASS) -> dict:
    """假阳性率：把「合格」的材料判成「不合格」的比例。

    verdict_pairs: [(predicted, gold), ...]
    对审查系统而言，假阳性（冤枉合格材料）比假阴性更致命 ——
    这正是「LLM 抽规则 + 确定性引擎」相对「纯 LLM 审查」要证明的优势点。
    """
    fp = 0
    total_pass = 0
    for pred, gold in verdict_pairs or []:
        g, p = normalize_verdict(gold), normalize_verdict(pred)
        if g == positive_label:
            total_pass += 1
            if p != positive_label:
                fp += 1
    return {"fp": fp, "total": total_pass,
            "rate": round(fp / total_pass, 4) if total_pass else None}
