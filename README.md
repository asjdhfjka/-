# 工大智政 — 高校行政智能问答与审查系统（比赛版）

基于 RAG 技术的高校行政知识问答与材料审查系统。

**本目录是 `ai-intern` 的改造版**，改动内容与实测数据见 [`改动说明.md`](改动说明.md)。
桌面原目录 `C:\Users\21003\Desktop\ai-intern` 未做任何改动，两边独立。

---

## 与原版的三个区别

1. **校规不再写死在代码里** —— 文档白名单、场景互斥、专有名词等学校特有约束
   全部移到 `domains/*.yaml`。换一所学校只改一份配置，代码不动。
2. **模型调用收口** —— 原来接入点 ID 硬编码 10 处，现在只在 `config.py` 出现 1 次；
   所有调用经过统一出口，自动计数、统一超时与温度。
3. **调用次数降 40%，上界确定** —— 相关性判定由「每条资料一次请求」合并为
   「一次批量判定」；关闭 SDK 自动重试，消除「4 秒超时实际等 12 秒」的问题。
   调用次数由 `tools/llm_proxy.py` 这个出站计量代理**实测**（两版口径一致），
   不再依赖改造版独有的 `/stats` 接口推断。

---

## 核心功能

- 🔍 **智能问答**：知识库精准问答 + 多轮对话 + 来源溯源 + 拒答
- 📋 **材料审查**：自动抽取规则 + 场景隔离 + 硬规则校验 + 软性审查（与事实抽取并行）+ 三档扣分
- 🧩 **规则审核**：制度文件只生成候选草稿，负责人逐条通过、编辑或驳回后再统一发布
- 📄 **多格式支持**：PDF / Word（含 .doc）/ TXT / 图片 / 扫描件 OCR ——
  上传与审查两条链路共用同一套解析，不会出现「前端能选、后端解不了」
- ⚡ **查询缓存**：重复问题 0 次模型调用，约 3 毫秒返回
- 👤 **轻量用户体系**：账号登录 + 学生/审核员/管理员权限 + 个人问答与审查历史
- 🧭 **角色工作台**：学生、审核员、管理员登录后进入不同首页与业务导航
- 🔄 **材料流转**：学生预审后提交，审核员可标记审核中、退回修改或审核通过
- 📊 **可观测接口**：`/stats` 实时查看调用次数、缓存命中率、当前生效配置

## 技术栈

- 后端：FastAPI + LangChain + Chroma
- 大模型：火山引擎方舟（DeepSeek-V4）
- 嵌入模型：BAAI/bge-small-zh-v1.5
- Rerank：BAAI/bge-reranker-base
- OCR：RapidOCR + PyMuPDF
- 配置与提示词：`config.py` / `prompts.py` / `domains/*.yaml`
- 测试：pytest（纯逻辑用例不加载模型，秒级可跑）

---

## 目录结构

