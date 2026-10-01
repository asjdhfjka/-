#!/usr/bin/env bash
# ============================================================
# 一键起「计量代理 + 改造版 + 原始版」，跑完对照后收工。
#
# 用法（在项目根目录、Git Bash 里执行）：
#     bash tools/run_compare.sh
#
# 可覆盖的环境变量：
#     VENV         复用哪个虚拟环境（默认桌面版 .venv，含全部依赖与模型缓存）
#     ORIGINAL_DIR 原始版目录（默认 C:\Users\21003\Desktop\ai-intern）
# ============================================================
set -uo pipefail

VENV="${VENV:-C:/Users/21003/Desktop/ai-intern/.venv/Scripts/python.exe}"
export ORIGINAL_DIR="${ORIGINAL_DIR:-C:\\Users\\21003\\Desktop\\ai-intern}"
PROXY_PORT="${PROXY_PORT:-8010}"
NEW_PORT="${NEW_PORT:-8001}"
OLD_PORT="${OLD_PORT:-8002}"

if [ ! -f "$VENV" ]; then
    echo "❌ 找不到虚拟环境：$VENV"
    echo "   可用 VENV=/path/to/python.exe bash tools/run_compare.sh 指定"
    exit 1
fi

PIDS=()
cleanup() {
    echo ""
    echo "正在关闭后台服务..."
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT

wait_http() {
    local url="$1" name="$2" tries="${3:-120}"
    for _ in $(seq 1 "$tries"); do
        if "$VENV" - "$url" <<'PY' 2>/dev/null
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=2).read(1)
PY
        then
            echo "  ✅ $name 就绪"
            return 0
        fi
        sleep 2
    done
    echo "  ❌ $name 在超时时间内未就绪：$url"
    return 1
}

echo "1/4 启动计量代理（:${PROXY_PORT}）"
"$VENV" tools/llm_proxy.py >.run_proxy.log 2>&1 &
PIDS+=($!)
wait_http "http://127.0.0.1:${PROXY_PORT}/__stats" "计量代理" 20 || exit 1

echo "2/4 启动改造版（:${NEW_PORT}，出站走代理 /new）"
ARK_BASE_URL="http://127.0.0.1:${PROXY_PORT}/new/api/v3" \
    "$VENV" -m uvicorn main:app --host 127.0.0.1 --port "${NEW_PORT}" >.run_new.log 2>&1 &
PIDS+=($!)

echo "3/4 启动原始版（:${OLD_PORT}，出站走代理 /old，原版文件不改动）"
"$VENV" tools/serve_original_via_proxy.py >.run_old.log 2>&1 &
PIDS+=($!)

echo "  首次启动需加载嵌入与精排模型，约 1～2 分钟，请稍候..."
wait_http "http://127.0.0.1:${NEW_PORT}/" "改造版" 150 || exit 1
wait_http "http://127.0.0.1:${OLD_PORT}/" "原始版" 150 || exit 1

echo "4/4 运行对照测试"
"$VENV" compare_before_after.py

echo ""
echo "日志：.run_proxy.log / .run_new.log / .run_old.log"
