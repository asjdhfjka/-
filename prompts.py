"""提示词集中层 —— 所有对大模型说的话都放这里。

改造动机：
    改造前，9 处提示词散落在 main.py 的 1271 行里，其中「作答提示词」把
    本校特有的东西以自然语言硬编码了进去：
      · 「四级报名费只允许引用《大学英语四六级》文档」—— 写死了文档名
      · 「四级缴费 vs 学费缴纳」—— 写死了业务场景
      · 示范句里写死了人名「何华儒」
    后果：换一所学校就必须改代码，且改完没有任何地方能核对改全了没有。

改造后分成两层：
    · 通用层（本文件）：修辞、格式、拒答策略、防串行核对 —— 任何学校都成立
    · 领域层（domains/*.yaml）：文档白名单、场景互斥、专有名词 —— 换学校只改这一份

本文件不认识任何一所学校。
"""

import re
from functools import lru_cache

import yaml

import config

# ============================================================
# 领域配置：加载 domains/<name>.yaml
# ============================================================

_MISSING_DOMAIN = {"name": "未配置", "alias": [], "domains": {}, "domain_aliases": {},
                   "rule_domain_fallbacks": {},
                   "contact_hint": "", "scenario_constraints": [],
                   "entity_guard": {}, "review_scenes": {}, "extra_rules": []}


@lru_cache(maxsize=8)
def load_domain(name: str = None) -> dict:
    """读取领域配置。

    找不到配置文件时返回空领域而不是抛异常 —— 主体功能不因为这个文件缺失而挂掉，
    这是改造前没有的健壮性。
    """
    name = name or config.DOMAIN_NAME
    path = config.DOMAINS_DIR / f"{name}.yaml"
    if not path.exists():
        print(f"[prompts] 领域配置不存在，按通用模式运行：{path}")
        return dict(_MISSING_DOMAIN)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[prompts] 领域配置解析失败，按通用模式运行：{e}")
        return dict(_MISSING_DOMAIN)

    merged = dict(_MISSING_DOMAIN)
    merged.update(data)
    return merged


def scene_enum(domain: dict = None) -> list:
    """可选的审核场景子类型，来自领域配置。"""
    domain = domain or load_domain()
    return list((domain.get("review_scenes") or {}).keys()) or ["通用"]


def domain_names(domain: dict = None) -> list:
    """已知业务领域名清单，来自领域配置。

    【改造点】原来这份清单（DOMAIN_TEMPLATES）硬编码在 main.py 里，
    正好是「换一所学校必须改代码」的最后一处漏网 —— 换学校时领域划分
    几乎必然要改。现在它跟其它校规一起落在 domains/*.yaml，代码不认识学校。
    """
    domain = domain or load_domain()
    return list((domain.get("domains") or {}).keys())


def _doc_label(pattern: str) -> str:
    """把配置里的通配符写法 `*四、六级*` 渲染成人话「含 四、六级」，
    让提示词读起来是自然语言而不是 glob。"""
    core = str(pattern).strip().strip("*")
    return "不限" if not core else f"含「{core}」的文档"


def _domain_block(domain: dict) -> str:
    """把配置里的场景约束渲染成人话，供作答提示词使用。

    这一步就是「解耦」的落点：代码负责拼装，学校负责填内容。
    """
    lines = []

    for item in domain.get("scenario_constraints") or []:
        name = item.get("name", "")
        triggers = "、".join(item.get("triggers") or []) or "—"
        docs = "、".join(_doc_label(p) for p in (item.get("allow_documents") or [])) or "不限"
        note = " ".join(str(item.get("conflict_note", "")).split())
        line = f"- 涉「{name}」（触发词：{triggers}）时，只允许引用：{docs}。"
        if note:
            line += note
        # YAML 里的多行折行会带出多余空格，这里收干净，让提示词读起来正常
        lines.append(re.sub(r"([，。；：、])\s+", r"\1", line))

    for i, extra in enumerate(domain.get("extra_rules") or [], 1):
        lines.append(f"- （补充规则{i}）{extra}")

    if not lines:
        return ""
    return ("\n\n【本校场景约束】（优先级高于上面的通用规则，用于避免把两件不同的事混为一谈）：\n"
            + "\n".join(lines))


# ============================================================
# 一、作答提示词（原 main.py 第 746-769 行）
# ============================================================

