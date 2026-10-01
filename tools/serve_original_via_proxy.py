"""把「桌面原始版」原封不动地接到计量代理后面启动。

背景：
    原始版（C:\\Users\\21003\\Desktop\\ai-intern）是改造的对照基线，
    按约定**一行都不改**。但它把 base_url 硬编码在 main.py 里
    （`AsyncOpenAI(base_url="https://ark.cn-beijing.volces.com/api/v3")`），
    直接在外部设环境变量是无效的 —— 于是它永远绕开计量代理，
    对照脚本就测不到它的调用次数。

做法：
    不碰原版任何文件。在本进程里 import 它的 main 模块，然后把模块命名空间里
    那些 AsyncOpenAI 实例整体替换成指向代理的新实例（原版所有函数引用的是
    模块全局 `client`，替换全局即生效）。之后再交给 uvicorn 托管。

    这样既守住了「基线不动」，又让两版的出站请求都落在同一个计量点上。

用法：
    python tools/serve_original_via_proxy.py
    # 可用环境变量覆盖：ORIGINAL_DIR / ORIGINAL_PORT / PROXY_BASE / ARK_API_KEY
"""

import importlib
import os
import sys

import uvicorn
from openai import AsyncOpenAI

ORIGINAL_DIR = os.getenv("ORIGINAL_DIR", r"C:\Users\21003\Desktop\ai-intern")
ORIGINAL_PORT = int(os.getenv("ORIGINAL_PORT", "8002"))
PROXY_BASE = os.getenv("PROXY_BASE", "http://127.0.0.1:8010/old/api/v3")


def main():
    if not os.path.isdir(ORIGINAL_DIR):
        raise SystemExit(f"找不到原始版目录：{ORIGINAL_DIR}（可用 ORIGINAL_DIR 指定）")

    os.chdir(ORIGINAL_DIR)          # 原版用的是相对路径（./data、./chroma_db）
    sys.path.insert(0, ORIGINAL_DIR)

    print(f"导入原始版：{ORIGINAL_DIR}")
    module = importlib.import_module("main")

    patched = 0
    for name in dir(module):
        obj = getattr(module, name, None)
        if isinstance(obj, AsyncOpenAI):
            # 保留原版的 api_key 与 max_retries：对照要的是「它原本的行为」，
            # 只把出站地址换到代理，不顺手改它的重试策略。
            setattr(module, name, AsyncOpenAI(
                api_key=obj.api_key or os.getenv("ARK_API_KEY"),
                base_url=PROXY_BASE,
                max_retries=obj.max_retries,
            ))
            patched += 1

    if not patched:
        raise SystemExit("未能在原版模块里找到 AsyncOpenAI 客户端，接管失败")

    print(f"已接管 {patched} 个模型客户端 → {PROXY_BASE}")
    print(f"原始版启动于 http://127.0.0.1:{ORIGINAL_PORT}")
    uvicorn.run(module.app, host="127.0.0.1", port=ORIGINAL_PORT, log_level="warning")


if __name__ == "__main__":
    main()
