import json
import zipfile
import hashlib
from langchain_core.documents import Document
from langchain_community.document_loaders import Docx2txtLoader
import os
import fitz  # PyMuPDF
from rapidocr_onnxruntime import RapidOCR
import asyncio
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
import httpx

import config
import prompts
from auth_store import AuthStore
from llm_utils import (
    build_fact_json_schema,
    parse_json_object,
    strip_fence,
    validate_fact_extraction,
    validate_soft_review,
)
from review_engine import (
    build_source_refs,
    compute_score,
    domain_alignment_report,
    domain_catalog,
    execute_hard_rules,
    formula_fields,
    normalize_import_domain,
    route_rules,
)
from rule_workflow import (
    clean_rule,
    create_draft_batch,
    load_draft_store,
    precheck_rule,
    public_batch,
    repair_conflicting_rule_ids,
    refresh_batch_status,
    save_draft_store,
    utc_now,
)
from fastapi import Depends, FastAPI, HTTPException, Request
from openai import AsyncOpenAI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from fastapi import UploadFile, File
import shutil
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from datetime import datetime
from fastapi.responses import FileResponse
ocr_engine = RapidOCR()


# 允许上传/审查的文件类型（/upload 与 /review 共用同一份口径，
# 避免出现「前端能选、后端解析不了」的错配）
ALLOWED_EXT = (".pdf", ".txt", ".docx", ".doc", ".png", ".jpg", ".jpeg")
IMAGE_EXT = (".png", ".jpg", ".jpeg")
MAX_UPLOAD_MB = 20