def answer_system_prompt(context: str, current_date: str, domain: dict = None) -> str:
    domain = domain or load_domain()
    guard = domain.get("entity_guard") or {}

    rules = [
        "参考资料已经过相关性精排，最相关的排在最前面。请优先关注排在前面的资料，后面的资料仅供参考。",
        "如果参考资料中出现了具体的日期、时间等细节，请直接提取并回答。",
        "在回答时，请在相关句子的末尾标注 [来源X]（X是数字），"
        "绝对不要在正文里编写“来源说明”这类解释段落。",
        "【兜底回答策略】如果参考资料中没有直接写明用户问的内容，但**有部分相关的信息**"
        "（例如用户问“准备什么材料”，资料里写了“报名方式、学号、报名网址”），请不要直接拒绝回答。"
        "你可以说：“资料中未明确列出所需的具体材料清单，但根据报名通知，您可能需要提前准备学号、"
        "登录报名网站并核对个人信息。建议您具体参考报名系统的提示。”**绝对不要凭空编造材料清单。**",
    ]

    if guard.get("subject_check", True):
        example = guard.get("example_subject")
        shown = f"【{example}】" if example else "【该主体】"
        rules.append(
            "【主语核对规则】如果用户的问题是一个省略句（例如“啥职位啊”），"
            "而你拿到的参考资料里全是与上文无关的内容（比如上一轮聊的是某个人，这轮资料全是招聘会），"
            f"请**不要强行用现有资料回答**。请直接说：“根据现有资料，未找到与{shown}相关的职位信息。”"
        )

    if guard.get("table_cross_row", True):
        rules.append(
            "【表格数据防串行规则】如果参考资料是由表格 OCR 识别而来的扁平文本，包含多行多列数据，"
            "请**极其严格地核对每一行的内容**。绝对不允许将某一行的人名、职位、学号与其他行的内容"
            "进行交叉组合！如果发现姓名和职位/学号在不同行或无法100%确认对应关系，"
            "请如实回答“资料排版可能存在错位，无法准确确认”，不要强行拼凑。"
        )

    # 学校配置里填的「找不到时该问谁」，在这里变成一句明确的求助出口。
    # 这一项以前写在 YAML 里却没有任何代码读它，属于死配置。
    contact = str(domain.get("contact_hint") or "").strip()
    if contact:
        rules.append(
            f"【求助出口】当资料确实无法回答用户的问题时，在说明「未找到」之后，"
            f"请建议用户向{contact}咨询，不要只丢一句「无法回答」就结束。"
        )

    numbered = "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))
    school = domain.get("name") or "本校"
    aliases = "、".join(domain.get("alias") or [])
    school_line = f"{school}（简称：{aliases}）" if aliases else school

    return (
        "你是一个高校行政问答助手。请严格根据以下提供的参考资料回答用户的问题。\n"
        "如果资料中完全没有提到用户询问的事项，请直接说“根据现有资料，我无法回答这个问题”，不要自己编造。\n\n"
        f"【当前时间】：{current_date}\n"
        f"【服务学校】：{school_line}\n\n"
        f"【核心规则】（请务必严格遵守）：\n{numbered}"
        f"{_domain_block(domain)}\n\n"
        f"参考资料：\n{context}\n"
    )


# ============================================================
# 二、查询重写（原 main.py 第 615-631 行）
# ============================================================

def rewrite_prompt(history: str, user_input: str, now_ym: str) -> str:
    year = now_ym.split("年")[0]
    return f"""你是一个查询改写专家。请结合历史对话，将用户的最新问题改写成一个完整、独立、适合用于检索的问题。
            【当前时间】是：{now_ym}。
            【重要规则】（请务必严格遵守）：
            1. ⭐【代词消解】如果用户的最新问题是一个简短的省略句（例如“啥职位啊”、“他的电话呢”、“那个怎么弄”），请**务必从历史对话中找到具体的实体（人名、文件名、事物名），补全主语**。
            2. 如果用户的最新问题是一个追问（包含“它”、“这个”、“那”等代词），请结合历史对话补全主语。
            3. ⭐【新话题隔离】如果用户的最新问题是一个完全无关的全新话题，请彻底忽略历史对话，绝对不要把旧话题的词汇混入新问题中。
               注意：如果用户的最新问题本身已经**包含了完整的语义**（如“那学校的学费怎么交”里已经明确了“学校的学费”），不要画蛇添足加“学生所在学校的”这种冗余前缀，直接用原意即可。
            4. 如果用户问的是“什么时候”、“时间”、“日期”，请在改写后的问题中明确加上“{year}年”或“最新”等时间限定词。
            5. 只输出改写后的问题，不要回答，不要解释。
            6. ⭐【对比类问题】如果用户问“A 和 B 分别是什么”、“A 与 B 有什么不同”，请把问题拆成两个独立子问题：
            - 例如：“上半年和下半年报名时间对比” → “{year}年上半年计算机等级考试报名时间 和 {year}年下半年计算机等级考试报名时间”
            - 保留两个主体，不要丢掉任何一个
            历史对话：
            {history}
            最新问题：{user_input}
            改写结果："""


