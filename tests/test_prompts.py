"""prompts / 领域配置单测。

焦点：
    1. CRAG 批量判定这个新增逻辑（README 的卖点之一）一直没有测试覆盖；
    2. 「校规外置」是否真的成立 —— 代码不认识任何一所学校，
       领域清单、别名、咨询部门都从 domains/*.yaml 来，而不是写死在 py 里。
"""

import pytest

import prompts


# ============================================================
# CRAG 批量判定
# ============================================================

def test_crag_batch_prompt_includes_all_docs():
    prompt = prompts.crag_batch_prompt("问题", ["甲", "乙", "丙"])
    assert "3 段资料" in prompt
    assert "【资料1】" in prompt and "【资料3】" in prompt


def test_parse_crag_batch_basic():
    verdicts = prompts.parse_crag_batch("1:YES\n2:NO\n3:YES", 3)
    assert verdicts == [True, False, True]


def test_parse_crag_batch_defaults_to_true():
    """解析不到的条目默认「相关」—— 与「失败默认保留」的口径一致。"""
    assert prompts.parse_crag_batch("垃圾输出", 3) == [True, True, True]
    assert prompts.parse_crag_batch("1:NO", 3) == [False, True, True]


def test_parse_crag_batch_tolerates_formats():
    raw = "资料1：是\n资料 2：不相关\n3.NO\n4) YES"
    assert prompts.parse_crag_batch(raw, 4) == [True, False, False, True]


def test_parse_crag_batch_ignores_out_of_range():
    assert prompts.parse_crag_batch("9:NO", 2) == [True, True]


def test_strip_fence_removes_json_fence():
    from llm_utils import strip_fence
    assert strip_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert strip_fence('```\n[1,2]\n```') == '[1,2]'
    assert strip_fence('{"a":1}') == '{"a":1}'
    # 正文里的反引号不能被误伤
    assert strip_fence('他说 `这样` 写') == '他说 `这样` 写'


def test_parse_json_object_accepts_reasoning_text_around_json():
    from llm_utils import parse_json_object
    raw = '分析如下：最终结果为 {"姓名":"张三","分数":{"值":90}} 请查收。'
    assert parse_json_object(raw) == {"姓名": "张三", "分数": {"值": 90}}


def test_parse_json_object_rejects_empty_or_non_json_text():
    from llm_utils import parse_json_object
    with pytest.raises(ValueError, match="为空"):
        parse_json_object("")
    with pytest.raises(ValueError, match="没有可解析"):
        parse_json_object("只有解释，没有结果")


def test_rule_extract_prompt_requires_reviewable_source_evidence():
    prompt = prompts.extract_rules_prompt("制度原文", "制度.pdf", "团员推优")
    assert '"rules"' in prompt
    assert "source_excerpt" in prompt
    assert "逐字找到" in prompt


def test_validate_soft_review_accepts_and_normalizes_complete_schema():
    from llm_utils import validate_soft_review
    raw = {
        "表述规范性": {"评分": "88", "问题": ["语句过长"], "修改建议": ["拆分长句"]},
        "逻辑一致性": {"评分": 92.5, "问题": [], "修改建议": []},
        "遗漏项": ["落款"],
        "遗漏项修改建议": ["补充落款"],
    }
    result = validate_soft_review(raw)
    assert result["表述规范性"]["评分"] == 88
    assert result["逻辑一致性"]["评分"] == 92.5
    assert result["遗漏项"] == ["落款"]


@pytest.mark.parametrize("raw,error", [
    ({"表述规范性": {"评分": 80}}, "逻辑一致性"),
    ({"表述规范性": {"评分": "很好"}, "逻辑一致性": {"评分": 90}}, "数值评分"),
    ({"表述规范性": {"评分": 101}, "逻辑一致性": {"评分": 90}}, "超出"),
])
def test_validate_soft_review_rejects_incomplete_or_invalid_schema(raw, error):
    from llm_utils import validate_soft_review
    with pytest.raises(ValueError, match=error):
        validate_soft_review(raw)


