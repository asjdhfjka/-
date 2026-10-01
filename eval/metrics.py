"""评测指标 —— 纯逻辑，仅标准库。

为什么要单独成模块：
    原脚本只算了一个「通过率」，无法回答评委必然会问的两件事：
      · 「样本才 30 题，83% 和 96% 差多少？」→ 需要置信区间；
      · 「标注是不是拍脑袋？」→ 需要 Kappa；
    也没有检索侧指标（Recall@k / MRR / nDCG），
    导致「检索错」和「生成错」分不开。

    这里把指标做成独立纯函数，全部可单测、无第三方依赖。
"""

import math


# ============================================================
# 一、基础计数指标
# ============================================================

def accuracy(correct: int, total: int):
    """准确率；分母为 0 时返回 None（而不是 0，避免「没测」被当成「全错」）。"""
    if not total:
        return None
    return round(correct / total, 4)


def precision_recall_f1(tp: int, fp: int, fn: int) -> dict:
    """标准 P/R/F1。tp/fp/fn 全 0 时视为「无正例且无预测」，返回 None。"""
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = 0.0 if (precision is not None and recall is not None) else None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
    }


def confusion_matrix(pairs, labels=None) -> dict:
    """混淆矩阵。pairs: [(predicted, gold), ...] → {gold: {predicted: count}}。"""
    labels = list(labels) if labels else sorted({p for pair in pairs for p in pair})
    matrix = {g: {p: 0 for p in labels} for g in labels}
    for pred, gold in pairs:
        if gold in matrix and pred in matrix[gold]:
            matrix[gold][pred] += 1
    return matrix


def confusion_summary(pairs, labels=None) -> dict:
    """从混淆矩阵导出逐类 P/R/F1 与整体准确率。"""
    labels = list(labels) if labels else sorted({p for pair in pairs for p in pair})
    matrix = confusion_matrix(pairs, labels)
    per_label = {}
    total = sum(sum(row.values()) for row in matrix.values())
    correct = sum(matrix[l][l] for l in labels if l in matrix)

    for label in labels:
        tp = matrix.get(label, {}).get(label, 0)
        fp = sum(matrix[g][label] for g in labels if g != label and label in matrix.get(g, {}))
        fn = sum(matrix.get(label, {}).get(p, 0) for p in labels if p != label)
        per_label[label] = {"tp": tp, "fp": fp, "fn": fn,
                            **precision_recall_f1(tp, fp, fn)}
    return {"labels": labels, "matrix": matrix, "per_label": per_label,
            "accuracy": accuracy(correct, total), "n": total}


# ============================================================
# 二、小样本置信区间（评委必问：30 题上的 83% 可信吗）
# ============================================================

def wilson_interval(k: int, n: int, z: float = 1.96) -> dict:
    """Wilson 置信区间（默认 95%）。

    【为什么用 Wilson 而不是正态近似】
    30 题、通过率接近 1 时，正态近似的区间会越出 [0,1]，
    且小样本下明显偏窄。Wilson 区间在极端比例与小样本下都稳定，
    是报告「通过率 ± 多少」时更该用的口径。
    """
    if n <= 0:
        return {"k": k, "n": n, "p": None, "low": None, "high": None}
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return {
        "k": k, "n": n, "p": round(p, 4),
        "low": round(max(0.0, center - half), 4),
        "high": round(min(1.0, center + half), 4),
    }


def cohen_kappa(labels_a, labels_b) -> dict:
    """Cohen's Kappa（两名标注者一致性）。

    【为什么要它】评审会问「你的 gold 是不是一个人随便标的」。
    Kappa 把「碰巧一致」扣掉，是证明标注可信的标准做法。
    """
    if len(labels_a) != len(labels_b):
        raise ValueError("两组标注长度不一致")
    n = len(labels_a)
    if n == 0:
        return {"kappa": None, "p_o": None, "p_e": None, "n": 0}

    cats = sorted(set(labels_a) | set(labels_b))
    p_o = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / n
    p_e = 0.0
    for c in cats:
        pa = sum(1 for a in labels_a if a == c) / n
        pb = sum(1 for b in labels_b if b == c) / n
        p_e += pa * pb
    kappa = (p_o - p_e) / (1 - p_e) if (1 - p_e) else None
    return {"kappa": round(kappa, 4) if kappa is not None else None,
            "p_o": round(p_o, 4), "p_e": round(p_e, 4), "n": n}


# ============================================================
# 三、检索指标（区分「检索错」与「生成错」）
# ============================================================
# retrieved / relevant 均为「文档标识」列表（字符串）。

def recall_at_k(retrieved, relevant, k: int = None) -> float:
    """前 k 条里命中了多少比例的 gold 文档。"""
    rel = set(relevant or [])
    if not rel:
        return None
    top = list(retrieved or [])[:k] if k else list(retrieved or [])
    return round(len(set(top) & rel) / len(rel), 4)


def reciprocal_rank(retrieved, relevant) -> float:
    """第一个命中 gold 的位置的倒数（MRR 的单条分量）。"""
    rel = set(relevant or [])
    for i, doc in enumerate(retrieved or [], 1):
        if doc in rel:
            return round(1.0 / i, 4)
    return 0.0


def mean_reciprocal_rank(cases) -> float:
    """MRR。cases: [(retrieved, relevant), ...]"""
    cases = list(cases or [])
    if not cases:
        return None
    return round(sum(reciprocal_rank(r, rel) for r, rel in cases) / len(cases), 4)


def ndcg_at_k(retrieved, relevant, k: int = None) -> float:
    """二值相关的 nDCG@k。

    DCG  = Σ rel_i / log2(i+1)
    IDCG = 理想排序下的 DCG
    """
    rel = set(relevant or [])
    if not rel:
        return None
    top = list(retrieved or [])[:k] if k else list(retrieved or [])

    dcg = sum(1.0 / math.log2(i + 1) for i, d in enumerate(top, 1) if d in rel)
    ideal_hits = min(len(rel), len(top)) if k else len(rel)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return round(dcg / idcg, 4) if idcg else None


# ============================================================
# 四、聚合辅助
# ============================================================

def group_by(items, key):
    """按 key(item) 分组，返回 {分组: [items]}。用于「分类型报告」。"""
    out = {}
    for it in items or []:
        out.setdefault(key(it), []).append(it)
    return out


def rate_with_ci(k: int, n: int) -> dict:
    """通过率 + 95% 置信区间，一次性给全，方便直接写进报告。"""
    ci = wilson_interval(k, n)
    return {"k": k, "n": n, "rate": ci["p"], "ci95": [ci["low"], ci["high"]]}