```
ai-intern-比赛版/
├── main.py                  主服务（原版 1271 → 1717 行）
├── auth_store.py            轻量账号、会话与个人记录（SQLite，无新增依赖）
├── config.py                集中配置层：模型、超时、阈值、缓存、重试
├── prompts.py               提示词集中层：通用与领域分离
├── review_engine.py         审查纯逻辑：规则路由 / 硬规则校验 / 评分（可单测）
├── rule_workflow.py         规则草稿预检 / 编号 / 持久化 / 发布前保护
├── llm_utils.py             模型输出文本工具（strip_fence 等）
├── eval/                    评测体系（纯逻辑与执行分离）
│   ├── schema.py            题集数据结构与加载器、题集指纹（纯逻辑）
│   ├── judges.py            判定器：关键事实覆盖 / 拒答混淆 / 三分类（纯逻辑）
│   ├── metrics.py           指标：分类型通过率 + 95% 置信区间（纯逻辑）
│   ├── report.py            由产物 JSON 现读渲染报告，指纹不符即拒出结论
│   ├── cases/               问答 qa_cases.json · 审查 review_cases.json
│   └── runners/             run_qa.py · run_review.py（打服务、写产物）
├── domains/
│   ├── gdut.yaml            本校领域配置（领域清单 / 场景约束 / 别名 / 领域回退）
│   └── _模板.yaml           接入新学校的模板，含逐项注释
├── tools/
│   ├── batch_upload.py      批量上传知识库文件（PDF/TXT/DOCX/DOC/图片）
│   ├── llm_proxy.py         LLM 出站计量代理（对照测试用）
│   ├── serve_original_via_proxy.py  让桌面原版不改一行地走代理
│   └── run_compare.sh       一键起「代理 + 两版 + 对照」
├── tests/                   pytest 用例（15 个文件，含 /review 最小端到端，默认跳过；
│                             其中 test_rule_library_guards.py 对真实规则库设防）
├── bench_local.py           本地检索耗时基准（不烧 token）
├── compare_before_after.py  改造前后端到端对照（调用次数由代理实测）
├── 改动说明.md              改动内容与实测数据
├── archive/                 与当前脚本不配套的历史产物（见其中 README）
├── .env.example             环境变量模板（复制为 .env 后填写；.env 不入库）
├── requirements.txt         运行依赖（UTF-8，已含 PyMuPDF / RapidOCR 等运行期包）
├── data/                    语料（未进 git）
├── chroma_db/               向量库（未进 git）
├── user_materials/          学生材料归档（未进 git，按用户隔离）
└── static/                  三角色比赛展示界面、样式与校园插画
```

---

## 快速开始

### 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.10 或以上（实测 3.13） |
| 磁盘 | 约 6 GB（PyTorch + 本地嵌入/精排模型） |
| 网络 | 首次启动需联网下载模型（已默认走 hf-mirror 镜像） |
| 模型服务 | 火山引擎方舟的 API Key + **你自己的**推理接入点 ID |

### 2. 创建虚拟环境并安装依赖

```bash
python -m venv .venv

# Windows (cmd)
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> **CPU 机器提速**：`requirements.txt` 中 `torch` 默认会装上体积很大的通用轮子。
> 不需要 GPU 时可先单独装 CPU 版，再装其余依赖：
> `pip install torch --index-url https://download.pytorch.org/whl/cpu`
>
> **国内网络**：`config.py` 已把 HuggingFace 端点默认指向 `hf-mirror.com`；
> pip 慢的话可追加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

### 3. 配置密钥

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

然后编辑 `.env`，至少填这两项（文件中已标注「必填」）：

- `ARK_API_KEY` —— 方舟控制台「API Key 管理」创建；
- `ARK_MODEL` —— 方舟控制台「在线推理 → 推理接入点」创建后得到的 ID（形如 `ep-…`）。

> 不要留空直接跑：留空会退回 `config.py` 里的示例接入点，那属于原作者账号，调用会失败。

### 4. 准备知识库（可选但建议）

出于隐私与体积考虑，仓库**不含语料与向量库**（`data/`、`chroma_db/` 均被 `.gitignore` 忽略）。
两种方式任选：

- **批量**：把语料放进 `data/`，启动服务后用 `tools/batch_upload.py` 一次入库（见下文）；
- **少量**：直接启动服务，在页面上传几份文档，边传边用。

### 5. 启动

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

访问 http://127.0.0.1:8001/ 。首次启动约 1 分钟（需加载本地嵌入模型与精排模型），之后常驻。

### 6. 登录

首次启动会自动创建三个演示账号，默认密码均为 `Demo@123456`，角色权限见下表。

### 复用作者本机已有的虚拟环境（仅限本机）

原开发机上已有一个装好全部依赖的虚拟环境，在这台机器上可跳过第 2 步：

```bash
VENV="C:/Users/21003/Desktop/ai-intern/.venv/Scripts/python.exe"

$VENV -m uvicorn main:app --host 127.0.0.1 --port 8001
```

### 登录与角色

本地比赛演示首次启动会自动创建三个账号，默认密码均为 `Demo@123456`：