# ============================================================
# 三、相关性判定 CRAG
#    单条版保留作降级用；批量版是新增的，把 N 次往返压成 1 次。
# ============================================================

_CRAG_RULES = """【重要规则】：
            1. 如果资料中提到了问题里的**核心实体或关键词**（如“报名费”、“准考证”、“收费标准”、“打印”等），即使资料讲的是其他内容，也应回答 YES。
            2. 如果问题是“A 和 B 分别是什么”这种**对比类问题**，只要资料涉及 A 或 B 任一侧，就输出 YES。
            3. 不要因为资料中没有直接出现“是什么”、“多少”这种问法就判 NO。"""


def crag_single_prompt(query: str, doc_text: str) -> str:
    return f"""判断以下资料是否与问题相关。只输出 YES 或 NO，不要解释。

            {_CRAG_RULES}

            【问题】：{query}

            【资料（全文）】：
            {doc_text[:1500]}

            只输出 YES 或 NO。"""


def crag_batch_prompt(query: str, doc_texts: list, doc_chars: int = None) -> str:
    """把 N 条资料一次性交给模型判定。

    改造前是「每条资料发一次请求」，5 条就是 5 次往返，其中 4 次是重复的协议开销。
    这里保留完全相同的判断标准，只把 N 次请求合并成 1 次。
    """
    doc_chars = doc_chars or config.CRAG_DOC_CHARS
    parts = [f"请逐条判断下列 {len(doc_texts)} 段资料是否与问题相关。每段只回答 YES 或 NO，不要解释。",
             "", _CRAG_RULES, "", f"【问题】：{query}", ""]
    for i, text in enumerate(doc_texts, 1):
        parts.append(f"【资料{i}】：")
        parts.append(text[:doc_chars])
        parts.append("")
    parts.append(f"请严格按以下格式逐行输出，共 {len(doc_texts)} 行，不要输出任何其他内容：")
    for i in range(1, len(doc_texts) + 1):
        parts.append(f"{i}:YES")
    return "\n".join(parts)


_LINE_RE = re.compile(r"^\s*资?料?\s*(\d+)\s*[:：.、)\]）]\s*(YES|NO|是|相关|不相关)", re.IGNORECASE)


def parse_crag_batch(raw: str, total: int) -> list:
    """解析批量判定结果。

    解析不到的条目一律判为「相关」—— 与改造前「超时或失败默认保留」的口径一致：
    宁可多给一条，也不要误杀真正有用的资料。
    """
    verdicts = [True] * total
    for line in str(raw).splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < total:
            token = m.group(2).upper()
            verdicts[idx] = token in ("YES", "是", "相关")
    return verdicts


# ============================================================
# 四、文档入库时的规则判定与领域识别
# ============================================================

def is_rule_doc_prompt(filename: str, content: str) -> str:
    return f"""你是一个高校行政文档分析专家。请判断以下文档**是否包含可执行的审查规则或审核标准**。

            【判断标准】：
            - 如果文档描述了"申请条件"、"审核要求"、"评定标准"、"必须满足的条件"、"合规要求"等可用于**核对材料是否合格**的规则 → 回复 YES
            - 如果文档只是"通知"、"公告"、"温馨提示"、"会议记录"、"新闻稿"等纯信息性内容，没有审核标准 → 回复 NO

            【文档标题】：{filename}
            【文档内容】：
            {content}

            只输出 YES 或 NO，不要任何解释。
            """


def domain_identify_prompt(filename: str, head: str, known_domains: list) -> str:
    return f"""请判断以下文档属于哪个业务领域。

            【已知领域】：{', '.join(known_domains)}

            【判断规则】：
            1. 必须优先复用上述已知领域的**原始名称**，不得同义改写、增删“管理/申请/评定”等后缀。
            2. 只有确实不属于任何已知领域时，才创建一个新的、简洁的领域名称（4-8个字，如"社团管理"、"实验室安全"）。
            3. 只输出领域名称，不要任何解释。

            【文档标题】：{filename}
            【文档开头】：{head}

            领域："""


