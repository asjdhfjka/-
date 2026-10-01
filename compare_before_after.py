"""改造前后对照测试（走计量代理，两版调用次数都是**实测**）。

与旧版的区别（这是本次最大的一处修正）：
    旧版从改造版新增的 /stats 接口读调用次数，而原始版没有这个接口 ——
    于是「原始版 3.33 次」在脚本里是一句写死的字符串，根本测不出来。
    现在两版的模型请求都经过 tools/llm_proxy.py，由代理按 /new、/old 分别计数，
    两边口径完全一致，且原始版一行代码都不用改。

前置（三个服务都要起着）：
    1) python tools/llm_proxy.py                        # 8010
    2) ARK_BASE_URL=http://127.0.0.1:8010/new/api/v3 uvicorn main:app --port 8001
    3) python tools/serve_original_via_proxy.py         # 8002

用法：
    python compare_before_after.py
"""

import json
import os
import sys
import time

import requests

PROXY = "http://127.0.0.1:8010"
NEW = "http://127.0.0.1:8001"
OLD = "http://127.0.0.1:8002"
NEW_USERNAME = os.getenv("TEST_USERNAME", "student")
NEW_PASSWORD = os.getenv("TEST_PASSWORD", "Demo@123456")

QUESTIONS = [
    "2026年下半年全国大学英语四、六级考试什么时候报名？",
    "计算机等级考试报名费是多少？",
    "四级报名费怎么交？",
    "学费怎么交？",
    "推优需要多少票才能通过？",
    "何华儒是哪个班的？担任什么职位？",
]


def ask(base, question, history="", session=None):
    """调一次流式问答，返回答案、来源、耗时。"""
    t0 = time.time()
    http = session or requests
    resp = http.post(f"{base}/chat/stream",
                     params={"user_input": question, "history": history},
                     stream=True, timeout=180)
    resp.raise_for_status()
    sources, chunks = [], []
    for line in resp.iter_lines(decode_unicode=True):
        if line is None:
            continue
        if line.startswith("__SOURCES__"):
            payload = line[len("__SOURCES__"):-len("__END__")]
            try:
                sources = json.loads(payload)
            except Exception:
                sources = []
        else:
            chunks.append(line)
    return {"answer": "\n".join(chunks).strip(),
            "sources": sources,
            "sec": round(time.time() - t0, 2)}


def proxy_calls():
    """从代理读两版累计请求数。"""
    data = requests.get(f"{PROXY}/__stats", timeout=10).json()
    return {label: data[label]["requests"] for label in ("new", "old")}


def main():
    # ---- 前置检查：三个服务都得在线 ----
    try:
        requests.get(f"{PROXY}/__stats", timeout=5)
    except Exception as e:
        sys.exit(f"❌ 计量代理未启动（{PROXY}）：{e}\n   请先运行：python tools/llm_proxy.py")
    for name, base in (("改造版", NEW), ("原始版", OLD)):
        try:
            requests.get(f"{base}/", timeout=5)
        except Exception as e:
            sys.exit(f"❌ {name}未启动（{base}）：{e}")

    requests.post(f"{PROXY}/__reset", timeout=5)

    new_session = requests.Session()
    try:
        login = new_session.post(
            f"{NEW}/auth/login",
            json={"username": NEW_USERNAME, "password": NEW_PASSWORD},
            timeout=10,
        )
        login.raise_for_status()
    except Exception as e:
        sys.exit(
            f"❌ 改造版登录失败：{e}\n"
            "   可用 TEST_USERNAME / TEST_PASSWORD 指定测试账号"
        )

    print("=" * 92)
    print("改造前后对照：同一批问题，两版各跑一遍；调用次数由计量代理实测")
    print("=" * 92)

    rows = []
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n[{i}/{len(QUESTIONS)}] {q}")

        before = proxy_calls()
        new = ask(NEW, q, session=new_session)
        mid = proxy_calls()
        old = ask(OLD, q)
        after = proxy_calls()

        new_calls = mid["new"] - before["new"]
        old_calls = after["old"] - before["old"]

        rows.append({"q": q, "new": new, "old": old,
                     "new_calls": new_calls, "old_calls": old_calls})

        print(f"  改造版: {new['sec']:>6.2f}s  模型调用 {new_calls} 次")
        print(f"  原始版: {old['sec']:>6.2f}s  模型调用 {old_calls} 次")
        print(f"  --- 改造版答案 ---\n{new['answer'][:300]}")
        print(f"  --- 原始版答案 ---\n{old['answer'][:300]}")

    new_avg = sum(r["new_calls"] for r in rows) / len(rows)
    old_avg = sum(r["old_calls"] for r in rows) / len(rows)
    drop = (1 - new_avg / old_avg) * 100 if old_avg else 0

    print("\n" + "=" * 92)
    print("调用次数（实测，非估算）")
    print(f"  原始版：{', '.join(str(r['old_calls']) for r in rows)}  → 平均 {old_avg:.2f}")
    print(f"  改造版：{', '.join(str(r['new_calls']) for r in rows)}  → 平均 {new_avg:.2f}")
    print(f"  降幅：{drop:.1f}%")
    print("=" * 92)

    with open("compare_result.json", "w", encoding="utf-8") as f:
        json.dump({"questions": rows,
                   "summary": {"new_avg_calls": round(new_avg, 2),
                               "old_avg_calls": round(old_avg, 2),
                               "drop_pct": round(drop, 1)}},
                  f, ensure_ascii=False, indent=2)
    print("\n已写出 compare_result.json")


if __name__ == "__main__":
    main()