| 账号 | 角色 | 权限 |
|---|---|---|
| `student` | 学生 | 智能问答、材料预审、查看自己的历史记录 |
| `reviewer` | 审核员 | 学生权限 + 知识入库、规则草稿审核、统计查看 |
| `admin` | 管理员 | 全部权限 + 创建和停用账号、重置统计 |

登录状态由 HttpOnly Cookie 保存，默认有效 7 天；密码以 PBKDF2-SHA256 加盐保存，
服务端只保存会话令牌的哈希值。账号、会话和个人历史位于 `auth.db`，该文件已加入
`.gitignore`。已有匿名操作不会自动归属到某个新账号，新产生的问答和审查才会进入个人历史。

部署到可公开访问的环境时，至少应修改 `AUTH_DEMO_PASSWORD`，并设置
`AUTH_COOKIE_SECURE=1`；创建正式账号后可设置 `AUTH_BOOTSTRAP_DEMO_USERS=0`，
停止自动补建演示账号。

### 批量上传知识库文件

先启动服务，再将整个目录中的支持文件依次上传：

```bash
python tools/batch_upload.py data --recursive --report batch_upload_report.json
```

也可以指定多个文件或目录：

```bash
python tools/batch_upload.py "资料/通知.pdf" "资料/制度目录" --recursive
```

脚本支持 PDF、TXT、DOCX、DOC、PNG、JPG/JPEG，默认连接
`http://127.0.0.1:8001`。上传前想先确认文件清单，可添加 `--dry-run`；
服务运行在其他地址时使用 `--base-url http://主机:端口`。服务端会跳过内容完全相同的文件，
同名但内容有变化的文件会作为新版本入库。运行 `python tools/batch_upload.py --help`
可查看超时、重试、大小限制等完整参数。

制度类文件上传完成后不会立即影响材料审查。点击页面右上角的 **🧩**：

1. 查看模型抽取的候选规则、制度原文与自动预检结果；
2. 逐条选择“通过 / 保存修改”或“驳回”；
3. 所有候选都处理完后，点击“发布已通过规则”。

缺少原文依据、状态规则缺少正反值、字段结构不完整或与正式库重复的规则不能通过。
审核人、操作时间、备注和发布结果保存在 `rule_drafts.json`；正式审查仍只读取
`auto_rules.json` 中已经发布的规则。

### 环境变量（均有默认值，不设也能跑）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ARK_API_KEY` | 读 `.env` | 火山引擎密钥 |
| `ARK_MODEL` | `ep-2026…` | 推理接入点 ID |
| `REVIEW_MODEL` | 同 `ARK_MODEL` | 材料类型识别等审查任务使用的接入点 |
| `FACT_MODEL` | 同 `REVIEW_MODEL` | 事实抽取接入点；可单独配置快速非推理模型 |
| `SOFT_REVIEW_MODEL` | 同 `REVIEW_MODEL` | 软性审查接入点；建议使用支持 JSON 输出的快速模型 |
| `ARK_BASE_URL` | 方舟官方地址 | 改它即可把出站请求指向计量代理 |
| `DOMAIN_NAME` | `gdut` | 使用 `domains/<名字>.yaml` |
| `ARK_MAX_RETRIES` | `0` | 自动重试次数，见改动说明 3.3 |
| `CACHE_ENABLED` | `1` | 查询级缓存开关 |
| `RERANK_POOL` | `10` | 精排候选上限（CPU 瓶颈，勿轻易调大） |
| `MIN_CHUNK_CHARS` | `150` | 片段最短长度（已接线，见改动说明第八节） |
| `REVIEW_MAX_MB` | `20` | 单次审查的文件体积上限 |
| `MAX_APPLICABLE_RULES` | `30` | 单次审查最多执行的硬规则条数 |
| `TIMEOUT_REVIEW` | `60` | `/review` 单次模型调用超时（审查 prompt 是整篇材料） |
| `REVIEW_RETRIES` | `1` | 审查结构化 JSON 失败后的显式重试次数 |
| `REVIEW_DISABLE_THINKING` | `1` | 结构化审查关闭深度思考，避免思维链吃光输出额度；不支持时自动兼容重试 |
| `REVIEW_JSON_MODE` | `1` | 要求模型返回 JSON 对象；不支持时自动兼容重试 |
| `FACT_MAX_TOKENS` | `6000` | 事实抽取的输出安全上限；实际输出由动态 JSON Schema 限制为短字段 |
| `SOFT_REVIEW_MAX_TOKENS` | `2500` | 软性审查的输出上限 |
| `REVIEW_MAX_TOKENS` | `8000` | 其他审查 JSON 调用的兼容输出上限 |
| `REVIEW_CONTENT_MAX_CHARS` | `12000` | 送审查模型的正文长度上限（硬规则仍查完整原文） |
| `RULE_DRAFTS_FILE` | `rule_drafts.json` | 候选规则、人工决定和发布记录的本地草稿库 |
| `AUTH_ENABLED` | `1` | 是否启用登录和角色权限；仅调试旧流程时可设为 `0` |
| `AUTH_DB_PATH` | `auth.db` | 用户、会话、个人问答和审查历史数据库 |
| `AUTH_SESSION_DAYS` | `7` | 登录会话有效天数 |
| `AUTH_BOOTSTRAP_DEMO_USERS` | `1` | 首次启动时补建三个比赛演示账号 |
| `AUTH_DEMO_PASSWORD` | `Demo@123456` | 演示账号初始密码 |
| `AUTH_COOKIE_SECURE` | `0` | HTTPS 部署时设为 `1` |
| `MATERIALS_DIR` | `user_materials` | 个人预审和正式提交材料的本地归档目录 |

