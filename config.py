"""集中配置层 —— 所有可调参数与外部依赖设置的唯一入口。

为什么要有这个文件：
    改造前，模型接入点 ID（ep-xxx）在 main.py 里散落多处，
    块大小 800/1500、阈值 0.3、超时 3.0/10.0/20.0 等也散落各处。
    换一个推理接入点或调一个阈值，需要全文搜索，容易漏改。

现在：读环境变量，带默认值；改配置不用动业务代码。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ============ 先加载 .env，再读任何环境变量 ============
# 【修复】改造前是「main.py 先 import config，第 205 行才 load_dotenv()」。
# 而本文件所有 os.getenv(...) 都在「被 import 的那一刻」求值完毕，
# 于是 .env 里的 ARK_MODEL / TIMEOUT_* / CACHE_ENABLED / ARK_MAX_RETRIES
# 全部读不到，只有 ARK_API_KEY 靠 main.py 里一句
# `config.ARK_API_KEY or os.getenv("ARK_API_KEY")` 兜住了 —— 这就是那句
# 看着多余的写法存在的原因，也是「改了 .env 却没生效」的根因。
# 现在把加载放在本文件最前面，配置来源收敛为：真实进程环境 + 项目根 .env。
from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

# ============ 大模型服务（火山引擎方舟） ============
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
# 推理接入点 ID。改造前这个字符串散落在代码里多处，现在只有这一处。
ARK_MODEL = os.getenv("ARK_MODEL", "ep-20260919155615-h2mfv")
# 审查可以单独使用一个响应更快的接入点；未配置时继续使用主模型。
REVIEW_MODEL = os.getenv("REVIEW_MODEL", ARK_MODEL)
# 事实抽取和软审查可分别接入更快的非推理模型；默认沿用 REVIEW_MODEL。
FACT_MODEL = os.getenv("FACT_MODEL", REVIEW_MODEL)
SOFT_REVIEW_MODEL = os.getenv("SOFT_REVIEW_MODEL", REVIEW_MODEL)

# 是否让模型客户端绕开系统代理。
# 改造前是 `os.environ["NO_PROXY"] = "localhost,127.0.0.1,ark.cn-beijing.volces.com"`，
# 在 import 阶段改全局环境变量，会静默影响同一进程里所有其他 HTTP 客户端。
# 现在改成只在造客户端时传一个 trust_env=False 的 httpx client，作用域收在客户端内部。
# 若所在网络确实需要代理才能访问方舟，把环境变量 ARK_BYPASS_PROXY 设为 0 即可。
ARK_BYPASS_PROXY = os.getenv("ARK_BYPASS_PROXY", "1") == "1"

# 模型客户端的自动重试次数。
#
# 这是本次改造里最容易被忽略、但影响最大的一处。
# OpenAI SDK 默认 max_retries=2，意思是「超时会自动重试两次」——
# 于是代码里写 timeout=4 秒，实际最坏要等 3×4≈12 秒才失败，超时配置形同虚设。
# 实测改造前单次问答出现过一次 18.9 秒，其中 13.7 秒全耗在这里。
#
# 本系统每一处模型调用都有明确的时间上界和降级路径（相关性判定失败就保留资料、
# 生成失败就提示重试），所以「悄悄重试」带来的是不可预测的延迟，而不是可靠性。
# 因此默认关掉，让每次调用的耗时上界变成确定值 —— 演示时这一点尤其重要。
ARK_MAX_RETRIES = int(os.getenv("ARK_MAX_RETRIES", "0"))

# ============ 生成参数 ============
# 检索决策路径（查询重写、相关性判定、规则抽取）一律 0，保证可复现；
# 只有最终作答保留一点随机性，避免措辞过度模板化。
TEMPERATURE_REWRITE = float(os.getenv("TEMPERATURE_REWRITE", "0.0"))
TEMPERATURE_JUDGE = float(os.getenv("TEMPERATURE_JUDGE", "0.0"))
TEMPERATURE_GENERATE = float(os.getenv("TEMPERATURE_GENERATE", "0.3"))

# ============ 超时（秒） ============
TIMEOUT_REWRITE = float(os.getenv("TIMEOUT_REWRITE", "10"))
# 相关性判定超时。判定失败会「默认保留」该条资料，所以这个值宁可紧凑也不要放宽：
# 放宽只会让单次问答更慢，并不会让结果更准。
TIMEOUT_JUDGE = float(os.getenv("TIMEOUT_JUDGE", "4"))
TIMEOUT_GENERATE = float(os.getenv("TIMEOUT_GENERATE", "20"))
TIMEOUT_AUX = float(os.getenv("TIMEOUT_AUX", "15"))
# 材料审查包含较长 JSON 输出。30 秒在图片 OCR 或模型繁忙时容易误判超时，
# 默认提高到 60 秒；事实抽取与软审查会并行，因此不会把两份等待时间相加。
TIMEOUT_REVIEW = float(os.getenv("TIMEOUT_REVIEW", "60"))

# ============ 检索 ============
# 单路召回条数。改造前向量与 BM25 各写死 5，合起来才 10 条候选，
# 精排的输入池被上游限死。这里各提到 20（召回本身很便宜），
# 但精排池单独限制——交叉编码器在 CPU 上逐对打分，是整条链路里最贵的一步。
RETRIEVE_VECTOR_K = int(os.getenv("RETRIEVE_VECTOR_K", "20"))
RETRIEVE_BM25_K = int(os.getenv("RETRIEVE_BM25_K", "20"))
RERANK_POOL = int(os.getenv("RERANK_POOL", "10"))      # 送进精排的候选上限（CPU 瓶颈，勿轻易调大）
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "5"))     # 精排后保留条数
RERANK_MIN_SCORE = float(os.getenv("RERANK_MIN_SCORE", "0.1"))
RERANK_RATIO = float(os.getenv("RERANK_RATIO", "0.3"))  # 动态阈值 = 最高分 × 该比例
CRAG_TOP_N = int(os.getenv("CRAG_TOP_N", "5"))          # 送进相关性判定的条数
# 批量判定时每条资料截取的字数。截得越长判断越准、但提示词越长调用越慢，
# 实测这一项对耗时影响明显，所以做成可调的。
CRAG_DOC_CHARS = int(os.getenv("CRAG_DOC_CHARS", "450"))

# ============ 文档分块 ============
PARENT_CHUNK_SIZE = int(os.getenv("PARENT_CHUNK_SIZE", "1500"))
PARENT_CHUNK_OVERLAP = int(os.getenv("PARENT_CHUNK_OVERLAP", "200"))
CHILD_CHUNK_SIZE = int(os.getenv("CHILD_CHUNK_SIZE", "800"))
CHILD_CHUNK_OVERLAP = int(os.getenv("CHILD_CHUNK_OVERLAP", "200"))
MIN_CHUNK_CHARS = int(os.getenv("MIN_CHUNK_CHARS", "150"))

# ============ 查询级缓存 ============
# 同一所学校里，学生问的问题高度重复。命中缓存直接返回，调用次数为 0。
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "1") == "1"
CACHE_SIZE = int(os.getenv("CACHE_SIZE", "128"))

# ============ 领域配置 ============
# 学校特有的约束（文档白名单、场景互斥、专有名词）放在 domains/<名字>.yaml。
# 换一所学校只需要换这个文件名，代码零改动。
DOMAIN_NAME = os.getenv("DOMAIN_NAME", "gdut")
DOMAINS_DIR = BASE_DIR / "domains"

# ============ 材料审查 ============
# 单次审查允许的最大文件体积（MB）。/upload 一直是 20MB，/review 以前没有上限，
# 一个几百 MB 的文件就能把进程内存吃满，这里与 /upload 对齐。
REVIEW_MAX_MB = int(os.getenv("REVIEW_MAX_MB", "20"))
# 发给审查模型的正文长度上限。硬规则仍检查完整原文，这里只约束模型提示词，
# 防止扫描 PDF/OCR 重复文本让模型请求变得过长。
REVIEW_CONTENT_MAX_CHARS = int(os.getenv("REVIEW_CONTENT_MAX_CHARS", "12000"))
# 结构化 JSON 阶段失败后进行一次显式重试。SDK 的全局自动重试仍保持关闭，
# 避免普通问答的延迟变得不可预测。
REVIEW_RETRIES = int(os.getenv("REVIEW_RETRIES", "1"))
REVIEW_RETRY_DELAY = float(os.getenv("REVIEW_RETRY_DELAY", "1"))
# 审查模型单次**输出**上限。
# ⚠️ 本接入点（火山方舟 DeepSeek-V4）是**推理型模型**：它先产出思维链，放在
# 响应的 reasoning_content 里，而思维链**同样计入 max_tokens**。
# 预算给小了，思维链会吃光额度、正式 content 变成空串（finish_reason=length），
# 外层只会看到「JSON 解析失败：Expecting value: line 1 column 1 (char 0)」——
# 一个看起来像解析 bug、实际是预算不够的假象。
# 实测：软审查 = 思维链 + 输出合计约 2600~3000 tokens，事实抽取 1200~2900
# （**波动极大**，单次思维链可达 ~3900 字符）；故留足 8000 以覆盖最长情况。
# 若换成非推理模型或不需要思维链，可调回 1200 左右。
REVIEW_MAX_TOKENS = int(os.getenv("REVIEW_MAX_TOKENS", "8000"))
# 结构化抽取无需长思维链。方舟支持的模型会关闭深度思考并强制 JSON 输出；
# 若接入点不支持，调用层会自动降级为普通请求再重试。
REVIEW_DISABLE_THINKING = os.getenv("REVIEW_DISABLE_THINKING", "1") == "1"
REVIEW_JSON_MODE = os.getenv("REVIEW_JSON_MODE", "1") == "1"
# 2000 在事实字段较多时会把尚未闭合的 JSON 截断（finish_reason=length）。
# JSON Schema 会把正常输出压到很短；6000 是异常情况下仍能完整闭合的安全余量。
FACT_MAX_TOKENS = int(os.getenv("FACT_MAX_TOKENS", "6000"))
SOFT_REVIEW_MAX_TOKENS = int(os.getenv("SOFT_REVIEW_MAX_TOKENS", "2500"))
# 一次审查最多执行多少条硬规则（防止规则库膨胀后单次审查过慢）
MAX_APPLICABLE_RULES = int(os.getenv("MAX_APPLICABLE_RULES", "30"))
# 规则库文件路径（auto_rules.json 由上传规则文档时生成，rebuild_rules.py 可重建）
RULES_FILE = os.getenv("RULES_FILE", "auto_rules.json")
# 模型抽取的候选规则先进入草稿库，经人工确认后才写入 RULES_FILE。
RULE_DRAFTS_FILE = os.getenv("RULE_DRAFTS_FILE", str(BASE_DIR / "rule_drafts.json"))

# ============ 轻量用户体系 ============
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "1") == "1"
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", str(BASE_DIR / "auth.db"))
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "gdut_session")
AUTH_SESSION_DAYS = int(os.getenv("AUTH_SESSION_DAYS", "7"))
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "0") == "1"
MATERIALS_DIR = os.getenv("MATERIALS_DIR", str(BASE_DIR / "user_materials"))
# 比赛本地演示默认创建 student/reviewer/admin 三个账号。正式部署应关闭并使用管理接口建号。
AUTH_BOOTSTRAP_DEMO_USERS = os.getenv("AUTH_BOOTSTRAP_DEMO_USERS", "1") == "1"
AUTH_DEMO_PASSWORD = os.getenv("AUTH_DEMO_PASSWORD", "Demo@123456")

# ============ 本地模型 ============
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
CHROMA_DIR = os.getenv("CHROMA_DIR", "./chroma_db")


def summary() -> str:
    """启动时打印一行配置摘要，方便现场核对用的是哪套参数。"""
    return (f"模型={ARK_MODEL} 召回k={RETRIEVE_VECTOR_K}/{RETRIEVE_BM25_K} "
            f"精排池={RERANK_POOL}→{RERANK_TOP_N} CRAG={CRAG_TOP_N}条×{CRAG_DOC_CHARS}字/次 "
            f"重试={ARK_MAX_RETRIES} 缓存={'开' if CACHE_ENABLED else '关'}({CACHE_SIZE}) "
            f"领域={DOMAIN_NAME} 规则上限={MAX_APPLICABLE_RULES} "
            f"审查={REVIEW_MODEL}/{TIMEOUT_REVIEW:g}s×{REVIEW_RETRIES + 1}")