# ============================================================
# 五、材料审查
# ============================================================

def compact_review_content(content: str, max_chars: int = None) -> str:
    """压缩审查模型输入，并在超长时保留材料首尾。

    完整原文仍由硬规则引擎使用；这里只控制远程模型的提示词体积。
    """
    text = str(content or "").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    limit = config.REVIEW_CONTENT_MAX_CHARS if max_chars is None else max_chars
    if limit <= 0 or len(text) <= limit:
        return text

    marker = "\n\n【中间内容过长，已省略；硬规则仍会检查完整原文】\n\n"
    available = max(0, limit - len(marker))
    head_chars = int(available * 0.7)
    tail_chars = available - head_chars
    return text[:head_chars] + marker + (text[-tail_chars:] if tail_chars else "")

def review_identify_prompt(content: str, known_domains: list, domain: dict = None) -> str:
    """识别待审材料的领域与子类型。子类型枚举来自领域配置。"""
    domain = domain or load_domain()
    scenes = domain.get("review_scenes") or {}
    scene_lines = "\n".join(f"           - \"{k}\"：{v}" for k, v in scenes.items()) \
        or "           - \"通用\"：无法判断的"

    return f"""请分析以下待审材料，输出结构化识别结果。

        【材料内容】：
        {content[:1000]}

        【已知领域】：{known_domains}

        【识别任务】：
        1. domain：判断材料属于哪个业务领域（从已知领域选，或新建一个简洁领域名）
        2. scene：判断材料的**具体子类型**（从下面选一个）：
{scene_lines}
        3. key_fields：从材料中提取 3-5 个关键字段（如人名、日期、学号）

        【输出格式】（只输出合法 JSON）：
        {{"domain": "团员推优", "scene": "票根", "key_fields": ["姓名", "赞成票数", "日期"]}}
        """


def fact_extract_prompt(content: str, schema_str: str) -> str:
    content = compact_review_content(content)
    return f"""请从以下材料中抽取结构化事实。
        必须直接输出一个合法 JSON 对象：第一个字符必须是 {{，最后一个字符必须是 }}。
        不要输出分析过程、Markdown 代码围栏或任何解释。
        如果某个字段在材料中找不到，请填写空字符串 ""。
        只允许输出给定字段，且每个字段只能出现一次。
        每个值必须是一个短文本或数字，最多 240 个字符；不要复制段落、表格、填写说明或整篇材料。
        同一字段出现多次时，选择材料明确填写的最终值，不要把多处原文拼接在一起。

        【材料】：
        {content}

        【输出格式】：
        {schema_str}
        """


def soft_review_prompt(content: str) -> str:
    content = compact_review_content(content)
    return f"""你是一个高校行政材料审查员。请对以下材料进行软性审查，只关注表述规范性和逻辑一致性。

【材料】：
{content}

【重要提醒】：
- 请严格区分"严重问题"和"建议性优化"。
- 如果只是"排版可以更好""措辞可以更规范"这种小瑕疵，不要列为"问题"，可以放在"建议"里。
- 满分 100 分对应"完全可以直接使用"，80 分对应"小修即可"，60 分对应"需要明显修改"。
- 请以"这份材料能不能直接提交"为标准评分，而不是"它能不能变得更好"。
- 每个问题和修改建议最多列 5 条，每条用一句可直接执行的话说明，避免复述材料全文。
- 不要输出分析过程、Markdown 或代码围栏；第一个字符必须是 {{，最后一个字符必须是 }}。

【输出格式】（只输出合法 JSON）：
{{
    "表述规范性": {{"评分": <0-100>, "问题": [], "修改建议": []}},
    "逻辑一致性": {{"评分": <0-100>, "问题": [], "修改建议": []}},
    "遗漏项": [],
    "遗漏项修改建议": []
}}
"""