`.env` 的加载发生在 `config.py` 的最前面 —— 即**任何 `os.getenv` 之前**。
改造前是「先 import config、后 load_dotenv()」，导致写在 `.env` 里的
`ARK_MODEL` / `TIMEOUT_*` / `CACHE_ENABLED` 其实全都不生效（详见改动说明第八节）。

完整清单见 `config.py`。

---

## 测试与评测

```bash
# 1. 纯逻辑用例（不加载模型、不调用大模型，秒级）
python -m pytest tests -q

# 2. 端到端用例（需要模型与向量库，默认跳过）
RUN_INTEGRATION=1 python -m pytest tests/test_api_integration.py -q

# 3. 批量评测（需先启动服务；默认打 8001，可用 TEST_BASE 覆盖）
python eval/runners/run_qa.py --split all        # 问答：严格 + 宽松双口径
python eval/runners/run_review.py --split all    # 审查：三分类判定 + 假阳性

# 4. 由产物现读生成报告（指纹与产物不符即拒绝出报告）
python eval/report.py
```

评测题集固定在 `eval/cases/*.json`，产物 `eval_results_qa.json` /
`eval_results_review.json` 带题集指纹（统一由 `eval/schema.py` 计算）。
一旦产物与当前题集不配套，`eval/report.py` 直接拒绝出报告、
`tests/test_eval_artifact.py` 也会让 CI 红 —— 避免「用旧数据冒充新结论」。
判定与指标全部在 `eval/judges.py`、`eval/metrics.py`（纯逻辑、可单测），
执行器只负责打接口与汇总；报准确率时严格与宽松两个口径必须并列。

---

## 接口