def convert_doc_to_docx(doc_path: str) -> str:
    """用 LibreOffice 把 .doc 转成 .docx（绕开中文路径问题）。

    【改造点】原来所有调用共用一个固定的 temp_convert 目录，且在 finally 里
    整个 rmtree —— 两个请求同时上传 .doc 时，先结束的那个会把另一个正在用的
    目录删掉。现在每次调用用独立临时目录，只清理自己那一份。
    """
    import subprocess

    soffice = os.getenv("SOFFICE_PATH", r"C:\Program Files\LibreOffice\program\soffice.exe")
    if not os.path.exists(soffice):
        print(f"   ❌ 未找到 LibreOffice：{soffice}（可用 SOFFICE_PATH 指定）")
        return None

    # 独立临时目录：短英文名正本 + 输出，绕开中文路径
    temp_dir = tempfile.mkdtemp(prefix="docconv_")
    short_doc_path = os.path.join(temp_dir, f"input_{uuid.uuid4().hex[:8]}.doc")
    docx_path = os.path.splitext(doc_path)[0] + ".docx"

    try:
        shutil.copy2(doc_path, short_doc_path)
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "docx",
             "--outdir", temp_dir, short_doc_path],
            capture_output=True, text=True, timeout=180
        )
        print(f"   返回码: {result.returncode}")

        for candidate in os.listdir(temp_dir):
            if candidate.endswith(".docx"):
                shutil.copy2(os.path.join(temp_dir, candidate), docx_path)
                print("   ✅ 转换成功")
                return docx_path
        print("   ❌ 转换失败：文件未生成")
    except subprocess.TimeoutExpired:
        print("   ❌ 超时 180 秒")
    except Exception as e:
        print(f"   ❌ 异常: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return None

def smart_docx_loader(docx_path: str, filename: str):
    """Word 图文混排智能解析：解压 zip 提取图片并 OCR"""
    try:
        loader = Docx2txtLoader(docx_path)
        text_docs = loader.load()
        base_text = text_docs[0].page_content if text_docs else ""
    except Exception as e:
        print(f"⚠️ Word 文本提取失败: {e}")
        base_text = ""

    image_texts = []
    try:
        with zipfile.ZipFile(docx_path, 'r') as z:
            for name in z.namelist():
                if name.startswith('word/media/') and name.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif')):
                    img_bytes = z.read(name)
                    # 【改造点】原来用 `./temp_docx_{原名}` 固定名，并发上传同名
                    # 图片时会互相覆盖、互相删除。改用临时文件，用完即弃。
                    suffix = os.path.splitext(name)[1] or ".png"
                    fd, temp_img = tempfile.mkstemp(prefix="docx_img_", suffix=suffix)
                    try:
                        with os.fdopen(fd, "wb") as f:
                            f.write(img_bytes)
                        ocr_text = extract_text_from_image(temp_img)
                        if ocr_text.strip():
                            image_texts.append(ocr_text)
                    finally:
                        if os.path.exists(temp_img):
                            os.remove(temp_img)
    except Exception as e:
        print(f"⚠️ Word 图片提取失败: {e}")

    combined_text = base_text
    if image_texts:
        combined_text += "\n\n【Word内嵌图片识别结果】：\n" + "\n".join(image_texts)
        print(f"   ✅ Word 内嵌图片 OCR 完成，识别出 {len(image_texts)} 张图片内容")

    if combined_text.strip():
        return [Document(page_content=combined_text, metadata={"source": filename, "page": 1})]
    return []

def extract_text_from_image(image_path: str) -> str:
    """用 OCR 识别图片里的文字"""
    try:
        result, _ = ocr_engine(image_path)
        if result:
            # result 格式是 [[[坐标], "文字", 置信度], ...]
            text_lines = [line[1] for line in result]
            return "\n".join(text_lines)
        return ""
    except Exception as e:
        print(f"❌ OCR 识别失败: {e}")
        return ""


async def extract_rules_from_content(doc_content: str, doc_title: str, domain: str) -> list:
    """从规章制度文本中抽取结构化规则"""
    prompt = prompts.extract_rules_prompt(doc_content, doc_title, domain)

    try:
        result = await review_json_call(
            prompt, "规则抽取", temperature=config.TEMPERATURE_JUDGE,
            model=config.REVIEW_MODEL, max_tokens=config.REVIEW_MAX_TOKENS)
        rules = result.get("rules")
        if not isinstance(rules, list):
            raise ValueError("规则抽取结果缺少 rules 数组")
        return [rule for rule in rules if isinstance(rule, dict)]
    except Exception as e:
        print(f"⚠️ 规则抽取失败: {e}")
        return []


def ocr_pdf_if_empty(pdf_path: str, documents) -> list:
    """如果是扫描版 PDF（提取不出字），则自动转图片并 OCR"""
    total_chars = sum([len(doc.page_content) for doc in documents])
    if total_chars > 50:  # 已经有正常文字，不是扫描件
        return documents

    print("⚠️ 检测到扫描版 PDF，启动 OCR 解析...")
    ocr_docs = []
    try:
        pdf_doc = fitz.open(pdf_path)
        for page_num in range(len(pdf_doc)):
            page = pdf_doc[page_num]
            pix = page.get_pixmap(dpi=150)
            # 【改造点】原来用 `./temp_ocr_page_{页号}.png` 固定名，两个请求
            # 同时解析扫描件时会互相覆盖。改用临时文件。
            fd, temp_img_path = tempfile.mkstemp(prefix="ocr_page_", suffix=".png")
            os.close(fd)
            try:
                pix.save(temp_img_path)
                page_text = extract_text_from_image(temp_img_path)
                if page_text.strip():
                    ocr_docs.append(Document(
                        page_content=page_text,
                        metadata={"source": pdf_path, "page": page_num + 1}
                    ))
            finally:
                if os.path.exists(temp_img_path):
                    os.remove(temp_img_path)

        print(f"✅ 扫描版 PDF OCR 完成，共解析 {len(ocr_docs)} 页")
        return ocr_docs
    except Exception as e:
        print(f"❌ PDF OCR 失败: {e}")
        return documents


def load_documents(file_location: str, filename: str) -> list:
    """按扩展名把磁盘上的文件解析成 Document 列表。

    【改造点】/upload 和 /review 以前各写一套解析分支，前端允许 .doc/图片，
    而 /review 只处理 pdf/docx/其它（一律按 UTF-8 文本读），于是上传 .doc 得到
    二进制乱码、上传图片直接失败。现在两条链路共用这一份，
    「支持的格式」这件事只有一个定义处。
    """
    lower = filename.lower()

    if lower.endswith(IMAGE_EXT):
        print("📷 检测到图片，启动 OCR 识别...")
        ocr_text = extract_text_from_image(file_location)
        if not ocr_text.strip():
            raise ValueError("图片中未识别到文字")
        return [Document(page_content=ocr_text, metadata={"source": filename})]

    if lower.endswith(".pdf"):
        documents = PyPDFLoader(file_location).load()
        return ocr_pdf_if_empty(file_location, documents)

    if lower.endswith(".txt"):
        return TextLoader(file_location, encoding="utf-8").load()

    if lower.endswith(".docx"):
        return smart_docx_loader(file_location, filename)

    if lower.endswith(".doc"):
        print("📝 检测到旧版 .doc，正在转换为 .docx...")
        converted = convert_doc_to_docx(file_location)
        if not converted:
            raise ValueError("旧版 .doc 转换失败，请另存为 .docx 后重试")
        try:
            return smart_docx_loader(converted, filename)
        finally:
            if os.path.exists(converted):
                os.remove(converted)

    raise ValueError(f"不支持的文件类型：{filename}")


async def save_upload(upload: UploadFile, max_mb: int):
    """校验类型与体积后落盘，返回 (安全路径, 文件字节)。

    【修复】本函数此前只返回路径字符串，而 /upload 与 /review 都按
    `路径, 内容 = await save_upload(...)` 解包 —— 于是两个端点每次调用
    都抛 `too many values to unpack (expected 2)`，上传与审查整体不可用。
    集成测试默认跳过，所以一直没暴露。现在返回二元组，与两处调用对齐。

    【改造点】原来 /review 直接把 `file.filename` 拼进路径
    （`./temp_{file.filename}`），既没有体积上限，也没有路径净化 ——
    文件名里带 `../` 就能写到目录外，几百 MB 的文件就能把内存吃满。
    /upload 用的是原始文件名落 data/，同样有覆盖与穿越问题。
    现在统一：先校验后缀，再按大小限制读入，最后用 uuid 生成磁盘名。
    """
    if not upload.filename or not upload.filename.lower().endswith(ALLOWED_EXT):
        raise ValueError(f"仅支持 {'、'.join(ALLOWED_EXT)} 文件")

    content = await upload.read()
    limit = max_mb * 1024 * 1024
    if len(content) > limit:
        raise ValueError(f"文件过大，请上传 {max_mb}MB 以内的文件")
    if not content:
        raise ValueError("文件内容为空")

    ext = os.path.splitext(upload.filename)[1].lower()
    fd, path = tempfile.mkstemp(prefix="upload_", suffix=ext)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path, content
# 说明：.env 的加载已挪到 config.py 的最前面（在任何 os.getenv 之前），
# 这里不再 load_dotenv —— 否则会出现「先读配置、后加载 .env」的经典时序坑。
app = FastAPI()#就是拿 FastAPI 框架，生产出一个属于你自己的 Web 应用实体（名叫 app）。等这个应用跑起来，
# FastAPI会自动送我一个交互式网页（/docs），方便和别人测试整个系统的接口

AUTH_STORE = AuthStore(config.AUTH_DB_PATH)
if config.AUTH_BOOTSTRAP_DEMO_USERS:
    created_users = AUTH_STORE.bootstrap_demo_users(config.AUTH_DEMO_PASSWORD)
    if created_users:
        print("✅ 已初始化演示用户：student / reviewer / admin")


def _disabled_auth_user() -> dict:
    return {
        "id": "auth-disabled", "username": "system", "display_name": "系统用户",
        "college": "", "role": "admin", "is_active": True, "created_at": "",
    }


def require_user(request: Request) -> dict:
    if not config.AUTH_ENABLED:
        return _disabled_auth_user()
    token = request.cookies.get(config.AUTH_COOKIE_NAME, "")
    user = AUTH_STORE.user_for_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_roles(*roles):
    allowed = set(roles)

    def dependency(user: dict = Depends(require_user)) -> dict:
        if user.get("role") not in allowed:
            raise HTTPException(status_code=403, detail="当前账号没有此操作权限")
        return user

    return dependency


def _record_chat_safe(user: dict, question: str, answer: str, sources: list):
    if not config.AUTH_ENABLED:
        return
    try:
        AUTH_STORE.record_chat(user["id"], question, answer, sources)
    except Exception as history_exc:
        print(f"⚠️ 对话记录保存失败: {history_exc}")


@app.post("/auth/login")
async def auth_login(payload: dict):
    if not config.AUTH_ENABLED:
        return {"ok": True, "user": _disabled_auth_user()}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    user = AUTH_STORE.authenticate(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = AUTH_STORE.create_session(user["id"], config.AUTH_SESSION_DAYS)
    response = JSONResponse({"ok": True, "user": user})
    response.set_cookie(
        config.AUTH_COOKIE_NAME,
        token,
        max_age=config.AUTH_SESSION_DAYS * 86400,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    if config.AUTH_ENABLED:
        AUTH_STORE.delete_session(request.cookies.get(config.AUTH_COOKIE_NAME, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(config.AUTH_COOKIE_NAME, path="/")
    return response


@app.get("/auth/me")
async def auth_me(user: dict = Depends(require_user)):
    return {"user": user}


@app.get("/auth/users")
async def auth_users(_: dict = Depends(require_roles("admin"))):
    return {"users": AUTH_STORE.list_users()}


@app.post("/auth/users")
async def auth_create_user(payload: dict, _: dict = Depends(require_roles("admin"))):
    try:
        user = AUTH_STORE.create_user(
            payload.get("username"), payload.get("password"), payload.get("display_name"),
            payload.get("role", "student"), payload.get("college", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "user": user}


@app.patch("/auth/users/{user_id}/active")
async def auth_set_user_active(user_id: str, payload: dict,
                               current: dict = Depends(require_roles("admin"))):
    if user_id == current.get("id") and not bool(payload.get("active")):
        raise HTTPException(status_code=400, detail="不能停用当前登录账号")
    user = AUTH_STORE.set_user_active(user_id, bool(payload.get("active")))
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "user": user}


@app.get("/me/chats")
async def my_chats(user: dict = Depends(require_user)):
    if not config.AUTH_ENABLED:
        return {"items": []}
    return {"items": AUTH_STORE.recent_chats(user["id"])}


@app.get("/me/reviews")
async def my_reviews(user: dict = Depends(require_user)):
    if not config.AUTH_ENABLED:
        return {"items": []}
    return {"items": AUTH_STORE.recent_reviews(user["id"])}


@app.post("/me/reviews/{review_id}/submit")
async def submit_my_review(review_id: str,
                           user: dict = Depends(require_roles("student"))):
    """把一次个人预审结果提交到审核员工作台。退回修改后可再次提交。"""
    try:
        submission = AUTH_STORE.submit_review(review_id, user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "submission": submission}


@app.get("/submissions")
async def list_submissions(user: dict = Depends(require_user)):
    student_id = user["id"] if user.get("role") == "student" else None
    return {"items": AUTH_STORE.list_submissions(student_id)}


@app.get("/submissions/{submission_id}")
async def get_submission(submission_id: str, user: dict = Depends(require_user)):
    submission = AUTH_STORE.get_submission(submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    if user.get("role") == "student" and submission.get("student_id") != user["id"]:
        raise HTTPException(status_code=403, detail="无权查看其他学生的材料")
    return {"submission": submission}


@app.patch("/submissions/{submission_id}")
async def review_submission(submission_id: str, payload: dict,
                            user: dict = Depends(require_roles("reviewer", "admin"))):
    try:
        submission = AUTH_STORE.update_submission(
            submission_id,
            payload.get("status"),
            user["id"],
            payload.get("note", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not submission:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    return {"ok": True, "submission": submission}


@app.get("/me/materials/{review_id}/file")
async def preview_user_material(review_id: str, user: dict = Depends(require_user)):
    owner_id = user["id"] if user.get("role") == "student" else None
    review = AUTH_STORE.get_review(review_id, owner_id)
    if not review:
        raise HTTPException(status_code=404, detail="材料不存在或无权访问")
    if user.get("role") == "reviewer" and not review.get("submission_id"):
        raise HTTPException(status_code=403, detail="该材料尚未正式提交")
    path = os.path.abspath(str(review.get("stored_path") or ""))
    root = os.path.abspath(config.MATERIALS_DIR)
    if not path or not path.startswith(root + os.sep) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="材料原文件未归档")
    return FileResponse(path, filename=review.get("filename") or os.path.basename(path))

# 2. 初始化火山引擎客户端
# 【改造点】原代码用 `os.environ["NO_PROXY"] = "..."` 绕开代理，发生在 import 阶段，
# 会静默影响同一进程里所有其他 HTTP 客户端。现在收进这个客户端自己的连接层，作用域不外泄。
_http_client = httpx.AsyncClient(trust_env=not config.ARK_BYPASS_PROXY)
client = AsyncOpenAI(
    api_key=config.ARK_API_KEY,
    base_url=config.ARK_BASE_URL,
    http_client=_http_client,
    max_retries=config.ARK_MAX_RETRIES,
)


class LLMStats:
    """大模型调用计数器。

    存在的意义是可验证：改造前后「一次问答到底调了几次模型」这件事，
    以前只能靠读日志数，现在随时可以查。答辩时这组数字就是证据。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._calls = 0
        self._by_purpose = {}

    def bump(self, purpose: str):
        with self._lock:
            self._calls += 1
            self._by_purpose[purpose] = self._by_purpose.get(purpose, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"calls": self._calls, "by_purpose": dict(self._by_purpose)}

    def reset(self):
        with self._lock:
            self._calls = 0
            self._by_purpose.clear()


LLM_STATS = LLMStats()


class QueryCache:
    """查询级 LRU 缓存。

    校园场景里问题高度重复（「四级什么时候报名」一天能被问几十遍），
    命中缓存时完全不走大模型，调用次数为 0。这是最省成本的一处优化。
    """

    def __init__(self, size: int):
        self.size = size
        self._data = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self._hits += 1
                return self._data[key]
            self._misses += 1
            return None

    def put(self, key: str, value):
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {"size": len(self._data), "hits": self._hits, "misses": self._misses,
                    "hit_rate": round(self._hits / total, 4) if total else 0.0}

    def clear(self):
        with self._lock:
            self._data.clear()
            self._hits = 0
            self._misses = 0


ANSWER_CACHE = QueryCache(config.CACHE_SIZE)


async def llm_chat(prompt: str, purpose: str, temperature: float = None,
                   timeout: float = None, model: str = None,
                   system: str = None, stream: bool = False,
                   max_tokens: int = None, structured: bool = False,
                   json_schema: dict = None, json_mode: bool = True):
    """所有大模型调用的唯一出口。

    统一了五件以前散落各处的事：模型接入点、超时、温度、消息拼装、调用计数。
    改造前模型接入点 ID 在文件里多处硬编码，换一个接入点要全文搜索。
    """
    LLM_STATS.bump(purpose)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    request = dict(
        model=model or config.ARK_MODEL,
        messages=messages,
        temperature=config.TEMPERATURE_JUDGE if temperature is None else temperature,
        timeout=timeout or config.TIMEOUT_AUX,
        stream=stream,
    )
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    if structured:
        if config.REVIEW_JSON_MODE and json_mode:
            request["response_format"] = ({
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_result",
                    "strict": True,
                    "schema": json_schema,
                },
            } if json_schema else {"type": "json_object"})
        if config.REVIEW_DISABLE_THINKING:
            # 方舟的 thinking 是兼容接口扩展字段，通过 OpenAI SDK 的
            # extra_body 透传。结构化抽取不需要长思维链，关闭后可避免
            # reasoning_content 吃光输出额度而让正式 content 为空。
            request["extra_body"] = {"thinking": {"type": "disabled"}}
    return await client.chat.completions.create(**request)


async def review_json_call(prompt: str, purpose: str, temperature: float = None,
                           model: str = None, max_tokens: int = None,
                           validator=None, json_schema: dict = None) -> dict:
    """调用审查模型并解析 JSON，失败时做有限次数的显式重试。"""
    last_error = None
    attempts = max(1, config.REVIEW_RETRIES + 1)
    use_structured_options = True
    use_json_mode = True
    active_json_schema = json_schema
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            response = await llm_chat(
                prompt,
                purpose,
                temperature=temperature,
                timeout=config.TIMEOUT_REVIEW,
                model=model or config.REVIEW_MODEL,
                max_tokens=max_tokens or config.REVIEW_MAX_TOKENS,
                structured=use_structured_options,
                json_schema=active_json_schema,
                json_mode=use_json_mode,
            )
            choice = response.choices[0]
            message = choice.message
            content = getattr(message, "content", None)
            reasoning = getattr(message, "reasoning_content", None)
            if not reasoning:
                reasoning = (getattr(message, "model_extra", None) or {}).get("reasoning_content")

            parse_errors = []
            result = None
            # 正常 content 优先；部分推理型兼容接口会把最终 JSON 留在
            # reasoning_content 中，因此 content 为空或不可解析时再尝试后者。
            for label, raw in (("content", content), ("reasoning_content", reasoning)):
                if raw is None or not str(raw).strip():
                    continue
                try:
                    result = parse_json_object(raw)
                    if label == "reasoning_content":
                        print(f"   ℹ️ {purpose}的 JSON 位于 reasoning_content，已兼容解析")
                    break
                except ValueError as parse_exc:
                    parse_errors.append(f"{label}: {parse_exc}")

            if result is None:
                finish_reason = getattr(choice, "finish_reason", None)
                content_len = len(str(content or ""))
                reasoning_len = len(str(reasoning or ""))
                detail = "；".join(parse_errors) or "两个返回字段均为空"
                raise ValueError(
                    f"{purpose}未返回可解析 JSON（finish_reason={finish_reason}, "
                    f"content={content_len}字, reasoning={reasoning_len}字；{detail}）")
            if validator is not None:
                result = validator(result)
            print(f"   ✅ {purpose}完成：{time.perf_counter() - started:.2f}s"
                  f"（输入 {len(prompt)} 字符，第 {attempt + 1}/{attempts} 次）")
            return result
        except Exception as exc:
            last_error = exc
            print(f"   ⚠️ {purpose}第 {attempt + 1}/{attempts} 次失败：{exc}")
            if attempt + 1 < attempts:
                # 旧接入点可能不支持 JSON mode 或 thinking 扩展字段；只在
                # 服务端明确报告参数不兼容时降级，普通解析失败仍保留结构化模式。
                error_text = str(exc).lower()
                if active_json_schema is not None and "json_schema" in error_text:
                    # 接入点只支持 json_object 时，第二次仍保留关闭思考，
                    # 仅把严格 Schema 降级成普通 JSON 对象。
                    active_json_schema = None
                    print(f"   ℹ️ {purpose}接入点不支持 JSON Schema，下一次改用 JSON 对象模式")
                elif use_json_mode and "response_format" in error_text:
                    use_json_mode = False
                    print(f"   ℹ️ {purpose}接入点不支持 JSON 输出参数，下一次改用提示词约束")
                elif use_structured_options and any(marker in error_text for marker in (
                        "thinking", "unknown parameter", "unsupported parameter",
                        "not support", "不支持")):
                    use_structured_options = False
                    print(f"   ℹ️ {purpose}接入点不支持结构化参数，下一次改用兼容模式")
                await asyncio.sleep(config.REVIEW_RETRY_DELAY)
    raise last_error


print(f"[config] {config.summary()}")
# 加载本地向量数据库
print("正在加载向量数据库...")
embeddings = HuggingFaceEmbeddings(model_name=config.EMBEDDING_MODEL)
#嵌入模型就是一个“翻译官”，这是北京智源研究院（BAAI）开源的一个专门针对中文优化的轻量级（small）向量模型。
# 它体积小、速度快，非常适合个人电脑或普通服务器跑本地知识库。
vector_db = Chroma(persist_directory=config.CHROMA_DIR, embedding_function=embeddings)
#向量数据库擅长语义模糊查找，告诉数据库把数据持久化保存在本地当前目录下的 chroma_db 文件夹里。这样下次重启程序，之前上传的知识库还在，不用重新导入。
# 告诉数据库，以后存文字或者查文字时，都用刚才加载的“翻译官”来转换。
# ========== 混合检索初始化（BM25 + 向量） ==========
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

print("正在构建 BM25 关键词索引...")
# 1. 从 Chroma 里把已入库的所有片段读出来，用于构建 BM25 内存索引
all_data = vector_db.get()
if all_data and all_data.get('documents'):
    all_docs = [
        Document(page_content=text, metadata=meta)
        for text, meta in zip(all_data['documents'], all_data['metadatas'])
    ]
else:
    all_docs = []
print(f"✅ BM25 索引准备就绪，共 {len(all_docs)} 个片段")

# 2. BM25 关键词检索器（擅长“GXzhcx”、“教务处”等专有名词）
bm25_retriever = BM25Retriever.from_documents(all_docs)
bm25_retriever.k = config.RETRIEVE_BM25_K

# 3. 向量语义检索器（擅长“怎么交学费”这类模糊语义）
vector_retriever = vector_db.as_retriever(search_kwargs={"k": config.RETRIEVE_VECTOR_K})

# 4. 手工实现混合检索（两路结果合并去重）
def hybrid_search(query: str, search_filter: dict = None):
    # ========== 阶段一：多路粗召回 ==========
    # 1. 向量检索
    if search_filter:
        vector_docs = vector_retriever.invoke(query, filter=search_filter)
    else:
        vector_docs = vector_retriever.invoke(query)

    # 2. BM25 检索
    bm25_docs = bm25_retriever.invoke(query)
    if search_filter:
        allowed = search_filter.get("topic")
        bm25_docs = [d for d in bm25_docs if d.metadata.get("topic") == allowed]

    # 3. 合并去重后截取精排池。
    # 【改造点】原来是 `vector_docs + bm25_docs` 顺序拼接——一路的结果排在前面，
    # 一旦召回条数调大，后一路会被整个挤出精排池。
    # 改成两路交替出队，保证关键词召回的结果同样有机会进精排。
    combined, seen = [], set()
    for i in range(max(len(vector_docs), len(bm25_docs))):
        for bucket in (vector_docs, bm25_docs):
            if i < len(bucket):
                doc = bucket[i]
                if doc.page_content not in seen:
                    seen.add(doc.page_content)
                    combined.append(doc)

    candidates = combined[:config.RERANK_POOL]

    if not candidates:
        return []

    # ========== 阶段二：Rerank 精排 ==========
    try:
        # 构造 (query, document) 对
        pairs = [(query, doc.page_content) for doc in candidates]

        # Rerank 打分
        scores = reranker.predict(pairs)

        # 按分数降序排列
        scored_docs = list(zip(candidates, scores))
        scored_docs.sort(key=lambda x: x[1], reverse=True)

        print(f"   🎯 Rerank 精排完成，最高分: {scored_docs[0][1]:.4f}，最低分: {scored_docs[-1][1]:.4f}")

        # ⭐ 动态阈值：取最高分的 RERANK_RATIO 作为下限，同时不低于 RERANK_MIN_SCORE
        # 【改造点】原注释写「不低于 0.5」，代码实际是 max(0.1, top*0.3)，两者对不上。
        # 现在阈值两项都进配置，注释与代码一致。
        top_score = scored_docs[0][1]
        dynamic_threshold = max(config.RERANK_MIN_SCORE, top_score * config.RERANK_RATIO)
        print(f"   📊 动态阈值: {dynamic_threshold:.2f}（最高分 {top_score:.2f} × {config.RERANK_RATIO}）")

        # 打印 Top 8 候选的分数分布（方便调试）
        for i, (doc, score) in enumerate(scored_docs[:8]):
            title = doc.metadata.get('source_title', doc.metadata.get('source_file', ''))[:30]
            print(f"      候选 {i + 1}: 分数 {score:.2f} | {title}")

        filtered = [(doc, score) for doc, score in scored_docs if score >= dynamic_threshold]

        if not filtered:
            filtered = scored_docs[:min(3, config.RERANK_TOP_N)]

        # ⭐ 父子分块核心：用父块内容替换子块，避免上下文断裂
        final_docs = []
        seen_parents = set()
        for doc, score in filtered[:config.RERANK_TOP_N]:
            parent_id = doc.metadata.get("parent_id")
            if parent_id and parent_id not in seen_parents:
                seen_parents.add(parent_id)
                # 用父块的完整内容替换子块的短内容
                new_doc = Document(
                    page_content=doc.metadata.get("parent_content", doc.page_content),
                    metadata=doc.metadata
                )
                final_docs.append(new_doc)
            elif not parent_id:
                # 兼容旧数据（没有父块信息的）
                final_docs.append(doc)

        return final_docs

    except Exception as e:
        print(f"⚠️ Rerank 失败，降级使用原始排序: {e}")
        return candidates[:config.RERANK_TOP_N]

print("✅ 混合检索器（BM25 + 向量）准备就绪！")
from sentence_transformers import CrossEncoder

print("正在加载 Rerank 精排模型（首次运行会下载约 1GB，请耐心等待）...")
# BGE-Reranker-v2-m3 对中文支持极好，适合高校行政场景
reranker = CrossEncoder(config.RERANKER_MODEL)
print("✅ Rerank 精排模型加载完成！")
#设置查询参数。意思是，每次用户提问，去数据库里找最相似的 6 个文本片段（k=6）。
print("向量数据库加载完成！")


# 规则路由（route_rules）与硬规则校验（execute_hard_rules）已迁到 review_engine.py，
# 在文件顶部导入。迁出的理由不是「为了好看」，而是为了可测：这两个函数是纯逻辑，
# 却因为长在 main.py 里（main.py 一 import 就加载向量库和两个模型）而一直
# 无法被单元测试覆盖 —— critical 严重度漏排序、enum 反向包含误判就是这么漏掉的。
@app.post("/chat")
#大管家（app），如果外面有人用 POST 方式，敲门牌是 /chat 的这扇门，你就去执行下面这个叫 chat 的函数。
async def chat(user_input: str, user: dict = Depends(require_user)):
    # 3. 调用大模型
    response = await llm_chat(user_input, "直答", temperature=config.TEMPERATURE_GENERATE)
    reply = response.choices[0].message.content
    _record_chat_safe(user, user_input, reply, [])
    return {"reply": reply}
#拆包：从大模型返回的一坨复杂对象里，精准提取出回答文字：
# response.choices：大模型可能一次生成多个备选答案（choices），是个列表。
# [0]：取第一个（也是最常用的那个）。
# .message：拿到第一条消息对象。
# .content：拿到消息里的具体文本内容（比如“你好，很高兴见到你”）。
# 打包：把提取出的文本，包装成 Python 字典 {"reply": "模型的回答"}。
# FastAPI 会自动把这个字典转换成 JSON 格式返回给前端，前端收到后就是：
#chat 函数是你整个后端代码里，第一个真正干活的“打工人”。它的任务非常明确：接收用户提问，去问大模型，然后把大模型的回答拿回来。
@app.post("/chat/stream")
# 和之前的 /chat 一样，这是告诉大管家（FastAPI），有人用 POST 敲门 /chat/stream，就执行这个函数
async def chat_stream(user_input: str, history: str = "",
                      user: dict = Depends(require_user)):
    async def event_generator():
        print(f"\n--- 收到请求: {user_input} ---")

        # 1. 查询重写（加回来，提升检索准确率）
        search_query = user_input
        if history:
            # 获取当前时间
            now_str = datetime.now().strftime("%Y年%m月")
            # 【改造点】提示词已迁至 prompts.py，这里只负责调用
            rewrite_prompt = prompts.rewrite_prompt(history, user_input, now_str)
            try:
                rewrite_res = await llm_chat(rewrite_prompt, "查询重写",
                                             temperature=config.TEMPERATURE_REWRITE,
                                             timeout=config.TIMEOUT_REWRITE)
                search_query = rewrite_res.choices[0].message.content.strip()
                if "改写结果：" in search_query:
                    search_query = search_query.split("改写结果：")[-1].strip()
                print(f"🔄 查询重写: {user_input} -> {search_query}")
            except Exception as e:
                print(f"⚠️ 查询重写失败，使用原问题: {e}")

        # ========== 查询级缓存 ==========
        # 【改造点】校园场景里问题高度重复（「四级什么时候报名」一天能被问几十遍），
        # 命中缓存时完全不调用大模型，调用次数为 0。
        cache_key = search_query if not history else f"{search_query}||{user_input}"
        if config.CACHE_ENABLED:
            cached = ANSWER_CACHE.get(cache_key)
            if cached:
                print(f"   ⚡ 命中查询缓存（{ANSWER_CACHE.stats()}），跳过全部模型调用")
                _record_chat_safe(user, user_input, cached["answer"], cached["sources"])
                yield f"__SOURCES__{json.dumps(cached['sources'], ensure_ascii=False)}__END__\n"
                yield cached["answer"]
                return

        print(f"⏳ 开始本地向量检索，检索词: {search_query}")
        _t0 = time.perf_counter()
        try:
            docs = await asyncio.to_thread(hybrid_search, search_query)
            print(f"✅ 检索完成，找到 {len(docs)} 个片段")
        except Exception as e:
            print(f"❌ 检索失败: {e}")
            docs = []
        _t1 = time.perf_counter()
        print(f"   ⏱ 检索+精排: {_t1 - _t0:.2f}s")

        # ========== CRAG 相关性过滤（批量版） ==========
        # 【改造点】原实现是「每条资料发一次请求」，5 条 = 5 次往返，其中 4 次是纯协议开销。
        # 现在 N 条合并成 1 次请求，判断标准一字未改，失败仍默认保留。
        candidates_to_judge = docs[:config.CRAG_TOP_N]

        if candidates_to_judge:
            print(f"⏳ CRAG 相关性过滤（批量：{len(candidates_to_judge)} 条合 1 次请求）...")
            batch_prompt = prompts.crag_batch_prompt(
                search_query, [d.page_content for d in candidates_to_judge])
            try:
                judge_res = await llm_chat(batch_prompt, "相关性判定",
                                           temperature=config.TEMPERATURE_JUDGE,
                                           timeout=config.TIMEOUT_JUDGE)
                verdicts = prompts.parse_crag_batch(
                    judge_res.choices[0].message.content, len(candidates_to_judge))
            except Exception as e:
                print(f"   ⚠️ 批量判定失败，全部保留: {e}")
                verdicts = [True] * len(candidates_to_judge)
            relevant_docs = [doc for doc, ok in zip(candidates_to_judge, verdicts) if ok]
            filtered_count = len(candidates_to_judge) - len(relevant_docs)
            print(f"   ✅ 批量过滤完成：保留 {len(relevant_docs)} 条，过滤掉 {filtered_count} 条")
        else:
            relevant_docs = []
        _t2 = time.perf_counter()
        print(f"   ⏱ 相关性判定: {_t2 - _t1:.2f}s")
        # ⭐ CRAG 后：用过滤后的 relevant_docs 重新生成 sources
        sources = []
        seen_urls = set()
        for doc in relevant_docs:
            url = doc.metadata.get("source_url", "")
            title = doc.metadata.get("source_title", "")
            file_name = doc.metadata.get("source_file", "")
            if not url and file_name:
                url = f"/preview/{file_name}"
                title = title or file_name
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append({"index": len(sources) + 1, "title": title, "url": url})

        sources = sources[:5]
        url_to_index = {s["url"]: s["index"] for s in sources}
        # ========== 如果过滤后为空，走拒答逻辑 ==========
        if not relevant_docs:
            fallback_sources = []
            for doc in docs[:3]:
                url = doc.metadata.get("source_url", "")
                title = doc.metadata.get("source_title", "")
                file_name = doc.metadata.get("source_file", "")
                if not url and file_name:
                    url = f"/preview/{file_name}"
                    title = title or file_name
                if url:
                    fallback_sources.append({
                        "index": len(fallback_sources) + 1,
                        "title": title,
                        "url": url
                    })

            fallback_answer = "根据现有资料，我无法准确回答这个问题。您可以参考下方我为您检索到的相关材料。"
            _record_chat_safe(user, user_input, fallback_answer, fallback_sources)
            yield f"__SOURCES__{json.dumps(fallback_sources, ensure_ascii=False)}__END__\n"
            yield fallback_answer
            return

        # ⭐ 用过滤后的 relevant_docs 重新拼装 context
        context_parts = []
        for doc in relevant_docs:
            doc_url = doc.metadata.get("source_url", "")
            if not doc_url and doc.metadata.get("source_file"):
                doc_url = f"/preview/{doc.metadata.get('source_file')}"
            if doc_url in url_to_index:
                idx = url_to_index[doc_url]
                context_parts.append(f"【来源{idx}】\n{doc.page_content}")
            else:
                context_parts.append(doc.page_content)

        context = "\n\n".join(context_parts)
        print(f"   📦 最终 context 长度: {len(context)} 字")

        yield f"__SOURCES__{json.dumps(sources, ensure_ascii=False)}__END__\n"

        print("⏳ 开始调用大模型生成回答...")
        # 【改造点】作答提示词改由 prompts.py 渲染：
        # 通用修辞留在代码里，「只允许引用哪些文档」这类校规从 domains/*.yaml 注入。
        # 改造前第 767-770 行把《全国大学英语四、六级考试报名》等文档名写死在代码里，换学校必须改代码。
        current_date = datetime.now().strftime("%Y年%m月%d日")
        system_prompt = prompts.answer_system_prompt(context, current_date)

        collected = []
        try:
            response = await llm_chat(user_input, "生成回答", system=system_prompt,
                                      temperature=config.TEMPERATURE_GENERATE,
                                      timeout=config.TIMEOUT_GENERATE, stream=True)
            async for chunk in response:
                # 有的服务端会补一个 choices 为空的收尾块，这里挡一下，避免 IndexError
                if not getattr(chunk, "choices", None):
                    continue
                piece = chunk.choices[0].delta.content
                if piece:
                    collected.append(piece)
                    yield piece
            print("✅ 回答完毕！")
            _t3 = time.perf_counter()
            print(f"   ⏱ 生成: {_t3 - _t2:.2f}s | 合计: {_t3 - _t0:.2f}s")
            if config.CACHE_ENABLED and collected:
                ANSWER_CACHE.put(cache_key, {"sources": sources, "answer": "".join(collected)})
            if collected:
                _record_chat_safe(user, user_input, "".join(collected), sources)
        except Exception as e:
            print(f"❌ 大模型调用失败: {e}")
            yield "抱歉，网络请求超时或服务暂时不可用。"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/stats")
async def stats(_: dict = Depends(require_roles("reviewer", "admin"))):
    """可观测性接口：模型调用次数与缓存命中率。

    改造前「一次问答到底调了几次模型」只能靠读日志逐行数；
    现在随时可查，答辩演示时可以直接把这个数字投出来。
    """
    try:
        with RULES_LOCK:
            rules_config = _load_rules_config()
        alignment = domain_alignment_report(prompts.load_domain(), rules_config)
    except Exception as exc:
        alignment = {"error": str(exc)}
    return {
        "llm": LLM_STATS.snapshot(),
        "cache": ANSWER_CACHE.stats(),
        "config": config.summary(),
        "domain_alignment": alignment,
    }


@app.post("/stats/reset")
async def stats_reset(_: dict = Depends(require_roles("admin"))):
    LLM_STATS.reset()
    ANSWER_CACHE.clear()
    return {"ok": True}


# 告诉 FastAPI，不要等水接满一桶再给前端，而是水龙头滴出一滴水，你就立刻通过网络把水传给前端。
# 挂载静态文件夹，让 / 路径直接显示 index.html

# ============================================================
# 规则库读写（加锁）
# 草稿审核与正式发布都会修改 JSON，/review 同时读取正式规则库。
# 统一加锁，避免并发审核、发布或审查时读到半份数据。
# ============================================================
RULES_LOCK = threading.Lock()


def _load_rules_config() -> dict:
    """读取规则库；调用方在并发读写场景中负责持有 RULES_LOCK。"""
    if os.path.exists(config.RULES_FILE):
        with open(config.RULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"version": "2.0", "domains": {}, "global_rules": []}
    if not isinstance(data.get("domains"), dict):
        data["domains"] = {}
    data.setdefault("global_rules", [])
    return data


def _save_rules_config(data: dict) -> None:
    """原子写入正式规则库，避免进程中断留下半份 JSON。"""
    target = config.BASE_DIR / config.RULES_FILE
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)


def _draft_source_pages(chunks: list) -> list[dict]:
    """保存可供原文核验的去重父块，避免把所有重叠子块写进草稿库。"""
    pages = []
    seen = set()
    total_chars = 0
    for chunk in chunks:
        metadata = chunk.metadata or {}
        identity = metadata.get("parent_id") or hashlib.md5(
            chunk.page_content.encode("utf-8")).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        text = str(metadata.get("parent_content") or chunk.page_content).strip()
        if not text:
            continue
        remaining = 30000 - total_chars
        if remaining <= 0:
            break
        text = text[:remaining]
        pages.append({"page": metadata.get("page"), "text": text})
        total_chars += len(text)
    return pages


def _find_draft_batch(store: dict, batch_id: str) -> dict:
    for batch in store.get("batches") or []:
        if batch.get("batch_id") == batch_id:
            return batch
    raise HTTPException(status_code=404, detail="规则草稿批次不存在")


def _official_rules_for_domain(rules_config: dict, domain: str) -> list:
    return list(((rules_config.get("domains") or {}).get(domain) or {}).get("rules") or [])


def split_parent_child(documents, source_file: str) -> list:
    """父子分块（Small-to-Big）：父块给模型看完整上下文，子块用于精准检索。

    块大小与重叠全部读 config —— 原实现把它们硬编码在函数体里，
    config 里同名的那几项形同虚设，注释还写着「子块：400字」而代码是 800，
    注释与代码互相打脸。
    """
    parent_splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.PARENT_CHUNK_SIZE,
        chunk_overlap=config.PARENT_CHUNK_OVERLAP,
        length_function=len,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
    )
    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHILD_CHUNK_SIZE,
        chunk_overlap=config.CHILD_CHUNK_OVERLAP,
        length_function=len,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
    )

    chunks = []
    for doc in documents:
        for p_idx, parent_chunk in enumerate(parent_splitter.split_documents([doc])):
            content_hash = hashlib.md5(parent_chunk.page_content.encode("utf-8")).hexdigest()[:8]
            parent_id = f"{source_file}__p{p_idx}__{content_hash}"
            for child_chunk in child_splitter.split_documents([parent_chunk]):
                child_chunk.metadata["parent_id"] = parent_id
                child_chunk.metadata["parent_content"] = parent_chunk.page_content
                chunks.append(child_chunk)
    return chunks


def rebuild_bm25_index():
    """从 Chroma 全量重建 BM25 索引。

    已知边界：全量重建期间并发问答可能读到中间状态（新旧替换不是原子的）。
    当前语料在百级片段，影响可忽略；上万片段时应改增量索引或加读写锁。
    """
    global bm25_retriever
    try:
        all_data_new = vector_db.get()
        if all_data_new and all_data_new.get("documents"):
            all_docs_new = [
                Document(page_content=text, metadata=meta)
                for text, meta in zip(all_data_new["documents"], all_data_new["metadatas"])
            ]
            retriever = BM25Retriever.from_documents(all_docs_new)
            retriever.k = config.RETRIEVE_BM25_K
            bm25_retriever = retriever
            print(f"✅ BM25 索引已更新：共 {len(all_docs_new)} 个片段")
    except Exception as e:
        print(f"⚠️ BM25 更新失败: {e}")


async def maybe_extract_rules(filename: str, chunks: list) -> dict:
    """识别规则文档并生成待人工确认的草稿，绝不直接写正式规则库。"""
    print("🔍 正在判断文档是否包含审查规则...")
    judge_content = "\n".join([c.page_content for c in chunks])[:3000]
    judge_prompt = prompts.is_rule_doc_prompt(filename, judge_content)
    try:
        judge_res = await llm_chat(judge_prompt, "规则文档判定",
                                   temperature=config.TEMPERATURE_JUDGE, timeout=10.0)
        is_rule_doc = "YES" in judge_res.choices[0].message.content.strip().upper()
    except Exception as e:
        print(f"   ⚠️ 语义判断失败，降级为关键词匹配: {e}")
        rule_keywords = ["办法", "规定", "细则", "条例", "章程", "制度", "准则", "标准", "规范", "说明", "要求"]
        is_rule_doc = any(kw in filename for kw in rule_keywords)

    if not is_rule_doc:
        return {"message": "", "draft_batch_id": None}

    print("📖 检测到规则类文档，正在抽取规则...")
    # 统一目录 = domains/*.yaml 的预设领域 + auto_rules.json 的动态领域。
    # 新领域的草稿发布后，下一次识别就能直接复用，不会成为规则库里的孤岛。
    with RULES_LOCK:
        rules_snapshot = _load_rules_config()
    domain_cfg = prompts.load_domain()
    known_domains = domain_catalog(domain_cfg, rules_snapshot)
    domain_prompt = prompts.domain_identify_prompt(
        filename, chunks[0].page_content[:800], known_domains)
    domain_res = await llm_chat(domain_prompt, "领域识别", temperature=config.TEMPERATURE_JUDGE)
    raw_domain = domain_res.choices[0].message.content.strip()
    matched_domain = normalize_import_domain(raw_domain, domain_cfg, rules_snapshot)
    if matched_domain != raw_domain:
        print(f"   🔁 导入领域归一：【{raw_domain}】→【{matched_domain}】")
    else:
        print(f"   ✅ 识别到领域：【{matched_domain}】")

    doc_content = "\n".join([c.page_content for c in chunks])
    new_rules = await extract_rules_from_content(doc_content, filename, matched_domain)
    if not new_rules:
        return {"message": "，但未抽取到可用规则", "draft_batch_id": None}
    if not isinstance(new_rules, list):
        return {"message": "，但规则抽取结果格式异常", "draft_batch_id": None}

    source_file = (chunks[0].metadata or {}).get("source_file") or filename
    source_pages = _draft_source_pages(chunks)
    with RULES_LOCK:
        rules_config = _load_rules_config()
        final_domain = normalize_import_domain(
            matched_domain, domain_cfg, rules_config) or "通用审查"
        existing_rules = _official_rules_for_domain(rules_config, final_domain)
        batch = create_draft_batch(
            final_domain, new_rules, filename, source_file, source_pages, existing_rules)
        draft_store = load_draft_store(config.RULE_DRAFTS_FILE)
        draft_store["batches"].insert(0, batch)
        draft_store["audit"].append({
            "time": utc_now(), "action": "extract", "batch_id": batch["batch_id"],
            "source_doc": filename, "domain": final_domain, "count": len(batch["rules"]),
        })
        save_draft_store(config.RULE_DRAFTS_FILE, draft_store)

    blockers = sum(
        1 for item in batch["rules"]
        if any(issue.get("blocking") for issue in item.get("precheck") or []))
    print(f"✅ 已生成规则草稿：{len(batch['rules'])} 条【{final_domain}】，"
          f"其中 {blockers} 条需先修正，等待负责人确认")
    return {
        "message": f"，已生成 {len(batch['rules'])} 条【{final_domain}】规则草稿，等待负责人确认",
        "draft_batch_id": batch["batch_id"],
    }


@app.post("/upload")
async def upload_file(file: UploadFile = File(...),
                      _: dict = Depends(require_roles("reviewer", "admin"))):
    """上传文档入库；规章类文档只生成规则草稿，不直接发布。"""
    # 类型 + 体积校验，落盘为 uuid 命名的临时文件（天然免疫路径穿越）
    try:
        file_location, file_content = await save_upload(file, MAX_UPLOAD_MB)
    except ValueError as e:
        return {"error": f"❌ {e}"}

    file_hash = hashlib.md5(file_content).hexdigest()
    original_name = os.path.basename(file.filename)
    stem, ext = os.path.splitext(original_name)
    stored_name = f"{stem}__{file_hash[:8]}{ext.lower()}"

    try:
        existing_docs = vector_db.get(where={"file_hash": file_hash})
        if existing_docs and existing_docs.get("ids"):
            # 文件可能在草稿功能上线前就已入库，或上一次规则抽取失败。
            # 已有草稿直接返回；没有草稿时复用知识库片段重新生成，不重复写向量库。
            with RULES_LOCK:
                draft_store = load_draft_store(config.RULE_DRAFTS_FILE)
                existing_batch = next((
                    batch for batch in draft_store.get("batches") or []
                    if batch.get("source_file") == stored_name
                ), None)
            if existing_batch:
                return {
                    "message": "⚠️ 该文件内容已存在，已为你打开已有规则草稿。",
                    "rule_draft_batch_id": existing_batch["batch_id"],
                }

            stored_chunks = [
                Document(page_content=text, metadata=metadata or {})
                for text, metadata in zip(
                    existing_docs.get("documents") or [],
                    existing_docs.get("metadatas") or [],
                )
                if text
            ]
            if stored_chunks:
                draft_result = await maybe_extract_rules(original_name, stored_chunks)
                response = {"message": "⚠️ 该文件内容已存在，无需重复入库"
                                       + draft_result.get("message", "")}
                if draft_result.get("draft_batch_id"):
                    response["rule_draft_batch_id"] = draft_result["draft_batch_id"]
                return response
            return {"message": "⚠️ 该文件内容已存在，无需重复上传！"}

        # 先记录同名旧版本的片段 ID。新版本成功写入后再删除，避免更新失败时知识库被清空。
        old_ids = set()
        for where in ({"original_filename": original_name}, {"source_file": original_name}):
            try:
                old = vector_db.get(where=where)
                old_ids.update((old or {}).get("ids") or [])
            except Exception as e:
                print(f"⚠️ 查询旧版本失败，将保留原片段：{e}")

        # 永久留档使用内容哈希作为版本号；同名更新不会覆盖旧原文。
        os.makedirs("./data", exist_ok=True)
        shutil.copy2(file_location, os.path.join("./data", stored_name))

        documents = load_documents(file_location, file.filename)
        for doc in documents:
            doc.metadata["file_hash"] = file_hash
            doc.metadata["original_filename"] = original_name
            doc.metadata["source_file"] = stored_name
            doc.metadata["source_title"] = original_name
            doc.metadata["uploaded_at"] = datetime.now().isoformat(timespec="seconds")
            # 把文件名拼到正文开头，防止标题污染
            if file.filename and not doc.page_content.startswith(f"【{file.filename}】"):
                doc.page_content = f"【{file.filename}】\n{doc.page_content}"

        # ========== 父子分块（Small-to-Big） ==========
        chunks = split_parent_child(documents, stored_name)
        # 过滤极短片段：阈值来自 config.MIN_CHUNK_CHARS ——
        # 原实现硬编码 150，而 config 里那个同名配置项从未被任何代码引用。
        chunks = [c for c in chunks if len(c.page_content) > config.MIN_CHUNK_CHARS]
        print(f"   🧹 过滤短片段后：{len(chunks)} 条")

        for chunk in chunks:
            src = chunk.metadata.get("source_title", chunk.metadata.get("source_file", "未知文件"))
            chunk.page_content = f"【文档来源】：{src}\n{chunk.page_content}"

        # 入库
        if not chunks:
            return {"message": "文件内容为空，未添加片段"}

        vector_db.add_documents(chunks)
        if old_ids:
            vector_db.delete(ids=list(old_ids))
        rebuild_bm25_index()
        draft_result = await maybe_extract_rules(file.filename, chunks)
        # 文档内容或规则发生变化后，旧答案的依据已经失效。
        ANSWER_CACHE.clear()
        version_msg = f"，已停用同名旧版本 {len(old_ids)} 个片段" if old_ids else ""
        response = {"message": (f"✅ 上传成功！已添加 {len(chunks)} 个文本片段到知识库"
                                f"（版本 {file_hash[:8]}）{version_msg}"
                                f"{draft_result.get('message', '')}")}
        if draft_result.get("draft_batch_id"):
            response["rule_draft_batch_id"] = draft_result["draft_batch_id"]
        return response

    except ValueError as e:
        return {"error": f"❌ {e}"}
    except Exception as e:
        print(f"❌ 解析文件失败: {e}")
        return {"error": "文件解析失败，可能格式不正确或编码有问题"}
    finally:
        # 临时文件用完即删（data/ 里已留档一份）
        if os.path.exists(file_location):
            os.remove(file_location)


@app.get("/rule-drafts")
async def list_rule_drafts(_: dict = Depends(require_roles("reviewer", "admin"))):
    """列出规则草稿批次；制度正文快照不会返回给浏览器。"""
    with RULES_LOCK:
        store = load_draft_store(config.RULE_DRAFTS_FILE)
        batches = [public_batch(batch) for batch in store.get("batches", [])[:50]]
    return {
        "batches": batches,
        "summary": {
            "pending": sum(batch.get("status") == "pending_review" for batch in batches),
            "ready": sum(batch.get("status") == "ready_to_publish" for batch in batches),
            "published": sum(batch.get("status") == "published" for batch in batches),
        },
    }


@app.patch("/rule-drafts/{batch_id}/rules/{draft_id}")
async def decide_rule_draft(batch_id: str, draft_id: str, payload: dict,
                            current: dict = Depends(require_roles("reviewer", "admin"))):
    """负责人逐条通过、编辑后通过或驳回候选规则。"""
    action = str(payload.get("action") or "").lower()
    if action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="action 只能是 approve 或 reject")
    reviewer = current.get("display_name") or current.get("username") or "负责人"
    comment = str(payload.get("comment") or "").strip()[:500]

    with RULES_LOCK:
        store = load_draft_store(config.RULE_DRAFTS_FILE)
        batch = _find_draft_batch(store, batch_id)
        if batch.get("published_at"):
            raise HTTPException(status_code=409, detail="该批次已经发布，不能再次修改")
        item = next(
            (entry for entry in batch.get("rules") or [] if entry.get("draft_id") == draft_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="规则草稿不存在")

        now = utc_now()
        if action == "reject":
            item["status"] = "rejected"
            item["decision"] = {"reviewer": reviewer, "comment": comment, "time": now}
            ok = True
        else:
            edited = payload.get("rule") if isinstance(payload.get("rule"), dict) else item.get("rule")
            edited = clean_rule(edited or {})
            edited.setdefault("id", (item.get("rule") or {}).get("id", ""))

            rules_config = _load_rules_config()
            comparison_rules = _official_rules_for_domain(rules_config, batch.get("domain"))
            comparison_rules.extend(
                other.get("rule") for other in batch.get("rules") or []
                if other.get("draft_id") != draft_id and other.get("status") != "rejected"
            )
            issues = precheck_rule(edited, comparison_rules, batch.get("source_pages") or [])
            item["rule"] = edited
            item["precheck"] = issues
            blockers = [issue for issue in issues if issue.get("blocking")]
            ok = not blockers
            item["status"] = "approved" if ok else "pending"
            item["decision"] = ({"reviewer": reviewer, "comment": comment, "time": now}
                                if ok else None)

        refresh_batch_status(batch)
        audit = {
            "time": now, "action": action if ok else "approve_blocked",
            "batch_id": batch_id, "draft_id": draft_id, "reviewer": reviewer,
            "comment": comment,
        }
        batch.setdefault("audit", []).append(audit)
        store["audit"].append(audit)
        save_draft_store(config.RULE_DRAFTS_FILE, store)
        response_batch = public_batch(batch)

    return {
        "ok": ok,
        "message": "规则已通过" if action == "approve" and ok
                   else "规则已驳回" if action == "reject"
                   else "规则仍有阻断问题，请修改后再次通过",
        "batch": response_batch,
        "rule": next(entry for entry in response_batch["rules"] if entry["draft_id"] == draft_id),
    }


@app.post("/rule-drafts/{batch_id}/publish")
async def publish_rule_draft_batch(batch_id: str, payload: dict = None,
                                   current: dict = Depends(require_roles("reviewer", "admin"))):
    """把负责人确认过的规则一次性发布到正式规则库并留下审计记录。"""
    payload = payload or {}
    reviewer = current.get("display_name") or current.get("username") or "负责人"
    comment = str(payload.get("comment") or "").strip()[:500]

    with RULES_LOCK:
        store = load_draft_store(config.RULE_DRAFTS_FILE)
        batch = _find_draft_batch(store, batch_id)
        if batch.get("published_at"):
            raise HTTPException(status_code=409, detail="该批次已经发布")
        pending = [item for item in batch.get("rules") or [] if item.get("status") == "pending"]
        if pending:
            raise HTTPException(status_code=409, detail=f"还有 {len(pending)} 条规则未处理")

        rules_config = _load_rules_config()
        domain = batch.get("domain") or "通用审查"
        existing_rules = _official_rules_for_domain(rules_config, domain)
        approved_items = [
            item for item in batch.get("rules") or [] if item.get("status") == "approved"
        ]
        repaired_rules = repair_conflicting_rule_ids(
            domain, [item.get("rule") or {} for item in approved_items], existing_rules)
        for item, repaired in zip(approved_items, repaired_rules):
            item["rule"] = repaired

        publish_rules = []
        blocked = []
        comparison_rules = list(existing_rules)
        for item in approved_items:
            rule = clean_rule(item.get("rule") or {})
            issues = precheck_rule(rule, comparison_rules, batch.get("source_pages") or [])
            item["rule"] = rule
            item["precheck"] = issues
            blockers = [issue for issue in issues if issue.get("blocking")]
            if blockers:
                item["status"] = "pending"
                item["decision"] = None
                blocked.append({"draft_id": item.get("draft_id"), "issues": blockers})
            else:
                publish_rules.append(rule)
                comparison_rules.append(rule)

        if blocked:
            refresh_batch_status(batch)
            save_draft_store(config.RULE_DRAFTS_FILE, store)
            raise HTTPException(
                status_code=409,
                detail={"message": "正式库已变化，部分规则需要重新确认", "blocked": blocked},
            )

        bucket = rules_config["domains"].setdefault(
            domain, {"rules": [], "source_docs": []})
        bucket["rules"].extend(publish_rules)
        source_doc = batch.get("source_doc")
        if source_doc and source_doc not in bucket["source_docs"]:
            bucket["source_docs"].append(source_doc)
        rules_config["generated_at"] = utc_now()
        _save_rules_config(rules_config)

        now = utc_now()
        batch["published_at"] = now
        batch["published_by"] = reviewer
        batch["publish_comment"] = comment
        refresh_batch_status(batch)
        audit = {
            "time": now, "action": "publish", "batch_id": batch_id,
            "reviewer": reviewer, "comment": comment, "count": len(publish_rules),
        }
        batch.setdefault("audit", []).append(audit)
        store["audit"].append(audit)
        save_draft_store(config.RULE_DRAFTS_FILE, store)

    ANSWER_CACHE.clear()
    return {
        "ok": True,
        "message": f"已发布 {len(publish_rules)} 条规则，驳回项未进入正式库",
        "published_count": len(publish_rules),
        "batch": public_batch(batch),
    }


@app.post("/review")
async def review_file(file: UploadFile = File(...), user: dict = Depends(require_user)):
    """材料审查：识别领域与子类型 → 路由规则 → 抽事实 → 硬规则 + 软审查 → 评分。"""
    # 与 /upload 同源的类型/体积校验与落盘方式。
    # 【修复】原来是 `f"./temp_{file.filename}"`：没有体积上限、没有类型校验、
    # 文件名未净化（带 ../ 能写到目录外），且只处理 pdf/docx/txt ——
    # 而前端 accept 里还写着 .doc 和图片，上传这两类必然失败或乱码。
    try:
        file_location, _ = await save_upload(file, config.REVIEW_MAX_MB)
    except ValueError as e:
        return {"error": f"❌ {e}"}

    try:
        # ========== 1. 读取待审材料 ==========
        documents = load_documents(file_location, file.filename)
        doc_content = "\n".join([d.page_content for d in documents])
        if not doc_content.strip():
            return {"error": "❌ 未从材料中解析出任何文字"}

        # 先读取规则库，用配置领域与动态规则领域的并集指导类型识别。
        if not os.path.exists(config.RULES_FILE):
            return {"error": "规则库未初始化，请先上传规章制度文件"}
        with RULES_LOCK:
            rules_config = _load_rules_config()
        domain_cfg = prompts.load_domain()
        known_domains = domain_catalog(domain_cfg, rules_config)

        # ========== 2. 识别材料类型（领域 + 子类型） ==========
        print("⏳ 正在识别材料类型...")
        domain_prompt = prompts.review_identify_prompt(doc_content, known_domains)
        key_fields = []
        try:
            domain_info = await review_json_call(
                domain_prompt, "材料类型识别", temperature=config.TEMPERATURE_JUDGE)
            matched_domain = domain_info.get("domain", "通用审查")
            matched_scene = domain_info.get("scene", "通用")
            # 识别阶段给出的 key_fields 以前被丢弃，现在并入第 4 步
            key_fields = [f for f in (domain_info.get("key_fields") or []) if isinstance(f, str)]
        except Exception as e:
            print(f"⚠️ 领域识别解析失败: {e}")
            matched_domain, matched_scene = "通用审查", "通用"

        print(f"✅ 识别到领域：【{matched_domain}】，子类型：【{matched_scene}】")

        # ========== 3. 使用统一领域目录路由规则 ==========
        # 【修复】领域名归一：规则库的领域名来自「上传规章时」的识别，这里是
        # 「审查材料时」的识别，两次常差一点（"奖学金申请" vs "奖学金评定"）。
        # 归一后的名字一并回显，避免「识别成 A、实际用了 B 的规则」无法解释。
        matched_rules, resolved_domain = route_rules(
            matched_domain, matched_scene, rules_config, domain_cfg)
        if resolved_domain != matched_domain:
            print(f"   🔁 领域名归一：【{matched_domain}】→【{resolved_domain}】")
        print(f"✅ 最终匹配到 {len(matched_rules)} 条规则")

        # ========== 4. 动态事实抽取 ==========
        print("⏳ 正在抽取事实清单（动态 schema）...")
        rule_fields = [rule.get("field", "") for rule in matched_rules if rule.get("field")]
        for rule in matched_rules:
            rule_fields.extend(formula_fields(rule))
        # 有规则时只抽取执行规则真正需要的字段。识别阶段给出的 key_fields
        # 常产生“姓名/候选人姓名、赞成/赞成票数”等重复项，会让推理模型
        # 花大量 token 猜字段差异，却不能增加任何可执行规则的覆盖率。
        source_fields = rule_fields if matched_rules else key_fields
        required_fields = sorted({f for f in source_fields if f})
        if not required_fields:
            required_fields = ["姓名", "学号", "学院", "班级", "职位", "日期", "绩点"]
        # 关键词型规则校验的是全文，不需要单独抽字段
        required_fields = [f for f in required_fields if f not in ("正文", "全文", "_full_text")]

        schema_str = "{\n" + ",\n".join(f'    "{f}": ""' for f in required_fields) + "\n}"
        print(f"   📋 动态字段: {required_fields}")
        print(f"   🧩 事实抽取约束：{len(required_fields)} 个短字段，"
              f"输出上限 {config.FACT_MAX_TOKENS} tokens")

        # 事实抽取和软审查互不依赖，并行请求可避免两个 60 秒上限串行累加。
        print("⏳ 正在并行执行事实抽取与软审查...")
        fact_call = review_json_call(
            prompts.fact_extract_prompt(doc_content, schema_str),
            "事实抽取", temperature=config.TEMPERATURE_JUDGE,
            model=config.FACT_MODEL, max_tokens=config.FACT_MAX_TOKENS,
            validator=lambda value: validate_fact_extraction(value, required_fields),
            json_schema=build_fact_json_schema(required_fields))
        soft_call = review_json_call(
            prompts.soft_review_prompt(doc_content),
            "软性审查", temperature=config.TEMPERATURE_JUDGE,
            model=config.SOFT_REVIEW_MODEL, max_tokens=config.SOFT_REVIEW_MAX_TOKENS,
            validator=validate_soft_review)
        fact_outcome, soft_outcome = await asyncio.gather(
            fact_call, soft_call, return_exceptions=True)

        fact_error = None
        if isinstance(fact_outcome, Exception):
            print(f"⚠️ 事实抽取失败，相关规则转为待核验: {fact_outcome}")
            fact_error = "事实抽取服务超时或返回格式异常"
            facts = {"_extraction_failed": True}
        else:
            facts = fact_outcome
        facts["_full_text"] = doc_content

        soft_error = None
        if isinstance(soft_outcome, Exception):
            print(f"⚠️ 软审查失败，本次仅返回硬规则结论: {soft_outcome}")
            soft_error = "软审查服务超时或返回格式异常"
            soft_result = {}
        else:
            soft_result = soft_outcome

        # ========== 5. 硬规则校验 ==========
        print("⏳ 正在执行硬规则校验...")
        hard_results = execute_hard_rules(facts, matched_rules)
        pending_results = [r for r in hard_results if r.get("needs_review")]
        applicable = [r for r in hard_results
                      if not r.get("not_applicable") and not r.get("needs_review")]
        passed_count = sum(1 for r in applicable if r["passed"])
        print(f"✅ 硬规则：{passed_count}/{len(applicable)} 已核验通过，"
              f"{len(pending_results)} 条待核验，"
              f"{sum(1 for r in hard_results if r.get('not_applicable'))} 条不适用")

        # ========== 6. 综合评分 ==========
        total_score, grade, score_detail = compute_score(hard_results, soft_result)
        print(f"   💯 硬规则扣分：致命 {score_detail['critical']} × 30 + "
              f"严重 {score_detail['error']} × 10 + 建议 {score_detail['warning']} × 3 "
              f"= {score_detail['deduction']} 分")
        print(f"   硬规则 {score_detail['hard_score']} / 材料质量 {score_detail['soft_score']} "
              f"→ 审查结论：{grade}")

        next_actions = {
            "未通过": "请先处理致命资格或合规问题，再重新提交审查。",
            "需修改": "请根据未通过规则逐项修改材料，再重新审查。",
            "待核验": "请补充缺失信息，或人工确认待核验项目后再提交。",
            "无法审查": "当前规则库无法覆盖这份材料，请先补充并确认对应制度规则。",
            "通过": "当前已核验项目均通过，可继续按正式流程提交。",
        }

        # 规则里保存的是上传时的原文件名，而磁盘归档使用“原名__内容哈希”防止
        # 同名覆盖。这里把两者重新关联，前端才能展示可点击的真实审查依据。
        source_titles = []
        for result in hard_results:
            for title in result.get("source_docs") or []:
                if title and title not in source_titles:
                    source_titles.append(title)
        try:
            vector_data = vector_db.get(include=["metadatas"])
            source_metadatas = (vector_data or {}).get("metadatas") or []
        except Exception as source_exc:
            print(f"⚠️ 审查依据索引读取失败，将仅显示文件名: {source_exc}")
            source_metadatas = []
        source_refs = build_source_refs(
            source_titles, source_metadatas, config.BASE_DIR / "data")

        review_result = {
            "材料类型": matched_domain,
            "领域归一后": resolved_domain,
            "审查结论": grade,
            "总评分": total_score,
            "等级": grade,
            "硬规则得分": score_detail["hard_score"],
            "软审查得分": score_detail["soft_score"],
            "材料质量评分": score_detail["soft_score"],
            "事实清单": {k: v for k, v in facts.items() if k != "_full_text"},
            "硬规则校验": hard_results,
            "软审查": soft_result,
            "审查依据": source_refs,
            "适用规则数": len(applicable),
            "已核验规则数": score_detail["checked"],
            "待核验规则数": score_detail["pending"],
            "下一步建议": next_actions[grade],
        }
        stage_warnings = [msg for msg in (fact_error, soft_error) if msg]
        if stage_warnings:
            review_result["阶段提示"] = stage_warnings
        if not score_detail["has_hard_rules"]:
            review_result["提示"] = ("没有完成任何硬规则核验，材料质量评分仅反映表述与逻辑，"
                                 "不能代表资格或合规条件已经通过。")
        if config.AUTH_ENABLED:
            archived_path = ""
            try:
                user_dir = os.path.join(config.MATERIALS_DIR, user["id"])
                os.makedirs(user_dir, exist_ok=True)
                suffix = os.path.splitext(file.filename or "")[1].lower()
                archived_path = os.path.abspath(
                    os.path.join(user_dir, f"{uuid.uuid4().hex}{suffix}"))
                shutil.copy2(file_location, archived_path)
                review_result["记录ID"] = AUTH_STORE.record_review(
                    user["id"], file.filename, review_result, archived_path)
            except Exception as history_exc:
                if archived_path and os.path.exists(archived_path):
                    os.remove(archived_path)
                print(f"⚠️ 审查记录保存失败: {history_exc}")
        return {"review_result": review_result}

    except ValueError as e:
        return {"error": f"❌ {e}"}
    except Exception as e:
        print(f"❌ 审查失败: {e}")
        return {"error": f"审查失败：{e}"}
    finally:
        if os.path.exists(file_location):
            os.remove(file_location)


@app.get("/preview/{filename:path}")
async def preview_file(filename: str, _: dict = Depends(require_user)):
    # 安全检查：防止路径穿越攻击
    safe_filename = os.path.basename(filename)
    file_path = os.path.join("./data", safe_filename)  # 假设文件都在 data 目录里

    if not os.path.exists(file_path):
        return {"error": "文件不存在"}

    # 返回文件给浏览器预览
    return FileResponse(file_path)
app.mount("/", StaticFiles(directory="static", html=True), name="static")#这行代码是整个后端项目的“门面装修”，第一先给地址
#第二个装修店面
#uvicorn main:app --reload
