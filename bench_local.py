"""本地检索耗时基准（不调用大模型，纯本地）。

目的：把「检索+精排」这一段单独测准。
端到端耗时里混着远程大模型的波动（同一个问题生成耗时 2.2s～6.0s 都出现过），
那部分不是改造带来的，也不可复现。本地这一段才是硬指标。

对比两种配置：
    原始：向量 k=5、BM25 k=5，顺序拼接取前 20
    改造：向量 k=20、BM25 k=20，两路交替取前 10
"""

import time

import config
import main

QUESTIONS = [
    "2026年下半年全国大学英语四、六级考试什么时候报名？",
    "计算机等级考试报名费是多少？",
    "四级报名费怎么交？",
    "学费怎么交？",
    "推优需要多少票才能通过？",
    "何华儒是哪个班的？担任什么职位？",
]


def bench(label, vec_k, bm25_k, pool, rounds=3):
    main.vector_retriever.search_kwargs["k"] = vec_k
    # 空知识库时 main.bm25_retriever 为 None（见 main.py 的启动保护）
    if main.bm25_retriever is not None:
        main.bm25_retriever.k = bm25_k
    else:
        print("⚠️ 知识库为空，跳过 BM25 参数调整")
    config.RERANK_POOL = pool

    main.hybrid_search(QUESTIONS[0])          # 预热

    times, counts = [], []
    for _ in range(rounds):
        for q in QUESTIONS:
            t0 = time.perf_counter()
            docs = main.hybrid_search(q)
            times.append(time.perf_counter() - t0)
            counts.append(len(docs))

    avg = sum(times) / len(times)
    print(f"{label:<34} 平均 {avg:5.3f}s   最快 {min(times):5.3f}s   最慢 {max(times):5.3f}s   "
          f"平均返回 {sum(counts)/len(counts):.1f} 条")
    return avg


print("\n" + "=" * 94)
print("本地检索+精排耗时基准（每配置 6 题 × 3 轮 = 18 次）")
print("=" * 94)
old = bench("原始：k=5/5  顺序拼接池≤20", 5, 5, 20)
new = bench("改造：k=20/20  交替池≤10", 20, 20, 10)
print("-" * 94)
print(f"本地检索一段的变化：{new - old:+.3f}s（{(new/old - 1) * 100:+.0f}%）")
