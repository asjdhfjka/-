"""规则草稿审核工作流的纯逻辑与持久化工具。"""

from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


RULE_TYPES = {
    "required", "regex", "range", "keyword", "forbidden_keyword", "enum", "status",
}
SEVERITIES = {"critical", "error", "warning"}
COMMON_FIELDS = ("id", "name", "type", "field", "severity", "message", "scene")
PUBLISHABLE_FIELDS = {
    *COMMON_FIELDS, "condition", "min", "max", "pattern", "keywords", "values", "allowed",
    "pass_values", "fail_values", "match_mode", "priority", "source_excerpt", "source_page",
}


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _norm(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _as_list(value) -> list:
    if isinstance(value, list):
        return [item for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [value]


def clean_rule(raw: dict) -> dict:
    """只保留正式规则允许的字段，防止审核接口写入任意内部状态。"""
    if not isinstance(raw, dict):
        raise ValueError("规则内容必须是对象")
    rule = {key: deepcopy(value) for key, value in raw.items() if key in PUBLISHABLE_FIELDS}
    for key in ("id", "name", "type", "field", "severity", "message", "condition",
                "pattern", "source_excerpt"):
        if key in rule:
            rule[key] = str(rule[key] or "").strip()
    rule["scene"] = [str(item).strip() for item in _as_list(rule.get("scene"))
                     if str(item).strip()]
    for key in ("keywords", "values", "allowed", "pass_values", "fail_values"):
        if key in rule:
            rule[key] = [str(item).strip() for item in _as_list(rule.get(key))
                         if str(item).strip()]
    return rule


def _find_excerpt(rule: dict, source_pages: list[dict]) -> tuple[str, int | None, bool]:
    requested = str(rule.get("source_excerpt") or "").strip()
    if requested:
        needle = _norm(requested)
        for page in source_pages:
            if needle and needle in _norm(page.get("text")):
                return requested[:300], page.get("page"), True

    signals = []
    for key in ("keywords", "pass_values", "fail_values", "values", "allowed"):
        signals.extend(str(item) for item in _as_list(rule.get(key)))
    signals.extend((str(rule.get("field") or ""), str(rule.get("name") or "")))
    signals = sorted({item.strip() for item in signals if len(item.strip()) >= 2},
                     key=len, reverse=True)

    for page in source_pages:
        text = str(page.get("text") or "")
        compact = _norm(text)
        for signal in signals:
            compact_signal = _norm(signal)
            if not compact_signal or compact_signal not in compact:
                continue
            # 原文坐标与去空白坐标不同，先尝试直接查找；失败时取页面开头作可核对证据。
            position = text.lower().find(signal.lower())
            if position < 0:
                position = 0
            start = max(0, position - 90)
            excerpt = text[start:position + len(signal) + 150].strip()
            return excerpt[:300], page.get("page"), bool(excerpt)
    return "", None, False


def _issue(code: str, message: str, blocking: bool = False) -> dict:
    return {"code": code, "message": message, "blocking": blocking}


def precheck_rule(rule: dict, existing_rules: list[dict], source_pages: list[dict]) -> list[dict]:
    """发布前预检；blocking=True 的问题必须修改后才能通过。"""
    issues = []
    for field in COMMON_FIELDS:
        value = rule.get(field)
        if value is None or value == "" or (field == "scene" and not _as_list(value)):
            issues.append(_issue(f"missing_{field}", f"缺少必填字段：{field}", True))

    rule_type = str(rule.get("type") or "")
    if rule_type and rule_type not in RULE_TYPES:
        issues.append(_issue("invalid_type", f"不支持的规则类型：{rule_type}", True))
    severity = str(rule.get("severity") or "")
    if severity and severity not in SEVERITIES:
        issues.append(_issue("invalid_severity", f"不支持的严重程度：{severity}", True))

    if rule_type in {"keyword", "forbidden_keyword"} and not _as_list(rule.get("keywords")):
        issues.append(_issue("missing_keywords", "关键词规则必须配置 keywords", True))
    if rule_type == "status":
        if not _as_list(rule.get("pass_values")) or not _as_list(rule.get("fail_values")):
            issues.append(_issue("incomplete_status", "状态规则必须同时配置通过值和不通过值", True))
    if rule_type == "range" and rule.get("min") in (None, "") and rule.get("max") in (None, ""):
        issues.append(_issue("missing_range", "范围规则至少要配置 min 或 max", True))
    if rule_type == "enum" and not (_as_list(rule.get("values")) or _as_list(rule.get("allowed"))):
        issues.append(_issue("missing_enum", "枚举规则必须配置允许值", True))
    if rule_type == "regex":
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            issues.append(_issue("missing_pattern", "正则规则必须配置 pattern", True))
        else:
            try:
                re.compile(pattern)
            except re.error:
                issues.append(_issue("invalid_pattern", "正则表达式无法编译", True))

    excerpt, page, verified = _find_excerpt(rule, source_pages)
    rule["source_excerpt"] = excerpt
    if page is not None:
        rule["source_page"] = page
    elif "source_page" in rule:
        rule.pop("source_page", None)
    if not verified:
        issues.append(_issue("missing_evidence", "没有找到可核对的制度原文依据", True))

    scenes = set(map(str, _as_list(rule.get("scene"))))
    for existing in existing_rules or []:
        if not isinstance(existing, dict):
            continue
        if rule.get("id") and rule.get("id") == existing.get("id"):
            issues.append(_issue("duplicate_id", f"规则编号已存在：{rule.get('id')}", True))
            break
        if _norm(rule.get("name")) and _norm(rule.get("name")) == _norm(existing.get("name")):
            issues.append(_issue("duplicate_name", f"正式库已有同名规则：{existing.get('name')}", True))
            break
        existing_scenes = set(map(str, _as_list(existing.get("scene"))))
        if (rule.get("field") and rule.get("field") == existing.get("field")
                and rule_type == existing.get("type")
                and (not scenes or not existing_scenes or scenes & existing_scenes)):
            issues.append(_issue(
                "possible_conflict",
                f"可能与现有规则“{existing.get('name', existing.get('id', '未命名'))}”约束同一字段，请确认边界。",
                False,
            ))
            break
    return issues


def assign_rule_ids(domain: str, rules: list[dict], existing_rules: list[dict]) -> list[dict]:
    """为候选规则分配不会与正式库冲突的稳定编号。"""
    prefix = f"{domain}_R"
    used = {str(rule.get("id")) for rule in existing_rules or [] if rule.get("id")}
    highest = 0
    for rule_id in used:
        match = re.fullmatch(re.escape(prefix) + r"(\d+)", rule_id)
        if match:
            highest = max(highest, int(match.group(1)))

    output = []
    for raw in rules or []:
        rule = clean_rule(raw)
        highest += 1
        candidate = f"{prefix}{highest:03d}"
        while candidate in used:
            highest += 1
            candidate = f"{prefix}{highest:03d}"
        rule["id"] = candidate
        used.add(candidate)
        output.append(rule)
    return output


def repair_conflicting_rule_ids(domain: str, rules: list[dict], existing_rules: list[dict]) -> list[dict]:
    """发布时只重排发生冲突的编号，处理多个草稿批次并行产生的编号碰撞。"""
    prefix = f"{domain}_R"
    used = {str(rule.get("id")) for rule in existing_rules or [] if rule.get("id")}
    highest = 0
    for rule in [*(existing_rules or []), *(rules or [])]:
        match = re.fullmatch(re.escape(prefix) + r"(\d+)", str(rule.get("id") or ""))
        if match:
            highest = max(highest, int(match.group(1)))

    output = []
    for raw in rules or []:
        rule = clean_rule(raw)
        candidate = str(rule.get("id") or "")
        if not candidate or candidate in used:
            highest += 1
            candidate = f"{prefix}{highest:03d}"
            while candidate in used:
                highest += 1
                candidate = f"{prefix}{highest:03d}"
            rule["id"] = candidate
        used.add(candidate)
        output.append(rule)
    return output


def create_draft_batch(domain: str, rules: list[dict], source_doc: str, source_file: str,
                       source_pages: list[dict], existing_rules: list[dict]) -> dict:
    prepared = assign_rule_ids(domain, rules, existing_rules)
    drafts = []
    peer_rules = list(existing_rules or [])
    for rule in prepared:
        issues = precheck_rule(rule, peer_rules, source_pages)
        drafts.append({
            "draft_id": uuid.uuid4().hex,
            "status": "pending",
            "rule": rule,
            "precheck": issues,
            "decision": None,
        })
        peer_rules.append(rule)
    return {
        "batch_id": uuid.uuid4().hex,
        "domain": domain,
        "source_doc": Path(source_doc).name,
        "source_file": Path(source_file).name,
        "source_url": f"/preview/{quote(Path(source_file).name, safe='')}",
        "created_at": utc_now(),
        "status": "pending_review",
        "source_pages": source_pages,
        "rules": drafts,
        "published_at": None,
    }


def refresh_batch_status(batch: dict) -> str:
    statuses = [item.get("status") for item in batch.get("rules") or []]
    if batch.get("published_at"):
        status = "published"
    elif statuses and all(status in {"approved", "rejected"} for status in statuses):
        status = "ready_to_publish"
    else:
        status = "pending_review"
    batch["status"] = status
    return status


def load_draft_store(path) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {"version": "1.0", "batches": [], "audit": []}
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("规则草稿库格式错误")
    data.setdefault("version", "1.0")
    data.setdefault("batches", [])
    data.setdefault("audit", [])
    return data


def save_draft_store(path, data: dict) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_name(file_path.name + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, file_path)


def public_batch(batch: dict) -> dict:
    """API 不返回整份制度正文快照，只返回审核所需字段。"""
    result = deepcopy(batch)
    result.pop("source_pages", None)
    return result