# 规则抽取提示词：JSON 示例太多，用占位符替换而不是 f-string，避免大括号转义噪音
_RULE_SCHEMA_TPL = """你是高校行政规章制度分析专家。请从以下文档中抽取可执行的结构化审查规则。

    【文档标题】：__TITLE__
    【文档领域】：__DOMAIN__
    【文档正文】：
    __CONTENT__

    【规则 Schema（强制）】：
    每条规则必须包含以下字段；除 condition 明确标注可选外，其余字段缺一不可：

    1. id：唯一标识，格式 "__DOMAIN___R001"
    2. name：规则名（10-20字，说明约束什么）
    3. type：校验类型，只能是 required / regex / range / keyword / forbidden_keyword / enum / status 之一
       - keyword：材料必须包含至少一个关键词
       - forbidden_keyword：材料不得包含任何一个关键词（如不得残留“模板”“备注”）
       - status：资格状态三态判断，必须同时给出 pass_values 和 fail_values；材料未提及该状态时待核验
    4. field：被校验的字段名（如"学号"、"日期"、"签名"）
    5. severity：严重程度，必须是以下三档之一：
    - "critical"：致命问题（缺签名、缺盖章、票数不够、资格审查不过），扣 30 分
    - "error"：严重问题（格式错误、信息不全、逻辑不一致），扣 10 分
    - "warning"：建议问题（措辞不规范、排版不美观），扣 3 分
    6. message：未通过时的提示
    7. scene：⭐ 适用材料类型（数组，可多选）
    可选值：__SCENES__
    8. condition：（可选）规则适用的额外条件（如"仅大一学生"）
    9. source_excerpt：该规则对应的制度原文，必须逐字摘录 20-120 字，禁止概括或编造

    【scene 判断指引（关键）】：
    请仔细阅读文档，判断每条规则约束的是哪种材料的哪个字段：
    - 提到"票根"、"选票"、"监票人"、"唱票" → scene: ["票根"] 或 ["选票"]
    - 提到"申请表"、"申请人"、"个人简历" → scene: ["申请表"]
    - 提到"资格审查"、"入团满一年"、"无挂科" → scene: ["资格审查"]
    - 提到"黑板"、"会议记录"、"发言时长" → scene: ["会议记录"]
    - 提到"通知"、"公告"、"时间安排" → scene: ["通知公告"]
    - 适用于所有材料的（如日期格式） → scene: ["通用"]

    【输出格式】（只输出合法 JSON 对象，不要任何解释）：
    {
      "rules": [
        {
            "id": "__DOMAIN___R001",
            "name": "票根必须有监票人签名",
            "type": "keyword",
            "field": "正文",
            "keywords": ["监票人", "签名"],
            "severity": "error",
            "message": "票根缺少监票人签名",
            "scene": ["票根"],
            "source_excerpt": "票根应由监票人签名确认，并随推优材料一并提交。"
        }
      ]
    }

    【最终要求】：
    - scene 必须是非空数组，绝对不能省略！
    - source_excerpt 必须能在文档正文中逐字找到；找不到明确原文依据的内容不要抽成规则。
    - 先分清“约束谁”，再决定 keyword 还是 forbidden_keyword：
      · 约束**材料本身**不得出现某内容 → forbidden_keyword（如“票根不得残留模版备注”）
      · 要求**通知/制度类文档必须写明**某条规定 → keyword，且 scene 必须是“通知公告”
        （不要写成“资格审查”：资格审查核验的是学生事实，不是制度条文）
    - ⚠️ keyword 的关键词必须是**待审材料里真会出现的用语**，要短、要像材料中的原话。
      只存在于制度/细则里的公式或条款描述（如权重公式“D×8%”“X×65%”），对材料永远不成立，
      不要写成关键词规则；这类“约束评比口径本身”的规则请直接省略，引擎会按规则名判为不适用。
    - “无挂科、无违纪方可参评”等资格状态必须使用 status，并配置明确的正反状态词；
      不能用 keyword 或 required，避免把“材料没写”误判成“存在不合格事实”。
    - required 表示“材料必须提供该字段，缺失即不合格”，只能用于确实能被抽取出来的字段；
      规则名含“不得 / 禁止 / 严禁”的禁令不要用 required（字段抽不到就会把合格材料判成不合格），
      应改用 status（三态）或 forbidden_keyword。
    - range 的 min/max 只能填写数值或由材料字段组成的简单四则公式；无法结构化时不要伪造可执行规则。
    - 如果一条规则适用于多种材料，写多个 scene。
    - 至少抽 5 条规则，最多 50 条，避免冗余。
    """


def extract_rules_prompt(doc_content: str, doc_title: str, domain: str, domain_cfg: dict = None) -> str:
    scenes = scene_enum(domain_cfg)
    return (_RULE_SCHEMA_TPL
            .replace("__TITLE__", str(doc_title))
            .replace("__DOMAIN__", str(domain))
            .replace("__CONTENT__", str(doc_content)[:6000])
            .replace("__SCENES__", "、".join(f'"{s}"' for s in scenes)))
