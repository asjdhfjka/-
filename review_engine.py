"""材料审查的纯逻辑层 —— 规则路由、硬规则校验、评分、名称归一。

为什么单独成文件：
    这三件事原本长在 main.py 里，而 main.py 在 import 阶段就会加载向量库、
    嵌入模型、精排模型和 OCR 引擎（合计数 GB、数十秒）。结果就是
    「想给规则引擎写个单元测试」得先把整个服务栈拖起来，于是没人写测试，
    逻辑缺陷长期隐身（比如 critical 严重度被漏掉、enum 反向包含误判）。

本模块只依赖标准库与 config：
    · 不 import 任何模型、向量库、Web 框架
    · 不发起任何网络请求
    · 毫秒级可跑完，适合放进 pytest
"""

import ast
import math
import re
from pathlib import Path
from urllib.parse import quote

import config


def build_source_refs(source_docs, metadatas, data_dir):
    """把规则中的原文件名解析为可预览的实际归档文件。

    新上传文件以 ``原名__内容哈希.ext`` 保存；规则库仍保留便于阅读的原名。
    优先使用向量库元数据关联，旧数据缺少元数据时再扫描 data 目录兜底。
    """
    root = Path(data_dir)
    metadata_rows = [row for row in (metadatas or []) if isinstance(row, dict)]
    refs = []
    seen = set()

    for raw_title in source_docs or []:
        title = Path(str(raw_title)).name.strip()
        if not title or title in seen:
            continue
        seen.add(title)

        matches = [row for row in metadata_rows if title in {
            Path(str(row.get("original_filename") or "")).name,
            Path(str(row.get("source_title") or "")).name,
            Path(str(row.get("source_file") or "")).name,
        }]
        matches.sort(key=lambda row: str(row.get("uploaded_at") or ""), reverse=True)

        stored = ""
        for row in matches:
            candidate = Path(str(row.get("source_file") or "")).name
            if candidate and (root / candidate).is_file():
                stored = candidate
                break

        if not stored and (root / title).is_file():
            stored = title

        if not stored and root.is_dir():
            original = Path(title)
            prefix = f"{original.stem}__"
            candidates = [
                path for path in root.iterdir()
                if path.is_file()
                and path.stem.startswith(prefix)
                and path.suffix.lower() == original.suffix.lower()
            ]
            if candidates:
                stored = max(candidates, key=lambda path: path.stat().st_mtime).name

        available = bool(stored)
        refs.append({
            "title": title,
            "stored_file": stored,
            "url": f"/preview/{quote(stored, safe='')}" if available else "",
            "available": available,
        })

    return refs

# ============================================================
# 常量
# ============================================================

# 严重度排序：critical 最前。
# 【修复】原实现是 {"error": 0, "warning": 1, "info": 2}，漏了 critical，
# 导致 payload 里写着 critical 的致命规则在排序时落到默认值 1，
# 与 warning 同级 —— 致命问题排到了建议问题后面。
SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}

# 扣分权重（与提示词里写给模型的口径一致）
DEDUCTION = {"critical": 30, "error": 10, "warning": 3}

# 这些不是业务领域名，命中它们说明「没有可用的领域规则」，只走全局规则
NON_BUSINESS_DOMAINS = {"通用审查", "通用", "其他", "未知", ""}

# 场景枚举里表示「不挑材料」的取值
GENERIC_SCENES = {"通用", ""}

# 元规则：约束的是「评比规则本身」而不是材料内容，无法从材料校验 → 判不适用。
# 这些规则如果真按「材料里没有这个词」来判，会把完全合格的材料扣成不合格。
META_KEYWORDS = [
    "公式", "计算方式", "计算规则", "按首次", "定义", "口径",
    "只计", "只取", "取最高", "计最高", "按50%", "按比例",
    "兼任", "重复", "同一项目", "同一学年", "累计",
]


# ============================================================
# 一、名称归一
# ============================================================

def _squash(s) -> str:
    """去掉所有空白，便于做名称比较。"""
    return re.sub(r"\s+", "", str(s or ""))


