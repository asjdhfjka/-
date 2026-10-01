"""与大模型交互的纯文本工具。

单独成文件的理由跟 review_engine 一样：这些函数是纯逻辑，但原来长在 main.py 里，
而 main.py 一 import 就要加载向量库和两个模型，导致它们无法被单测覆盖。
"""

import re
import json


def strip_fence(raw: str) -> str:
    """剥掉模型爱加的 ```json 围栏。

    注意不是简单 strip("`")：正文里出现的反引号也会被误伤。
    这里只处理「整个回复被围栏包住」这一种情况，并保留原有的
    ````json\\n` 前缀剥离行为以兼容历史调用。
    """
    text = str(raw or "").strip()
    if text.startswith("```"):
        # 去掉首行的 ``` 或 ```json
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        # 去掉结尾的 ```
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json_object(raw) -> dict:
    """从纯 JSON、Markdown 围栏或夹带说明的模型回复中提取 JSON 对象。"""
    text = strip_fence(raw)
    if not text:
        raise ValueError("模型返回内容为空")

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    # 推理模型偶尔会在 JSON 前后附带说明。用 raw_decode 从每个左大括号
    # 尝试解析，再选择跨度最大的对象，避免误取内部的嵌套小对象。
    decoder = json.JSONDecoder()
    candidates = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((end, index, value))
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    raise ValueError("模型返回内容中没有可解析的 JSON 对象")


def validate_soft_review(value: dict) -> dict:
    """校验并归一软审查结构，避免“解析成字典”被误报为审查成功。"""
    if not isinstance(value, dict):
        raise ValueError("软审查结果不是 JSON 对象")

    result = {}
    for section in ("表述规范性", "逻辑一致性"):
        block = value.get(section)
        if not isinstance(block, dict):
            raise ValueError(f"软审查缺少“{section}”对象")
        try:
            score = float(block.get("评分"))
        except (TypeError, ValueError):
            raise ValueError(f"软审查“{section}”缺少数值评分")
        if not 0 <= score <= 100:
            raise ValueError(f"软审查“{section}”评分超出 0-100")

        problems = block.get("问题") or []
        suggestions = block.get("修改建议") or []
        if not isinstance(problems, list) or not isinstance(suggestions, list):
            raise ValueError(f"软审查“{section}”的问题和修改建议必须是数组")
        result[section] = {
            "评分": int(score) if score.is_integer() else score,
            "问题": [str(item) for item in problems if str(item).strip()][:5],
            "修改建议": [str(item) for item in suggestions if str(item).strip()][:5],
        }

    for key in ("遗漏项", "遗漏项修改建议"):
        items = value.get(key) or []
        if not isinstance(items, list):
            raise ValueError(f"软审查“{key}”必须是数组")
        result[key] = [str(item) for item in items if str(item).strip()][:5]
    return result


def build_fact_json_schema(required_fields: list[str], max_value_chars: int = 240) -> dict:
    """按本次适用规则动态生成事实抽取 Schema。所有值用短字符串表达。"""
    fields = list(dict.fromkeys(str(field).strip() for field in required_fields if str(field).strip()))
    return {
        "type": "object",
        "properties": {
            field: {"type": "string", "maxLength": max_value_chars}
            for field in fields
        },
        "required": fields,
        "additionalProperties": False,
    }


def validate_fact_extraction(value: dict, required_fields: list[str],
                             max_value_chars: int = 240) -> dict:
    """归一事实清单，只保留规则需要的短标量，避免整段原文污染字段。"""
    if not isinstance(value, dict):
        raise ValueError("事实抽取结果不是 JSON 对象")

    result = {}
    for field in dict.fromkeys(str(item).strip() for item in required_fields if str(item).strip()):
        raw = value.get(field, "")
        if raw is None:
            text = ""
        elif isinstance(raw, (str, int, float, bool)):
            text = str(raw).strip()
        elif isinstance(raw, list) and all(
                isinstance(item, (str, int, float, bool)) for item in raw):
            text = "、".join(str(item).strip() for item in raw if str(item).strip())
        else:
            raise ValueError(f"事实字段“{field}”必须是短文本或数字")
        result[field] = text[:max_value_chars]
    return result