| 接口 | 说明 |
|---|---|
| `POST /auth/login` | 登录并设置安全会话 Cookie |
| `POST /auth/logout` | 退出并销毁当前会话 |
| `GET /auth/me` | 获取当前用户与角色 |
| `GET/POST /auth/users` | 管理员查看或创建账号 |
| `PATCH /auth/users/{id}/active` | 管理员启用或停用账号 |
| `GET /me/chats` | 当前用户最近的问答记录 |
| `GET /me/reviews` | 当前用户最近的材料审查记录 |
| `POST /me/reviews/{id}/submit` | 学生将一次预审结果提交负责人审核 |
| `GET /submissions` | 学生查看自己的提交；审核员和管理员查看全部队列 |
| `GET /submissions/{id}` | 查看一条正式提交及其 AI 预审结果 |
| `PATCH /submissions/{id}` | 审核员更新状态并填写人工意见 |
| `GET /me/materials/{review_id}/file` | 按权限查看归档的原始材料 |
| `POST /chat/stream` | 流式问答，首行为 `__SOURCES__[…]__END__` 来源头 |
| `POST /upload` | 上传文档入库；规则文档只生成待确认草稿 |
| `GET /rule-drafts` | 查看最近的规则草稿批次与审核状态 |
| `PATCH /rule-drafts/{batch}/rules/{draft}` | 通过、编辑后通过或驳回单条候选规则 |
| `POST /rule-drafts/{batch}/publish` | 将整批已确认规则发布到正式规则库 |
| `POST /review` | 材料审查（multipart 上传） |
| `GET /preview/{file}` | 查看来源原文 |
| `GET /stats` | **新增**：调用次数、缓存命中率、生效配置 |
| `POST /stats/reset` | **新增**：清空计数与缓存 |

---

## 换一所学校

1. 复制 `domains/_模板.yaml` 为 `domains/<学校代号>.yaml` 并填写
2. 启动时设 `DOMAIN_NAME=<学校代号>`

不需要改任何 Python 代码。配置文件里所有内容都会被真正使用：
`name` / `alias` / `contact_hint` 进提示词，`domains` 决定可识别的业务领域，
`domain_aliases` 兜住两次识别之间的名称漂移，`scenario_constraints` 防张冠李戴，
`review_scenes` 是材料子类型枚举。

可用一条命令验证渲染结果：

```bash
python -c "import prompts; print(prompts.answer_system_prompt('X','2026年09月30日',prompts.load_domain('_模板')))"
```

---

## 已知边界

详见 [`改动说明.md`](改动说明.md) 第七、八节。摘要：

- `main.py` 仍是单文件（1717 行）。审查规则引擎已拆到 `review_engine.py`，
  其余部分（文档解析、检索、路由）尚未拆分
- BM25 索引全量重建期间，并发问答可能读到中间状态。当前百级片段影响可忽略，
  上万片段需改增量索引或加读写锁
- 端到端耗时受远程模型波动主导（1.25s～11.5s），未取得稳定优势，
  结论以「调用次数」与「本地检索耗时」这两个确定性指标为准
- `/review` 已改用独立 `TIMEOUT_REVIEW`（默认 60s），事实抽取与软审查并行发起；
  结构化阶段默认关闭深度思考并要求 JSON 输出，软审查还会校验完整字段和数值评分，
  不会再把“可解析但缺评分”的字典误报为成功
- 审查依据会把规则库原文件名关联到 `data/` 中的实际归档版本，前端可直接点击预览；
  没有归档原文的旧规则会明确显示“原文未归档”
- `domains/gdut.yaml` 与规则库 `auto_rules.json` 的领域错配**已修复**：
  4 个「有规则却未声明」的领域（共 96 条）已写进 yaml；6 个「零规则且无兜底」的领域
  （请假管理/学生干部/违纪处分/转专业申请/宿舍管理/助学贷款）已从 yaml 移除。
  yaml 内写明不变量：只声明「规则库确有规则」或「有 `rule_domain_fallbacks` 兜底」的领域；
  后续上传对应制度文件会自动抽取入库，届时把领域加回清单即可（详见评测报告缺陷 D1）
- **规则数据质量已设防**（缺陷 D11~D13）：规则抽取把制度条文编成规则时，错编语义**不会报错**，
  只会静默把合格材料判成不合格（实测：一份没有挂科的综测材料被判「存在挂科」）。
  现由三层防线兜住 —— 抽取提示词约束 type 的选用规则、引擎的 `status` 三态与元规则判定、
  以及 `tests/test_rule_library_guards.py` 对真实 `auto_rules.json` 的守卫断言
  （该守卫在首次运行时就抓出第三条同类规则）。详见 `改动说明.md` 第 9.4 节