def test_fact_json_schema_requires_only_dynamic_short_string_fields():
    from llm_utils import build_fact_json_schema
    schema = build_fact_json_schema(["姓名", "日期", "姓名"])
    assert schema["required"] == ["姓名", "日期"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["姓名"] == {"type": "string", "maxLength": 240}


def test_validate_fact_extraction_fills_missing_removes_extra_and_compacts_lists():
    from llm_utils import validate_fact_extraction
    result = validate_fact_extraction(
        {"姓名": " 张三 ", "奖项": ["一等奖", "二等奖"], "模型解释": "不应保留"},
        ["姓名", "奖项", "日期"],
    )
    assert result == {"姓名": "张三", "奖项": "一等奖、二等奖", "日期": ""}


def test_validate_fact_extraction_limits_copied_paragraph_and_rejects_nested_object():
    from llm_utils import validate_fact_extraction
    assert len(validate_fact_extraction({"说明": "甲" * 500}, ["说明"])["说明"]) == 240
    with pytest.raises(ValueError, match="短文本或数字"):
        validate_fact_extraction({"姓名": {"值": "张三"}}, ["姓名"])


# ============================================================
# 领域配置（校规外置）
# ============================================================

def test_domain_names_come_from_yaml():
    names = prompts.domain_names(prompts.load_domain("gdut"))
    assert "团员推优" in names and "奖学金评定" in names
    assert len(names) >= 5


def test_no_school_name_hardcoded_in_code():
    """代码里不应再出现写死的学校领域表、文档名或示范人名。

    用 AST 取「真实代码」，而不是文本匹配：
      · 标识符（DOMAIN_TEMPLATES 之类的名字）直接查；
      · 字符串只查非 docstring 的那些 —— docstring 里提到 DOMAIN_TEMPLATES、
        提到原来写死的文档名，是在交代这次改造的来龙去脉，属于期望保留的历史说明。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    banned_in_strings = ("全国大学英语四、六级考试报名", "何华儒")

    for fname in ("main.py", "prompts.py", "review_engine.py"):
        tree = ast.parse((root / fname).read_text(encoding="utf-8"))

        docstring_ids = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstring_ids.add(id(body[0].value))

        identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        identifiers |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "DOMAIN_TEMPLATES" not in identifiers, f"{fname} 里还在使用硬编码的领域表"

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstring_ids:
                for banned in banned_in_strings:
                    assert banned not in node.value, (
                        f"{fname} 的代码字符串里还写死了：{banned}"
                    )


def test_scene_enum_from_yaml():
    scenes = prompts.scene_enum(prompts.load_domain("gdut"))
    assert "票根" in scenes and "通用" in scenes


def test_answer_prompt_uses_domain_config():
    domain = prompts.load_domain("gdut")
    prompt = prompts.answer_system_prompt("【来源1】示例资料", "2026年09月30日", domain)
    assert "广东工业大学" in prompt
    assert "广工" in prompt          # alias 现在真的被用上了
    assert "教务处" in prompt        # contact_hint 现在真的被用上了
    assert "【本校场景约束】" in prompt


def test_answer_prompt_has_no_scenario_block_when_empty():
    prompt = prompts.answer_system_prompt("ctx", "2026年09月30日", dict(prompts._MISSING_DOMAIN))
    assert "【本校场景约束】" not in prompt
    assert "【服务学校】：未配置" in prompt      # 领域缺失时回落到占位名
    assert "【求助出口】" not in prompt          # 没有 contact_hint 就不加求助规则


def test_missing_domain_does_not_raise():
    """领域配置缺失时应当降级运行，而不是让整个服务起不来。"""
    domain = prompts.load_domain("这所学校不存在")
    assert domain["name"] == "未配置"
    assert prompts.domain_names(domain) == []


def test_template_domain_is_valid():
    """模板文件必须能被解析，避免新学校接入时踩到同样的坑。"""
    domain = prompts.load_domain("_模板")
    assert domain["name"] and domain["domains"]
    assert domain["review_scenes"]


@pytest.mark.parametrize("code", ["gdut", "_模板"])
def test_all_shipped_domains_load(code):
    domain = prompts.load_domain(code)
    assert domain["name"] != "未配置"
    assert isinstance(domain.get("domain_aliases") or {}, dict)


# ============================================================
# 其余提示词
# ============================================================

def test_rewrite_prompt_mentions_current_year():
    prompt = prompts.rewrite_prompt("历史", "四级什么时候报名", "2026年09月")
    assert "2026年" in prompt


def test_extract_rules_prompt_includes_scenes():
    prompt = prompts.extract_rules_prompt("正文", "标题", "团员推优", prompts.load_domain("gdut"))
    assert "\"票根\"" in prompt
    assert "__SCENES__" not in prompt and "__TITLE__" not in prompt


def test_review_identify_prompt_uses_scene_enum():
    prompt = prompts.review_identify_prompt("材料", ["团员推优"], prompts.load_domain("gdut"))
    assert "票根" in prompt and "通用" in prompt


def test_compact_review_content_keeps_short_text_unchanged():
    assert prompts.compact_review_content("第一行\n第二行", 100) == "第一行\n第二行"


def test_compact_review_content_limits_long_text_and_keeps_both_ends():
    content = "开头事实" + "中" * 200 + "结尾签名"
    compacted = prompts.compact_review_content(content, 80)
    assert len(compacted) <= 80
    assert compacted.startswith("开头事实")
    assert compacted.endswith("结尾签名")
    assert "已省略" in compacted
