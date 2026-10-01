"""批量把本地文档上传到知识库。

示例：
    python tools/batch_upload.py data
    python tools/batch_upload.py data --recursive
    python tools/batch_upload.py a.pdf b.docx --base-url http://127.0.0.1:8001

脚本默认串行上传。服务端在每次入库后会重建检索索引，规则类文档还可能触发
规则抽取；串行执行可以避免这些写操作互相干扰。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import requests


SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".docx", ".doc", ".png", ".jpg", ".jpeg")
DEFAULT_BASE_URL = "http://127.0.0.1:8001"


def parse_extensions(raw: str) -> tuple[str, ...]:
    """把逗号分隔的扩展名统一为小写且带点的形式。"""
    values = []
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        values.append(item if item.startswith(".") else f".{item}")
    if not values:
        raise argparse.ArgumentTypeError("扩展名列表不能为空")
    return tuple(dict.fromkeys(values))


def discover_files(
    inputs: Iterable[str],
    recursive: bool,
    extensions: tuple[str, ...],
) -> tuple[list[Path], list[str]]:
    """展开文件和目录，返回去重后的文件列表及无法使用的输入。"""
    found: list[Path] = []
    problems: list[str] = []
    seen: set[str] = set()

    for raw in inputs:
        path = Path(raw).expanduser()
        if not path.exists():
            problems.append(f"路径不存在：{path}")
            continue

        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            pattern = "**/*" if recursive else "*"
            candidates = (item for item in path.glob(pattern) if item.is_file())
        else:
            problems.append(f"不是普通文件或目录：{path}")
            continue

        matched = 0
        for candidate in candidates:
            if candidate.suffix.lower() not in extensions:
                continue
            matched += 1
            resolved = candidate.resolve()
            key = str(resolved).casefold()
            if key not in seen:
                seen.add(key)
                found.append(resolved)

        if path.is_file() and matched == 0:
            problems.append(f"不支持的文件类型：{path}")

    found.sort(key=lambda item: str(item).casefold())
    return found, problems


def upload_one(
    session: requests.Session,
    endpoint: str,
    path: Path,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> dict:
    """上传单个文件；只对网络错误、限流和服务端错误重试。"""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    last_error = "未知错误"

    for attempt in range(retries + 1):
        try:
            with path.open("rb") as stream:
                response = session.post(
                    endpoint,
                    files={"file": (path.name, stream, mime)},
                    timeout=timeout,
                )

            retryable = response.status_code in (408, 429) or response.status_code >= 500
            if retryable and attempt < retries:
                last_error = f"HTTP {response.status_code}"
                time.sleep(retry_delay * (attempt + 1))
                continue

            try:
                body = response.json()
            except ValueError:
                body = {"error": response.text.strip() or f"HTTP {response.status_code}"}

            if not response.ok:
                return {
                    "status": "failed",
                    "message": str(body.get("error") or body.get("detail") or f"HTTP {response.status_code}"),
                    "http_status": response.status_code,
                    "attempts": attempt + 1,
                }

            if body.get("error"):
                return {
                    "status": "failed",
                    "message": str(body["error"]),
                    "http_status": response.status_code,
                    "attempts": attempt + 1,
                }

            message = str(body.get("message") or "上传完成")
            status = "skipped" if "已存在" in message or "无需重复" in message else "success"
            return {
                "status": status,
                "message": message,
                "http_status": response.status_code,
                "attempts": attempt + 1,
            }

        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
                continue

    return {
        "status": "failed",
        "message": last_error,
        "http_status": None,
        "attempts": retries + 1,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将多个文件或目录中的文档批量上传到工大智政知识库。",
    )
    parser.add_argument("paths", nargs="+", help="文件或目录，可同时传多个")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"服务地址（默认：{DEFAULT_BASE_URL}）",
    )
    parser.add_argument("--timeout", type=float, default=180, help="单个文件超时秒数（默认：180）")
    parser.add_argument("--retries", type=int, default=2, help="网络或服务端错误重试次数（默认：2）")
    parser.add_argument("--retry-delay", type=float, default=2, help="首次重试等待秒数（默认：2）")
    parser.add_argument("--max-mb", type=float, default=20, help="本地预检的单文件上限 MB（默认：20）")
    parser.add_argument(
        "--extensions",
        type=parse_extensions,
        default=SUPPORTED_EXTENSIONS,
        help="允许的扩展名，逗号分隔",
    )
    parser.add_argument("--report", type=Path, help="将详细结果写入 JSON 文件")
    parser.add_argument("--dry-run", action="store_true", help="只列出将上传的文件，不发送请求")
    parser.add_argument("--stop-on-error", action="store_true", help="遇到第一个失败立即停止")
    parser.add_argument("--username", default=os.getenv("UPLOAD_USERNAME", "reviewer"),
                        help="负责人账号（默认读取 UPLOAD_USERNAME，未设置则 reviewer）")
    parser.add_argument("--password", default=os.getenv("UPLOAD_PASSWORD", "Demo@123456"),
                        help="账号密码（默认读取 UPLOAD_PASSWORD）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0 or args.retries < 0 or args.retry_delay < 0 or args.max_mb <= 0:
        print("参数错误：timeout、retry-delay、max-mb 必须为正数，retries 不能为负数。", file=sys.stderr)
        return 2

    files, input_problems = discover_files(args.paths, args.recursive, args.extensions)
    for problem in input_problems:
        print(f"警告：{problem}", file=sys.stderr)

    if not files:
        print("没有找到可上传的文件。", file=sys.stderr)
        return 1

    endpoint = f"{args.base_url.rstrip('/')}/upload"
    print(f"找到 {len(files)} 个文件，目标：{endpoint}")

    if args.dry_run:
        for index, path in enumerate(files, 1):
            print(f"[{index}/{len(files)}] {path}")
        print("预演完成，未发送任何文件。")
        return 0

    results = []
    counts = {"success": 0, "skipped": 0, "failed": 0}
    max_bytes = int(args.max_mb * 1024 * 1024)

    with requests.Session() as session:
        try:
            login_response = session.post(
                f"{args.base_url.rstrip('/')}/auth/login",
                json={"username": args.username, "password": args.password},
                timeout=15,
            )
            if not login_response.ok:
                detail = login_response.json().get("detail", login_response.text)
                print(f"登录失败：{detail}", file=sys.stderr)
                return 1
        except requests.RequestException as exc:
            print(f"登录失败：{exc}", file=sys.stderr)
            return 1
        for index, path in enumerate(files, 1):
            size = path.stat().st_size
            if size > max_bytes:
                result = {
                    "status": "failed",
                    "message": f"文件超过本地限制 {args.max_mb:g}MB",
                    "http_status": None,
                    "attempts": 0,
                }
            elif size == 0:
                result = {
                    "status": "failed",
                    "message": "文件内容为空",
                    "http_status": None,
                    "attempts": 0,
                }
            else:
                print(f"[{index}/{len(files)}] 正在上传：{path.name}")
                started = time.perf_counter()
                result = upload_one(
                    session,
                    endpoint,
                    path,
                    args.timeout,
                    args.retries,
                    args.retry_delay,
                )
                result["elapsed_seconds"] = round(time.perf_counter() - started, 3)

            result.update({"file": str(path), "size_bytes": size})
            results.append(result)
            counts[result["status"]] += 1

            labels = {"success": "成功", "skipped": "跳过", "failed": "失败"}
            print(f"    {labels[result['status']]}：{result['message']}")
            if result["status"] == "failed" and args.stop_on_error:
                break

    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "summary": {"total": len(results), **counts},
        "input_warnings": input_problems,
        "results": results,
    }

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"详细报告：{args.report.resolve()}")

    print(
        f"完成：成功 {counts['success']}，重复跳过 {counts['skipped']}，失败 {counts['failed']}。"
    )
    return 1 if counts["failed"] or input_problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