def normalize_domain(name, available, alias_map=None) -> str:
    """把「两次 LLM 识别可能不一致的领域名」归一到规则库里的键。

    背景：auto_rules.json 的领域名是**上传规章文档时**识别的，审查材料时
    会**再识别一次**。两次结果常常只差一点（"团员推优" vs "推优"、
    "奖学金评定" vs "奖学金申请"）。原实现用 `in` 精确判断，差一个字就
    静默退化成「只用全局规则」，还不报错。

    归一顺序：精确 → 别名表 → 去空白后相等 → 互含 → 原样返回。
    """
    raw = str(name or "").strip()
    if not raw:
        return raw

    avail = [str(a) for a in (available or [])]
    if raw in avail:
        return raw

    if alias_map:
        if raw in alias_map:
            return str(alias_map[raw])
        for alias, canonical in alias_map.items():
            if _squash(alias) == _squash(raw):
                return str(canonical)

    target = _squash(raw)
    for a in avail:
        if _squash(a) == target:
            return a
    for a in avail:
        sa = _squash(a)
        if sa and (sa in target or target in sa):
            return a
    return raw


def domain_catalog(domain_cfg=None, all_rules=None) -> list:
    """返回运行时统一领域目录：配置领域在前，动态规则领域随后。"""
    configured = list(((domain_cfg or {}).get("domains") or {}).keys())
    generated = list(((all_rules or {}).get("domains") or {}).keys())
    result = []
    seen = set()
    for name in configured + generated:
        clean = str(name or "").strip()
        key = _squash(clean)
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def normalize_import_domain(name, domain_cfg=None, all_rules=None) -> str:
    """将规则文档识别出的领域归入统一目录。

    导入时别名优先于模型原文，随后再对配置领域和已有规则领域做名称归一；
    只有确实匹配不到时才保留为新领域。
    """
    raw = str(name or "").strip()
    aliases = (domain_cfg or {}).get("domain_aliases") or {}
    mapped = raw
    if raw in aliases:
        mapped = str(aliases[raw]).strip()
    else:
        for alias, canonical in aliases.items():
            if _squash(alias) == _squash(raw):
                mapped = str(canonical).strip()
                break
    return normalize_domain(mapped, domain_catalog(domain_cfg, all_rules))


def domain_alignment_report(domain_cfg=None, all_rules=None) -> dict:
    """检查静态领域配置、动态规则桶、别名和兜底映射的一致性。"""
    configured = set(((domain_cfg or {}).get("domains") or {}).keys())
    rule_domains = set(((all_rules or {}).get("domains") or {}).keys())
    aliases = (domain_cfg or {}).get("domain_aliases") or {}
    fallbacks = (domain_cfg or {}).get("rule_domain_fallbacks") or {}
    known = configured | rule_domains

    bad_aliases = {
        str(alias): str(target)
        for alias, target in aliases.items()
        if str(target) not in known
    }
    bad_fallbacks = {}
    for source, targets in fallbacks.items():
        values = targets if isinstance(targets, list) else [targets]
        missing = [str(target) for target in values if str(target) not in rule_domains]
        if missing:
            bad_fallbacks[str(source)] = missing

    return {
        "catalog": domain_catalog(domain_cfg, all_rules),
        "configured_without_rules": sorted(configured - rule_domains),
        "rules_without_static_config": sorted(rule_domains - configured),
        "aliases_with_missing_target": bad_aliases,
        "fallbacks_with_missing_rule_target": bad_fallbacks,
    }


def scene_matches(matched_scene, rule_scenes) -> bool:
    """场景匹配：精确 → 互含。

    【修复】规则库里的 scene 常写成 "资格审查"，而 domains/*.yaml 的
    review_scenes 枚举写的是 "资格审查表"，精确匹配永远不中，
    规则被整批丢掉。这里允许「互含」兜住这种差一个字的枚举漂移。
    """
    if isinstance(rule_scenes, str):
        rule_scenes = [rule_scenes]
    scenes = [str(s).strip() for s in (rule_scenes or []) if str(s).strip()]
    if not scenes:
        return False

    ms = str(matched_scene or "").strip()
    if not ms or ms in GENERIC_SCENES:
        return False

    if ms in scenes:
        return True
    key = _squash(ms)
    for s in scenes:
        ss = _squash(s)
        if ss and (ss in key or key in ss):
            return True
    return False


