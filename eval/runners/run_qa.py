"""问答评测执行器。

用法：
    1. 先启动服务（默认 8001，可用 TEST_BASE 覆盖）
    2. python eval/runners/run_qa.py [--split dev|test|all]

与旧 test_all.py 的区别：
    · 判定改用 eval.judges（关键事实覆盖 / 拒答混淆 / 引用准确），不再只看关键词；
    · 指标改用 eval.metrics，输出「分类型通过率 + 95% 置信区间」，不给虚高的单一总分；
    · 支持 dev/test 切分 —— 调参只用 dev，test 集冻结；
    · 产物带题集指纹，杜绝「用旧数据冒充新结论」。

本文件只负责「打接口 + 汇总」，判定逻辑全在 eval/judges.py、eval/metrics.py，
那两者是纯逻辑、可单测。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import judges, metrics                       # noqa: E402
from eval.schema import (case_set_fingerprint, filter_split,  # noqa: E402
                         load_qa_cases, qa_case_set_version)

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001").rstrip("/")
OUT = ROOT / "eval_results_qa.json"
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


def ask(question: str, timeout: int = 60):
    """调用 /chat/stream，返回 (answer, sources)。"""
    resp = SESSION.post(f"{BASE}/chat/stream",
                        params={"user_input": question, "history": ""},
                        stream=True, timeout=timeout)
    resp.raise_for_status()
    raw = ""
    for chunk in resp.iter_content(chunk_size=1024):
        if chunk:
            raw += chunk.decode("utf-8", errors="ignore")

    sources, answer = [], raw
    if "__SOURCES__" in raw and "__END__" in raw:
        s = raw.find("__SOURCES__") + len("__SOURCES__")
        e = raw.find("__END__", s)
        try:
            sources = json.loads(raw[s:e])
        except Exception:
            sources = []
        answer = raw[:raw.find("__SOURCES__")] + raw[e + len("__END__"):]
    return answer.strip(), sources


def judge_case(case, answer, sources):
    """对单题产出结构化判定结果。"""
    coverage = judges.key_fact_coverage(answer, case.key_facts)
    refusal = judges.judge_refusal(answer, case.expect_refusal)
    valid_indices = [s.get("index") for s in (sources or []) if isinstance(s, dict)]
    citations = judges.judge_citations(answer, valid_indices) if valid_indices else None

    if case.expect_refusal:
        passed = refusal == "correct"
    else:
        passed = coverage["verdict"] == "full"
    # 宽松口径：非拒答题只要「实质作答」（full 或 partial）就算过。
    # 【为什么要并列输出】严格口径要求 key_facts 逐字全覆盖，会把
    # 「答对了但换了措辞」的正确回答判为失败（实测 137元/科 因缺字面词「费用」被判错）。
    # 两个数字并排展示，把「判定口径对结论的影响」摆在台面上，而不是只报一个好看的数字。
    if case.expect_refusal:
        soft_passed = refusal == "correct"
    else:
        soft_passed = coverage["verdict"] in ("full", "partial")
    return {
        "id": case.id, "type": case.type, "split": case.split,
        "question": case.question, "answer": answer[:500],
        "coverage": coverage, "refusal": refusal, "citations": citations,
        "passed": passed, "soft_passed": soft_passed,
    }


def aggregate(rows) -> dict:
    """分类型通过率 + 置信区间；拒答混淆；引用准确率；严格/宽松双口径。"""
    by_type = {}
    for t, items in metrics.group_by(rows, lambda r: r["type"]).items():
        k = sum(1 for r in items if r["passed"])
        by_type[t] = {**metrics.rate_with_ci(k, len(items)),
                      "soft": metrics.rate_with_ci(sum(1 for r in items if r["soft_passed"]), len(items))["rate"],
                      "partial": sum(1 for r in items if r["coverage"]["verdict"] == "partial")}

    k = sum(1 for r in rows if r["passed"])
    k_soft = sum(1 for r in rows if r["soft_passed"])
    refusal_cm = metrics.confusion_summary(
        [(r["refusal"], "correct") for r in rows if r["refusal"] != "bad_answer"],
        labels=["correct", "wrong_refusal", "missed_refusal"],
    )
    cite = [r["citations"]["precision"] for r in rows if r["citations"] and r["citations"]["n"]]
    return {
        "overall": metrics.rate_with_ci(k, len(rows)),
        "overall_soft": metrics.rate_with_ci(k_soft, len(rows)),
        "by_type": by_type,
        "refusal": {t: sum(1 for r in rows if r["refusal"] == t)
                    for t in ("correct", "wrong_refusal", "missed_refusal", "bad_answer")},
        "citation_precision": round(sum(cite) / len(cite), 4) if cite else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all", choices=["dev", "test", "all"])
    args = ap.parse_args()

    all_cases = load_qa_cases()
    cases = all_cases if args.split == "all" else filter_split(all_cases, args.split)

    print(f"问答评测 | 目标：{BASE} | 题集：{qa_case_set_version()} | "
          f"指纹：{case_set_fingerprint(cases)} | 用例：{len(cases)}（split={args.split}）")

    import requests
    try:
        requests.get(f"{BASE}/", timeout=5)
        login_session()
    except Exception as e:
        print(f"无法连接或登录服务：{e}\n请先启动服务，并用 TEST_USERNAME/TEST_PASSWORD 配置评测账号")
        return

    rows = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case.question}")
        try:
            answer, sources = ask(case.question)
        except Exception as e:
            answer, sources = f"[ERROR] {e}", []
        row = judge_case(case, answer, sources)
        rows.append(row)
        flag = "PASS" if row["passed"] else "FAIL"
        print(f"    {flag} | 覆盖 {row['coverage']['hits']}/{row['coverage']['total']} | 拒答={row['refusal']}")
        time.sleep(1)

    summary = aggregate(rows)
    artifact = {
        "manifest": {
            "module": "qa", "case_set_version": qa_case_set_version(),
            "case_set_fingerprint": case_set_fingerprint(cases), "split": args.split,
            "question_count": len(rows), "base": BASE,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "summary": summary,
        "results": rows,
    }
    OUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物已写入：{OUT}")
    print(f"总体通过率：{summary['overall']['rate']} "
          f"(95%CI {summary['overall']['ci95'][0]}~{summary['overall']['ci95'][1]})")
    for t, s in sorted(summary["by_type"].items(), key=lambda x: x[1]["rate"] or 0):
        print(f"  {t}: {s['rate']} ({s['k']}/{s['n']})")


if __name__ == "__main__":
    main()
