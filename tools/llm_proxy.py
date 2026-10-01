"""LLM 出站请求反向代理 —— 用于「改造前后调用次数」的客观计量。

为什么需要它：
    README/改动说明的核心卖点是「单次问答的模型调用次数 3.33 → 2.00」。
    但原来的 compare_before_after.py 是从改造版新增的 /stats 接口读次数的 ——
    而桌面原版根本没有 /stats。于是原版那 3.33 次在脚本里是一句**写死的文字**，
    不是测出来的：所谓「可复现」，复现不出来。

    这里换一个两边都适用的计量点：让两个版本的所有模型请求都经过同一个中间层，
    由中间层按 /new、/old 两个前缀分别计数。口径统一，物理可验证。

怎么用（详见 tools/run_compare.sh）：
    1. 起代理：      python tools/llm_proxy.py            # 默认 127.0.0.1:8010
    2. 起改造版：    ARK_BASE_URL=http://127.0.0.1:8010/new/api/v3  uvicorn main:app --port 8001
    3. 起原始版：    python tools/serve_original_via_proxy.py      # 8002，无需改原版一行代码
    4. 跑对照：      python compare_before_after.py

接口：
    POST /new/api/v3/chat/completions   → 转发到上游，计入 new
    POST /old/api/v3/chat/completions   → 转发到上游，计入 old
    GET  /__stats                        → {"new": {...}, "old": {...}}
    POST /__reset                        → 清零
"""

import os
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# 上游真实地址（火山引擎方舟）。若要换成别的 OpenAI 兼容服务，改这个环境变量即可。
UPSTREAM = os.getenv("PROXY_UPSTREAM", "https://ark.cn-beijing.volces.com").rstrip("/")
HOST = os.getenv("PROXY_HOST", "127.0.0.1")
PORT = int(os.getenv("PROXY_PORT", "8010"))

# 两个被计量的版本。前缀必须出现在 base_url 的第一段。
LABELS = ("new", "old")

# 转发时要剥掉的逐跳头（保留 Authorization —— 那是鉴权必需的）
HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive",
    "transfer-encoding", "upgrade", "proxy-connection", "accept-encoding",
}

app = FastAPI(title="LLM 出站代理（计量用）")

_lock = threading.Lock()
_stats = {label: {"requests": 0, "by_path": {}, "first_ts": None, "last_ts": None}
          for label in LABELS}

_client = httpx.AsyncClient(timeout=None, trust_env=False)


def _bump(label: str, path: str):
    now = time.time()
    with _lock:
        bucket = _stats[label]
        bucket["requests"] += 1
        bucket["by_path"][path] = bucket["by_path"].get(path, 0) + 1
        if bucket["first_ts"] is None:
            bucket["first_ts"] = now
        bucket["last_ts"] = now


def _snapshot():
    with _lock:
        return {k: {**v, "by_path": dict(v["by_path"])} for k, v in _stats.items()}


@app.get("/__stats")
async def stats():
    """给对照脚本读的计数快照。"""
    return _snapshot()


@app.post("/__reset")
async def reset():
    with _lock:
        for label in LABELS:
            _stats[label] = {"requests": 0, "by_path": {}, "first_ts": None, "last_ts": None}
    return {"ok": True}


@app.api_route("/{label}/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def proxy(label: str, path: str, request: Request):
    if label not in LABELS:
        return JSONResponse({"error": f"未知计量前缀：{label}，只接受 {LABELS}"}, status_code=404)

    _bump(label, path)

    url = f"{UPSTREAM}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    body = await request.body()

    upstream_req = _client.build_request(request.method, url, headers=headers, content=body)
    upstream_resp = await _client.send(upstream_req, stream=True)

    # 只回传必要的响应头，避免把上游的连接管理头带回来
    passthrough = {}
    for key in ("content-type", "x-request-id", "openai-processing-ms"):
        if key in upstream_resp.headers:
            passthrough[key] = upstream_resp.headers[key]

    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=passthrough,
        background=BackgroundTask(upstream_resp.aclose),
    )


if __name__ == "__main__":
    print(f"LLM 计量代理启动： http://{HOST}:{PORT}")
    print(f"  改造版请设 ARK_BASE_URL=http://{HOST}:{PORT}/new/api/v3")
    print(f"  原始版由 tools/serve_original_via_proxy.py 接到 /old/api/v3")
    print(f"  上游： {UPSTREAM}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