# ============================================================
# 二、规则路由（三级优先级）
# ============================================================

def route_rules(matched_domain, matched_scene, all_rules, domain_cfg=None):
    """按 领域+场景精准 → 领域+通用 → 全局 三级优先级挑规则。

    返回 (规则列表, 归一后的领域名)，第二个值是给调用方打日志/回显用的，
    避免「识别成 A、实际用了 B 的规则」这种不可解释的情况。

    三级优先级：
        1. 领域 + scene 精准匹配 → 优先
        2. 领域 + 通用 scene    → 次之
        3. global_rules         → 兜底
    """
    all_rules = all_rules or {}
    domains = all_rules.get("domains") or {}
    alias_map = (domain_cfg or {}).get("domain_aliases") or {}

    resolved = normalize_domain(matched_domain, domains.keys(), alias_map)
    if resolved not in domains:
        fallback_map = (domain_cfg or {}).get("rule_domain_fallbacks") or {}
        for candidate in fallback_map.get(str(matched_domain or "").strip(), []) or []:
            if candidate in domains:
                resolved = candidate
                break
    applicable = []

    is_specific = resolved and resolved not in NON_BUSINESS_DOMAINS and resolved in domains

    if is_specific:
        domain_bucket = domains.get(resolved) or {}
        domain_rules = domain_bucket.get("rules") or []
        source_docs = domain_bucket.get("source_docs") or []
        exact, general, no_scene = [], [], []

        for r in domain_rules:
            r = dict(r)
            r.setdefault("source_docs", source_docs)
            rules_scenes = r.get("scene", [])
            if isinstance(rules_scenes, str):
                rules_scenes = [rules_scenes]
            rules_scenes = [str(s).strip() for s in (rules_scenes or []) if str(s).strip()]

            # 兼容没有 scene 的旧规则
            if not rules_scenes:
                no_scene.append(r)
                continue

            if str(matched_scene or "").strip() not in GENERIC_SCENES:
                if scene_matches(matched_scene, rules_scenes):
                    exact.append(r)
                elif "通用" in rules_scenes:
                    general.append(r)
            else:
                # 材料类型判不出来 → 该领域规则全收
                general.append(r)

        if exact:
            applicable.extend(exact)
            applicable.extend(general)
        else:
            # 没有精准匹配 → 通用 + 无 scene 的旧规则
            applicable.extend(general)
            applicable.extend(no_scene)

    # 追加全局规则
    applicable.extend(all_rules.get("global_rules") or [])

    # 按严重度排序：critical → error → warning → info，同级按 priority
    applicable.sort(key=lambda r: (
        SEVERITY_ORDER.get(r.get("severity", "warning"), len(SEVERITY_ORDER)),
        r.get("priority", 99),
    ))

    limit = getattr(config, "MAX_APPLICABLE_RULES", 30)
    if len(applicable) > limit:
        applicable = applicable[:limit]

    return applicable, resolved


# ============================================================
# 三、硬规则校验
# ============================================================

