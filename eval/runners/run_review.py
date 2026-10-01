"""审查评测执行器。

用法：
    1. 先启动服务（默认 8001，可用 TEST_BASE 覆盖）
    2. python eval/runners/run_review.py [--split dev|test|all]

它在测什么：
    对每个审查用例，把材料作为 .txt 上传到 /review，取回「硬规则校验」逐条结果，
    与题集里的 gold 三分类（pass/fail/na）对齐，算：
      · 判定准确率（三分类）
      · 假阳性率 —— 把「合格」判成「不合格」的比例（审查系统最致命的错误）
      · 逐规则命中明细（用于定位是抽取错还是校验错）

注意：若 /review 整体报错（例如触发已知的公式型 min 崩溃），
该用例记为 error，不会中断其余用例 —— 这本身就是一条重要结论。
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import judges, metrics                       # noqa: E402
from eval.schema import case_set_fingerprint, filter_split, load_review_cases  # noqa: E402

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001").rstrip("/")
OUT = ROOT / "eval_results_review.json"
USERNAME = os.getenv("TEST_USERNAME", "student")
PASSWORD = os.getenv("TEST_PASSWORD", "Demo@123456")
SESSION = None


def login_session():
    import requests
    global SESSION
    SESSION = requests.Session()
    response = SESSION.post(
        f"{BASE}/auth/login", json={"username": USERNAME, "password": PASSWORD}, timeout=15)
    response.raise_for_status()


def _norm(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def to_verdict(item: dict) -> str:
    """把 /review 返回的单条硬规则结果转成判定标签；pending 单独保留。"""
    if item.get("needs_review") or item.get("status") == "pending":
        return "pending"
    if item.get("not_applicable"):
        return judges.VERDICT_NA
    return judges.VERDICT_PASS if item.get("passed") else judges.VERDICT_FAIL


def compare_verdicts(predicted_rules, gold_verdicts) -> dict:
    """把「系统输出的规则结果」与「gold 判定」按规则名对齐，算准确率与假阳性。

    predicted_rules: /review 的「硬规则校验」列表
    gold_verdicts:   [{rule, verdict}, ...]
    返回：matched（逐条）、missing（gold 有但系统没输出）、extra、accuracy、fpr
    """
    pred_by_name = {_norm(r.get("rule_name", "")): to_verdict(r) for r in (predicted_rules or [])}
    matched, missing = [], []
    pairs = []
    for g in gold_verdicts or []:
        name = _norm(g.get("rule"))
        if name in pred_by_name:
            pred = pred_by_name[name]
            ok = judges.judge_verdict(pred, g["verdict"])
            matched.append({"rule": g["rule"], "gold": g["verdict"],
                            "pred": pred, "correct": ok})
            pairs.append((pred, g["verdict"]))
        else:
            missing.append({"rule": g["rule"], "gold": g["verdict"]})

    used = {_norm(g.get("rule")) for g in (gold_verdicts or [])}
    extra = [{"rule": r.get("rule_name"), "pred": to_verdict(r)}
             for r in (predicted_rules or []) if _norm(r.get("rule_name", "")) not in used]

    correct = sum(1 for m in matched if m["correct"])
    gold_count = len(gold_verdicts or [])
    return {
        "matched": matched, "missing": missing, "extra": extra,
        # 漏掉的 gold 规则同样算错，避免只统计成功匹配项造成虚高。
        "accuracy": metrics.accuracy(correct, gold_count),
        "matched_accuracy": metrics.accuracy(correct, len(matched)) if matched else None,
        "coverage": metrics.accuracy(len(matched), gold_count),
        "false_positive": judges.false_positive_rate(pairs),
        "n_matched": len(matched), "n_gold": gold_count,
    }


def review_material(material: str, timeout: int = 120) -> dict:
    """把材料写成临时 txt 上传 /review。"""
    fd, path = tempfile.mkstemp(prefix="eval_material_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(material)
        with open(path, "rb") as fh:
            resp = SESSION.post(f"{BASE}/review",
                                files={"file": ("material.txt", fh, "text/plain")},
                                timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    finally:
        if os.path.exists(path):
            os.remove(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all", choices=["dev", "test", "all"])
    args = ap.parse_args()

    all_cases = load_review_cases()
    cases = all_cases if args.split == "all" else filter_split(all_cases, args.split)

    print(f"审查评测 | 目标：{BASE} | 指纹：{case_set_fingerprint(cases)} | 用例：{len(cases)}（split={args.split}）")

    import requests
    try:
        requests.get(f"{BASE}/", timeout=5)
        login_session()
    except Exception as e:
        print(f"无法连接或登录服务：{e}\n请先启动服务，并用 TEST_USERNAME/TEST_PASSWORD 配置评测账号")
        return

    rows, all_pairs = [], []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case.id} | gold={case.gold_domain}/{case.gold_scene}")
        try:
            resp = review_material(case.material)
        except Exception as e:
            resp = {"error": f"{type(e).__name__}: {e}"}

        if "error" in resp:
            rows.append({"id": case.id, "split": case.split, "error": resp["error"],
                         "n_gold": len(case.gold_verdicts)})
            print(f"    ERROR: {resp['error']}")
            time.sleep(1)
            continue

        hard = (resp.get("review_result") or {}).get("硬规则校验") or []
        cmp = compare_verdicts(hard, case.gold_verdicts)
        all_pairs.extend([(m["pred"], m["gold"]) for m in cmp["matched"]])
        rows.append({"id": case.id, "split": case.split,
                     "domain": resp["review_result"].get("材料类型"),
                     "score": resp["review_result"].get("总评分"),
                     "grade": resp["review_result"].get("等级"), **cmp})
        print(f"    准确率={cmp['accuracy']} 假阳性={cmp['false_positive']} "
              f"缺规则={len(cmp['missing'])}")
        time.sleep(1)

    errs = [r for r in rows if "error" in r]
    ok = [r for r in rows if "error" not in r]
    matched_total = sum(r["n_matched"] for r in ok)
    gold_total = sum(r.get("n_gold", 0) for r in rows)
    correct_total = sum(sum(1 for m in r["matched"] if m["correct"]) for r in ok)

    artifact = {
        "manifest": {"module": "review", "case_set_fingerprint": case_set_fingerprint(cases),
                     "split": args.split, "case_count": len(rows), "base": BASE,
                     "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        "summary": {
            "cases_ok": len(ok), "cases_error": len(errs),
            "verdict_accuracy": metrics.accuracy(correct_total, gold_total),
            "matched_verdict_accuracy": metrics.accuracy(correct_total, matched_total),
            "rule_coverage": metrics.accuracy(matched_total, gold_total),
            "verdict_pairs": matched_total, "gold_rules": gold_total,
            "false_positive": judges.false_positive_rate(all_pairs),
            "errors": [{"id": r["id"], "error": r["error"]} for r in errs],
        },
        "results": rows,
    }
    OUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物已写入：{OUT}")
    print(f"用例：成功 {len(ok)} / 报错 {len(errs)}")
    print(f"端到端判定准确率：{artifact['summary']['verdict_accuracy']}（gold={gold_total}）")
    print(f"规则覆盖率：{artifact['summary']['rule_coverage']}（匹配={matched_total}）")
    print(f"假阳性率：{artifact['summary']['false_positive']}")


if __name__ == "__main__":
    main()