def _extract_number(value):
    """从「入团年限 1 年」「绩点 3.5」这类文本里抠出第一个数字。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        m = re.search(r"-?\d+\.?\d*", str(value))
        return float(m.group()) if m else None


def enum_match(value, allowed) -> bool:
    """枚举校验：**只做正向包含**（合法值出现在抽取值里）。

    【修复】原实现是 `str(av) in str(value) or str(value) in str(av)`。
    反向那一半（抽取值包含于合法值）非常危险：当抽取值只有 1 个字，
    例如 "通"，而合法值是 "通知"，`"通" in "通知"` 成立 → 误判通过。
    这里去掉反向匹配，只保留「合法值出现在抽取值中」。
    """
    v = str(value or "").strip()
    if not v:
        return False
    for av in allowed or []:
        a = str(av).strip()
        if a and a in v:
            return True
    return False


def _rule_requirement(rule):
    rule_type = rule.get("type")
    if rule_type == "range":
        parts = []
        if rule.get("min") is not None:
            parts.append(f"不低于 {rule['min']}")
        if rule.get("max") is not None:
            parts.append(f"不高于 {rule['max']}")
        return "，".join(parts)
    if rule_type in {"forbidden_keyword", "not_keyword"} or _is_forbidden_keyword_rule(rule):
        return "不得包含：" + "、".join(map(str, rule.get("keywords") or []))
    if rule_type == "keyword":
        return "应包含：" + "、".join(map(str, rule.get("keywords") or []))
    if rule_type == "enum":
        return "允许值：" + "、".join(map(str, rule.get("values") or rule.get("allowed") or []))
    if rule_type == "status":
        return "通过状态：" + "、".join(map(str, rule.get("pass_values") or []))
    if rule_type == "regex":
        return f"格式要求：{rule.get('pattern', '')}"
    if rule_type == "required":
        return f"必须提供：{rule.get('field') or rule.get('name', '')}"
    return str(rule.get("name") or "")


def _fix_suggestion(rule, pending=False):
    """按规则类型生成稳定、可执行且不会诱导篡改事实的修改建议。"""
    explicit = str(rule.get("fix_suggestion") or "").strip()
    if explicit:
        return explicit

    rule_type = rule.get("type")
    field = str(rule.get("field") or rule.get("name") or "相关内容")
    if rule_type == "status":
        return (f"请补充能够证明“{field}”的官方记录或佐证材料，并由审核人员核验。"
                "资格事实应以真实记录为准，不要仅修改文字表述。")
    if pending:
        return f"请补充“{field}”的明确内容或佐证材料，再重新提交审查。"
    if rule_type == "required":
        return f"请在材料中补充“{field}”，并确保内容清晰、完整、可核验。"
    if rule_type == "regex":
        return f"请核对并统一“{field}”的填写格式，使其满足：{_rule_requirement(rule)}。"
    if rule_type == "range":
        return (f"请核对“{field}”及其证明材料，要求为：{_rule_requirement(rule)}。"
                "若实际数值不符合条件，应按制度处理，不要改动客观数据。")
    if rule_type == "keyword":
        return f"请补充能证明“{field}”的内容或附件，并明确体现：{_rule_requirement(rule)}。"
    if rule_type in {"forbidden_keyword", "not_keyword"} or _is_forbidden_keyword_rule(rule):
        return f"请删除“{field}”中的模板备注或禁用文字：{'、'.join(map(str, rule.get('keywords') or []))}。"
    if rule_type == "enum":
        return f"请核对“{field}”，并按允许值规范填写：{'、'.join(map(str, rule.get('values') or rule.get('allowed') or []))}。"
    return f"请根据规则要求核对并完善“{field}”，修改后重新提交审查。"


def _result(rule, passed, not_applicable, severity, message="", actual_value=None):
    if not_applicable:
        status = "not_applicable"
    else:
        status = "pass" if passed else "fail"
    return {
        "rule_id": rule.get("id", ""),
        "rule_name": rule.get("name", ""),
        "passed": bool(passed),
        "not_applicable": bool(not_applicable),
        "severity": severity,
        "status": status,
        "needs_review": False,
        "field": rule.get("field", ""),
        "actual_value": actual_value,
        "requirement": _rule_requirement(rule),
        "fix_suggestion": "" if passed or not_applicable else _fix_suggestion(rule),
        "source_docs": list(rule.get("source_docs") or []),
        "message": "" if passed or not_applicable else (message or rule.get("message", "规则未通过")),
    }


def _pending_result(rule, severity, message, actual_value=None):
    """规则适用，但当前材料或规则结构不足以自动判定。"""
    return {
        "rule_id": rule.get("id", ""),
        "rule_name": rule.get("name", ""),
        "passed": False,
        "not_applicable": False,
        "severity": severity,
        "status": "pending",
        "needs_review": True,
        "field": rule.get("field", ""),
        "actual_value": actual_value,
        "requirement": _rule_requirement(rule),
        "fix_suggestion": _fix_suggestion(rule, pending=True),
        "source_docs": list(rule.get("source_docs") or []),
        "message": message,
    }


def _keyword_content(facts, field):
    """关键词规则优先检查对应字段；字段缺失时才退回全文。"""
    value = facts.get(field)
    if value is not None and str(value).strip():
        return str(value)
    return str(facts.get("_full_text", ""))


def _is_forbidden_keyword_rule(rule) -> bool:
    mode = str(rule.get("match_mode") or "").lower()
    if mode in {"forbidden", "deny", "must_not_contain"}:
        return True
    if rule.get("type") in {"forbidden_keyword", "not_keyword"}:
        return True
    name = str(rule.get("name") or "")
    return any(token in name for token in ("不得", "禁止", "严禁", "不可包含", "不能包含", "不得残留"))


_FACT_ALIASES = {
    "班级团员数a": ("班级团员数", "应到团员数"),
    "班级团员数": ("班级团员数a", "应到团员数"),
}


def _fact_number(name, facts):
    candidates = (name,) + _FACT_ALIASES.get(name, ())
    for candidate in candidates:
        value = _extract_number(facts.get(candidate))
        if value is not None:
            return value
    return None


def _eval_formula_node(node, facts):
    if isinstance(node, ast.Expression):
        return _eval_formula_node(node.body, facts)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        value = _fact_number(node.id, facts)
        if value is None:
            raise KeyError(node.id)
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_formula_node(node.operand, facts)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = _eval_formula_node(node.left, facts)
        right = _eval_formula_node(node.right, facts)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left / right
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"floor", "ceil"} and len(node.args) == 1 and not node.keywords):
        value = _eval_formula_node(node.args[0], facts)
        return float(math.floor(value) if node.func.id == "floor" else math.ceil(value))
    raise ValueError("不支持的公式结构")


def _resolve_bound(bound, facts):
    """解析固定数值或受限的字段计算公式，不执行任意代码。"""
    if bound is None:
        return None, None
    if isinstance(bound, (int, float)):
        return float(bound), None
    raw = str(bound).strip().replace("×", "*").replace("÷", "/")
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw):
        return None, f"日期边界需要使用日期规则：{bound}"
    try:
        return float(raw), None
    except ValueError:
        pass
    try:
        tree = ast.parse(raw, mode="eval")
        return _eval_formula_node(tree, facts), None
    except KeyError as e:
        return None, f"缺少计算规则所需字段：{e.args[0]}"
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError):
        return None, f"规则中的计算式暂不支持自动执行：{bound}"


def formula_fields(rule) -> list:
    """返回 range 公式依赖的事实字段，供调用方构造抽取 schema。"""
    fields = []
    for bound in (rule.get("min"), rule.get("max")):
        if not isinstance(bound, str):
            continue
        try:
            tree = ast.parse(bound.replace("×", "*").replace("÷", "/"), mode="eval")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id not in {"floor", "ceil"} and node.id not in fields:
                fields.append(node.id)
    return fields


def _condition_evidenced(condition, facts) -> bool:
    """保守判断规则的适用条件是否在材料中有明确证据。"""
    if not condition:
        return True
    content = _squash(facts.get("_full_text", ""))
    if not content:
        return False
    for clause in re.split(r"[；;，,。]", str(condition)):
        anchor = _squash(clause)
        for token in ("仅", "适用于", "参加", "报考", "时", "后", "则需", "即可"):
            anchor = anchor.replace(token, "")
        # 复合说明无法安全化简时，继续寻找其中的业务关键词。
        if anchor and len(anchor) >= 2 and anchor in content:
            return True
        for keyword in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", anchor):
            if len(keyword) >= 2 and keyword in content:
                return True
    return False


def execute_hard_rules(facts, rules) -> list:
    """对事实清单执行硬规则校验，支持「不适用」状态。

    四种结果：pass / fail / pending / not_applicable。
    pending 表示规则适用但证据不足或规则暂不可执行，必须回显给用户核验。
    """
    facts = facts or {}
    results = []

    for rule in rules or []:
        field = rule.get("field")
        value = facts.get(field, "")
        severity = rule.get("severity", "warning")
        if severity not in DEDUCTION:
            severity = "warning"
        rule_type = rule.get("type")
        rule_name = rule.get("name", "")

        if (rule_type in {"keyword", "forbidden_keyword", "not_keyword"}
                and (value is None or not str(value).strip())
                and str(facts.get("_full_text", "")).strip()):
            value = facts["_full_text"]

        if facts.get("_extraction_failed") and rule_type not in {
                "keyword", "forbidden_keyword", "not_keyword"}:
            results.append(_pending_result(
                rule, severity, "事实抽取服务超时，无法自动核验本条规则，请稍后重试。",
                actual_value=value))
            continue

        # 元规则：约束评比规则本身，无法从材料校验 → 不适用。
        # 判定范围必须同时看 name 与 field：有些口径规则把约束对象写在字段里
        # （如 name="总积分权重计算正确" + field="总积分计算公式"），只看规则名会漏判，
        # 于是「材料里没写制度公式」被当成违规，产生必然误判。
        meta_blob = f"{rule_name}{rule.get('field') or ''}"
        if any(kw in meta_blob for kw in META_KEYWORDS):
            results.append(_result(rule, True, True, severity))
            continue

        condition = rule.get("condition")
        if condition and not _condition_evidenced(condition, facts):
            results.append(_pending_result(
                rule, severity, f"无法确认适用条件“{condition}”是否成立，本条需人工核验。",
                actual_value=value))
            continue

        passed, not_applicable = True, False

        if value is None or str(value).strip() == "":
            # 状态类规则缺证据只能待核验；required 才能把字段缺失判为失败。
            if rule_type == "status":
                results.append(_pending_result(
                    rule, severity, f"材料中没有“{field or rule_name}”的明确证明，请补充成绩单或人工核验。",
                    value))
                continue
            if rule_type == "required":
                passed = False
            else:
                results.append(_pending_result(
                    rule, severity, f"材料中未识别到“{field or rule_name}”，请补充或人工核对。",
                    actual_value=value))
                continue
        elif rule_type == "required":
            passed = bool(str(value).strip())
        elif rule_type == "regex":
            try:
                passed = bool(re.match(rule["pattern"], str(value)))
            except (re.error, KeyError):
                results.append(_pending_result(rule, severity, "规则中的格式表达式有误，请管理员检查规则。", value))
                continue
        elif rule_type in {"keyword", "forbidden_keyword", "not_keyword"}:
            is_forbidden = _is_forbidden_keyword_rule(rule)
            full_text = str(facts.get("_full_text", ""))
            # 禁止词必须检查原文全文，不能相信可能遗漏敏感词的模型摘要字段。
            content = full_text if is_forbidden and full_text.strip() else _keyword_content(facts, field)
            keywords = rule.get("keywords") or []
            if not keywords:
                results.append(_pending_result(rule, severity, "规则未配置关键词，请管理员检查规则。", value))
                continue
            else:
                found = any(str(kw) in content for kw in keywords if str(kw))
                passed = not found if is_forbidden else found
        elif rule_type == "range":
            num = _extract_number(value)
            lo, lo_error = _resolve_bound(rule.get("min"), facts)
            hi, hi_error = _resolve_bound(rule.get("max"), facts)
            bound_error = lo_error or hi_error
            if bound_error:
                results.append(_pending_result(rule, severity, f"{bound_error}，本条需人工核验。", value))
                continue
            if num is None or (lo is None and hi is None):
                results.append(_pending_result(rule, severity, "未能识别待比较的数值或规则上下限，本条需人工核验。", value))
                continue
            else:
                passed = True
                if lo is not None:
                    passed = passed and num >= lo
                if hi is not None:
                    passed = passed and num <= hi
        elif rule_type == "enum":
            allowed = rule.get("values") or rule.get("allowed") or []
            if not allowed:
                passed, not_applicable = True, True
            else:
                passed = enum_match(value, allowed)
        elif rule_type == "status":
            # 资格状态必须做三态判断：明确正向证据才通过，明确负向证据才失败，
            # 材料没有提及则待核验，不能把“没写无挂科”推断成“存在挂科”。
            pass_values = [str(v) for v in (rule.get("pass_values") or []) if str(v)]
            fail_values = [str(v) for v in (rule.get("fail_values") or []) if str(v)]
            if not pass_values or not fail_values:
                results.append(_pending_result(
                    rule, severity, "状态规则未完整配置通过值和不通过值，请管理员检查规则。", value))
                continue
            status_text = str(value or "").strip()
            if not status_text:
                results.append(_pending_result(
                    rule, severity, f"材料中没有“{field or rule_name}”的明确证明，请补充成绩单或人工核验。",
                    value))
                continue
            if any(item in status_text for item in pass_values):
                passed = True
            elif any(item in status_text for item in fail_values):
                passed = False
            else:
                results.append(_pending_result(
                    rule, severity, f"“{field or rule_name}”的识别结果无法明确判定，请人工核验。", value))
                continue
        else:
            results.append(_pending_result(rule, severity, f"暂不支持规则类型：{rule_type or '未填写'}。", value))
            continue

        results.append(_result(rule, passed, not_applicable, severity, actual_value=value))

    return results


# ============================================================
# 四、综合评分
# ============================================================

def _clamp(x, lo=0.0, hi=100.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def compute_score(hard_results, soft_result):
    """计算材料质量分，并给出不受软分抵消的审查结论。

    软分会限制在 0-100，但只用于展示材料表达质量。最终结论由硬规则状态决定：
    致命失败不会被软分抵消；证据不足返回待核验；没有完成硬规则核验返回无法审查。
    """
    soft_result = soft_result or {}

    pending = [r for r in (hard_results or []) if r.get("needs_review") or r.get("status") == "pending"]
    applicable = [r for r in (hard_results or [])
                  if not r.get("not_applicable") and r not in pending]

    counts = {"critical": 0, "error": 0, "warning": 0}
    deduction = 0
    if applicable:
        for r in applicable:
            if r.get("passed"):
                continue
            sev = r.get("severity", "warning")
            if sev not in counts:
                sev = "warning"
            counts[sev] += 1
            deduction += DEDUCTION[sev]
        hard_score = max(0, 100 - deduction)
    else:
        hard_score = None

    soft_parts = []
    for key in ("表述规范性", "逻辑一致性"):
        s = _clamp((soft_result.get(key) or {}).get("评分"))
        if s is not None:
            soft_parts.append(s)
    soft_score = int(sum(soft_parts) / len(soft_parts)) if soft_parts else None

    if hard_score is None and soft_score is None:
        total = None
    elif hard_score is None:
        total = soft_score   # 无适用硬规则，只用软审查定级
    elif soft_score is None:
        total = hard_score
    else:
        total = hard_score * 0.6 + soft_score * 0.4
    total = int(_clamp(total)) if total is not None else None

    critical_failed = counts["critical"] > 0
    error_failed = counts["error"] > 0
    if critical_failed:
        grade = "未通过"
    elif error_failed:
        grade = "需修改"
    elif pending:
        grade = "待核验"
    elif not applicable:
        grade = "无法审查"
    else:
        grade = "通过"

    return total, grade, {
        "hard_score": hard_score,
        "soft_score": soft_score,
        "deduction": deduction,
        "has_hard_rules": hard_score is not None,
        "outcome": grade,
        "pending": len(pending),
        "checked": len(applicable),
        **counts,
    }
