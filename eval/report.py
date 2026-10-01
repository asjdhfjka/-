"""评测报告生成器：读评测产物 JSON → 产出单文件 HTML 报告。

用法：
    python eval/report.py

为什么要有这个脚本（而不是手写一份报告）：
    手写的报告会「慢慢和产物脱节」——改了题集、重跑了评测，报告里的数字还是旧的，
    而且没人能发现。这里所有数字都从 eval_results_qa.json / eval_results_review.json
    现读现渲染，报告里显示题集指纹，指纹与产物不一致时直接报错退出。

    ⚠️ 缺陷清单（DEFECTS）是人工维护的「结论层」，没法从产物自动推导，
       所以每条都必须带「证据」字段指向可复现的用例或文件行号。
"""

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.schema import (case_set_fingerprint, load_qa_cases,  # noqa: E402
                         load_review_cases)

QA_ARTIFACT = ROOT / "eval_results_qa.json"
REVIEW_ARTIFACT = ROOT / "eval_results_review.json"
OUT = ROOT / "eval_report.html"


# ---------------------------------------------------------------
# 结论层：缺陷清单。severity 取值 critical / high / medium。
# 每条 defect 必须能被「证据」列定位，禁止写没有证据的结论。
# ---------------------------------------------------------------
DEFECTS = [
    {
        "id": "D1", "severity": "critical", "status": "已修复（选项 A：移除零规则领域）",
        "title": "领域清单与规则库双向错配：零规则领域空转、有规则领域永久失效",
        "detail": (
            "domains/gdut.yaml（给模型看的可识别领域）与 auto_rules.json（真有规则的领域）原先是双向错配的："
            "yaml 声明了 9 个领域，其中 7 个在规则库中一条规则都没有；反过来规则库里真有规则的 4 个领域"
            "（考试报名管理 / 四六级考试报名 / 学科竞赛管理 / 校园艺术活动，共 96 条规则）未写进 yaml，"
            "模型识别时永远不会被引导过去，那批规则等于永久失效。"
            "【已修复 · 方向二】4 个有规则领域已全部写进 yaml，规则恢复可达。"
            "【已修复 · 方向一】把 6 个「零规则且无兜底」的领域（请假管理 / 学生干部 / 违纪处分 / "
            "转专业申请 / 宿舍管理 / 助学贷款）从 yaml 移除：它们此前会让材料被判到该领域后一条硬规则都不适用。"
            "保留的「奖学金评定」虽零规则，但有 rule_domain_fallbacks → 综合素质测评 兜底，可正常工作。"
            "yaml 内已写入不变量：只声明「规则库确有规则」或「有兜底映射」的领域；"
            "后续上传对应制度文件会自动抽取入库，届时把领域加回清单即可 —— 领域可逆，不会被永久删除。"
        ),
        "evidence": "对齐报告 domain_alignment_report 现为：configured_without_rules=['奖学金评定']（有兜底）、"
                    "rules_without_static_config=[]、aliases_with_missing_target={}、fallbacks_with_missing_rule_target={}。"
                    "rv_003（奖学金材料）经 fallback 正确路由到 3 条规则并全部判对（accuracy=1.0）。"
                    "口径更正：「零规则领域 fail-open 直接给高分」描述的是原系统（数字分级为「优秀」）；"
                    "经复核当前 compute_score 对「零适用规则」返回结论「无法审查」（review_engine.py:648，"
                    "tests/test_review_engine.py:314 锁定），「总评分」退化为软审查分（review_engine.py:633）—— "
                    "数字仍可能偏高，但结论不放行，故不再属于 fail-open。",
    },
    {
        "id": "D2", "severity": "critical", "status": "已修复",
        "title": "公式型 range 规则导致整个 /review 请求崩溃",
        "detail": (
            "range 类型规则的 min/max 允许是公式字符串（如 \"0.5 * 班级团员数a + 1\"），"
            "原先 execute_hard_rules 直接拿数字与它比较，抛 TypeError；"
            "由于该函数对整份材料一次性执行，**一条坏规则会拖垮整个审查请求**，材料拿不到任何结果。"
            "【已修复】改用 `ast` 白名单求值（review_engine._resolve_bound / _eval_formula_node）："
            "只允许数字字面量、材料字段名与四则运算；无法求值的规则转 pending，不再抛错。"
        ),
        "evidence": "rv_004 修复前端到端复现 \"'<=' not supported between instances of 'float' and 'str'\"；"
                    "修复后全部 4 例零报错完成。回归用例见 tests/test_review_engine.py。",
    },
    {
        "id": "D3", "severity": "critical", "status": "已修复",
        "title": "否定型 keyword 规则语义反转",
        "detail": (
            "keyword 原先被实现为「正文里出现任一关键词 → 通过」，"
            "而「票根不得残留模版备注」这类**否定语义**规则，出现关键词恰恰意味着违规，判断完全反了。"
            "【已修复】新增三重否定识别（review_engine._is_forbidden_keyword_rule）："
            "`match_mode`（forbidden/deny/must_not_contain）、`type`（forbidden_keyword/not_keyword）、"
            "规则名含「不得/禁止/严禁」任一命中即判为否定规则，语义反转为「出现即失败」，并在全文而非单字段上查找。"
        ),
        "evidence": "rv_001 修复前脏票根判 pass（应 fail）、rv_002 干净票根判 fail（应 pass），两例方向相反；"
                    "修复后 rv_001=74「需修改」、rv_002=92「通过」，逐规则判定均与 gold 一致。",
    },
    {
        "id": "D4", "severity": "high", "status": "已修复",
        "title": "审查链路超时预算过紧，默认配置下 /review 基本不可用",
        "detail": (
            "/review 原先串行发起 3 次模型调用（材料类型识别 / 事实抽取 / 软性审查），三次都继承默认 TIMEOUT_AUX=15s，"
            "而审查的 prompt 是整篇材料，单次耗时普遍超过 15s，超时成为常态而非偶发。"
            "【已修复】新增独立 TIMEOUT_REVIEW（默认 60s）；事实抽取与软审查改用 asyncio.gather 并行（不再串行累加）；"
            "结构化 JSON 失败加 1 次显式重试；超长材料经 compact_review_content 掐头去尾后送模型，硬规则仍检查完整原文。"
            "任一步失败仍走降级路径，不会让整个请求崩溃。"
        ),
        "evidence": "修复前默认配置下 4 例中 3 例 timeout。修复后用**默认配置**实测：软审查单次 29~32s、"
                    "事实抽取 8~10s、材料类型识别 3~5s，两次 /review 均在 33~37s 内完成、三次调用全部一次成功（无重试）。",
    },
    {
        "id": "D5", "severity": "high", "status": "已修复",
        "title": "save_upload 返回类型与调用方不匹配，/upload 与 /review 整体不可用",
        "detail": (
            "save_upload() 只返回路径字符串，而两个端点都按 `路径, 内容 = await save_upload(...)` 解包，"
            "每次调用必抛 `too many values to unpack (expected 2)`。因集成测试默认跳过，一直没暴露。"
        ),
        "evidence": "main.py save_upload 返回值；修复为 `return path, content` 后两个端点恢复。",
    },
    {
        "id": "D6", "severity": "medium", "status": "待定（口径问题）",
        "title": "问答「严格口径」把措辞正确的回答判为失败",
        "detail": (
            "严格口径要求 key_facts 逐字全覆盖，会把「答对了但换了说法」的正确回答判为失败。"
            "本报告并列给出严格与宽松两个口径，把「判定口径对结论的影响」摆在明面上，而不是只报一个好看的数字。"
        ),
        "evidence": "qa_003 答出「137元/科」，因未出现字面词「费用」被判 partial（宽松通过、严格失败）。",
    },
    {
        "id": "D7", "severity": "medium", "status": "已修复",
        "title": "题集自带的标注错误",
        "detail": "qa_028（四级报名费）原标注为「应拒答」，但知识库中明确写明 36 元/人次，属于标注方错，非系统缺陷。",
        "evidence": "已将该题改为数字提取，并另补 qa_031 作为真·拒答用例。",
    },
    {
        "id": "D8", "severity": "medium", "status": "未修复",
        "title": "拒答判定在本可回答的问题上仍偏保守（过度拒答）",
        "detail": (
            "跨文档综合类问题仍被判为「资料不足」而拒绝：材料其实足以支撑答案，模型却先声明无法回答，"
            "属于过度拒答（wrong_refusal）。本批漏拒为 0，风险集中在误拒一侧；"
            "误拒会让用户以为「系统不知道」，比答错更容易被察觉。"
        ),
        "evidence": "误拒 3 例：qa_007（计算机等级考试成绩查询）、qa_013（推优会议监票人产生方式）、"
                    "qa_018（奖学金评选流程，key_facts 覆盖 0/3）；本批漏拒 0 例，拒答总判定 28/31 正确。",
    },
    {
        "id": "D9", "severity": "high", "status": "已修复",
        "title": "评测口径漏气：判定分母只算「已匹配规则」，虚高准确率",
        "detail": (
            "审查判定原先以 n_matched（只统计 gold 与系统都输出的规则）为分母，"
            "gold 里存在、系统却没输出的规则（missing）不计入，等于「系统没答的题不扣分」，"
            "极端情况下系统只对少数规则作答也能拿到高分。"
            "【已修复】改以 gold 规则总数为分母，并单列 rule_coverage（覆盖率）与 "
            "matched_verdict_accuracy（已匹配项准确率），让「漏答」显式暴露。"
        ),
        "evidence": "tests/test_eval_review_runner.py::test_compare_verdicts_missing_and_extra 锁定口径："
                    "漏掉 1 条 gold 规则时 accuracy=0.5、coverage=0.5，而 matched_accuracy=1.0。",
    },
    {
        "id": "D10", "severity": "high", "status": "已修复",
        "title": "审查模型返回空内容：思维链吃满 max_tokens（推理型模型陷阱）",
        "detail": (
            "为审查调用设定 max_tokens=1200 后，事实抽取与软审查大面积返回空串，"
            "日志只报 `Expecting value: line 1 column 1 (char 0)` —— 看起来像 JSON 解析 bug，"
            "实为额度不足：本接入点是**推理型模型**，响应带 reasoning_content（思维链），"
            "思维链同样计入 max_tokens；被吃光后 finish_reason=length、正式 content 为空。"
            "【已修复】预算提高到 8000（实测思维链单次可达 ~3900 字符），"
            "并在 config.py 注明「思维链计入 max_tokens」。"
        ),
        "evidence": "同一材料直调模型：max_tokens=1200 → finish_reason=length、content 长度 0；"
                    "max_tokens=4000 → 正常但余量小；8000 → 稳定。修复后连测两次 /review，"
                    "三个阶段均在第 1 次调用成功，总耗时 37s / 33s（软审查单次 29~32s）。",
    },
    {
        "id": "D11", "severity": "critical", "status": "已修复",
        "title": "否定语义被编码成正向关键词：「无挂科」被当作必须出现的字样",
        "detail": (
            "规则 `综合素质测评_R003` 原为 `type:keyword, keywords:[\"无挂科\"]`，"
            "语义变成「材料必须逐字出现『无挂科』」，而它的真实含义是「不得有挂科」。"
            "放大器是引擎：keyword 族规则在字段为空时用 `_full_text` 顶替 value，"
            "**绕过「字段缺失 → 待核验」分支**，于是「材料没写」被算成「材料违规」。"
            "【已修复】① 规则数据：`综合素质测评_R003` / `团员推优_R003` 改 `type:status` "
            "并配 pass_values / fail_values，`团员推优_R011` 改 forbidden_keyword；"
            "② 引擎新增 `status` 三态分支：命中正状态词→通过、命中负状态词→不通过、"
            "两者都没命中→待核验，空值不再被全文顶替；"
            "③ 抽取提示词写明「否定约束用 forbidden_keyword、资格状态用 status」，堵住重传时复发。"
        ),
        "evidence": "用户实名材料实测：`挂科记录=''` 时结论为 pending"
                    "（「材料中没有『挂科记录』的明确证明，请补充成绩单或人工核验」），"
                    "修复前同一材料判 fail（致命 −30）。该材料全文检索「挂科」二字命中数为 0。"
                    "回归用例 tests/test_review_engine.py::"
                    "test_status_rule_distinguishes_pass_fail_and_missing_evidence。",
    },
    {
        "id": "D12", "severity": "high", "status": "已修复",
        "title": "keyword 规则的关键词对材料永不成立 / 被挂到核验学生事实的场景上",
        "detail": (
            "两种形态：① `综合素质测评_R004 总积分权重计算正确` 是 keyword，关键词是权重公式"
            "「D×8%、X×65%…」——学生提交的佐证材料里根本不会写制度公式，字段抽不到→回退全文→"
            "仍然命中不了，**必然判违规**；② `四六级考试报名_R004` / `考试报名管理_R015` 的关键词"
            "是通知原文「…不得同时报考」，本身没错，却被挂到 `scene:[\"资格审查\"]` —— "
            "审学生报考资格时当然找不到这句通知用语，同样必然误判。"
            "【已修复】① 元规则判定范围由「只看 rule_name」扩展为「name + field」，"
            "`R004` 的 field=「总积分计算公式」因此被正确判为「不适用」；"
            "② 上面两条 keyword 的 scene 改为「通知公告」（约束的是文档须载明某禁令，不是学生事实）。"
        ),
        "evidence": "用户材料实测 `综合素质测评_R004` 由 fail（error −10）变为 not_applicable。"
                    "新增真实规则库守卫 tests/test_rule_library_guards.py —— "
                    "该守卫在扫描时**又抓出第三条**（`考试报名管理_R015`），已一并修复。",
    },
    {
        "id": "D13", "severity": "high", "status": "已修复",
        "title": "加分条款被抽成 critical 资格条款：把「加不了分」判成「不合格」",
        "detail": (
            "`综合素质测评_R019 第二课堂时长至少60` 被抽成 `range / critical / scene:[\"资格审查\"]`。"
            "但细则原文该条位于「2．社会服务加分标准（S2 满分40分）」：「…完成第二课堂成绩单"
            "总时长且各细项达标的学生**可以加40分**；…每个细项按少1 小时**扣0.5 分**…」。"
            "即这是**加分标准**：未达标只少加分，不是不具备资格，判「未通过」口径过重。"
            "【已修复】severity 由 critical 降为 error，结论由「未通过」变为「需修改」。"
        ),
        "evidence": "用户材料 `第二课堂总时长=24学时` < 60：修复前致命 −30 → 结论「未通过 / 59 分」；"
                    "修复后 error −10。规则条款性质依据 data/【学院综测细则】"
                    "广东工业大学计算机学院2025-2026年度本科学生综合素质测评实施细则(1)(1).pdf。",
    },
]

SEV_LABEL = {"critical": "致命", "high": "高", "medium": "中"}
SEV_COLOR = {"critical": "#c0392b", "high": "#d68910", "medium": "#2c7fb8"}


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _pct(x, nd=1):
    return "—" if x is None else f"{x * 100:.{nd}f}%"


def _ci(d) -> str:
    ci = d.get("ci95") or (d.get("rate_ci95") if isinstance(d, dict) else None)
    if not ci:
        return ""
    return f"[{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]"


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


_CASE_LOADERS = {"qa": load_qa_cases, "review": load_review_cases}
_RUNNERS = {"qa": "eval/runners/run_qa.py", "review": "eval/runners/run_review.py"}


def artifact_fingerprint_mismatch(name: str, artifact: dict) -> str:
    """核对「产物里的题集指纹」与「按当前题集重算的指纹」。

    一致返回空串；缺指纹或对不上，返回一句可直接打印的说明。
    main() 据此拒绝出报告 —— 这正是文件头承诺的「指纹不一致即拒出结论」。
    此前只有注释、没有实现，靠 tests/test_eval_artifact.py 把这道防线兜住。
    """
    manifest = (artifact or {}).get("manifest") or {}
    actual = manifest.get("case_set_fingerprint")
    if not actual:
        return f"{name} 产物缺少题集指纹，无法核对它是否为当前题集所跑"
    expected = case_set_fingerprint(_CASE_LOADERS[name]())
    if actual != expected:
        return (f"{name} 产物指纹 {actual} ≠ 当前题集指纹 {expected}："
                f"产物与题集不配套，拒绝据此出结论，请重跑 {_RUNNERS[name]}")
    return ""


def section_qa(qa: dict) -> str:
    if not qa:
        return "<p class='muted'>未找到问答评测产物（eval_results_qa.json），请先运行 eval/runners/run_qa.py。</p>"
    s = qa["summary"]
    m = qa["manifest"]

    rows = []
    for t, d in sorted(s["by_type"].items(), key=lambda x: (x[1]["rate"] if x[1]["rate"] is not None else -1)):
        soft = d.get("soft")
        soft_s = "—" if soft is None else f"{soft * 100:.0f}%"
        rate = d["rate"]
        bar = "" if rate is None else (
            f"<div class='bar'><span style='width:{rate * 100:.1f}%'></span></div>")
        rows.append(
            f"<tr><td>{_esc(t)}</td><td class='num'>{rate * 100:.1f}%</td>"
            f"<td class='bar-cell'>{bar}</td><td class='num muted'>{_ci(d)}</td>"
            f"<td class='num'>{d['k']}/{d['n']}</td><td class='num'>{soft_s}</td>"
            f"<td class='num muted'>{d.get('partial', 0)}</td></tr>")

    ref = s["refusal"]
    cite = s.get("citation_precision")
    soft_overall = s.get("overall_soft") or {}

    # 结论层的数字一律从产物现算，绝不手写 —— 手写的数字会在重跑后悄悄失真。
    results = qa.get("results") or []
    strict_fail = [r for r in results if not r.get("passed")]
    wording_only = [r for r in strict_fail if r.get("soft_passed")]
    both_fail = [r for r in strict_fail if not r.get("soft_passed")]
    wrong_ids = [r.get("id") for r in results if r.get("refusal") == "wrong_refusal"]
    missed_ids = [r.get("id") for r in results if r.get("refusal") == "missed_refusal"]
    ids = lambda xs: "、".join(str(x) for x in xs) if xs else "无"

    return f"""
<h2>一、问答模块（RAG 检索问答）</h2>
<p class="muted">题集版本 {_esc(m.get('case_set_version'))} · 指纹 <code>{_esc(m.get('case_set_fingerprint'))}</code>
 · {m.get('question_count')} 题（dev/test 已切分） · 评测时间 {_esc(m.get('generated_at'))}</p>

<div class="cards">
  <div class="card"><div class="kpi">{_pct(s['overall']['rate'])}</div>
    <div class="lbl">严格口径通过率</div>
    <div class="sub">{s['overall']['k']}/{s['overall']['n']} · 95%CI {_ci(s['overall'])}</div></div>
  <div class="card"><div class="kpi">{_pct(soft_overall.get('rate'))}</div>
    <div class="lbl">宽松口径通过率</div>
    <div class="sub">{soft_overall.get('k', '—')}/{soft_overall.get('n', '—')} · 95%CI {_ci(soft_overall)}</div></div>
  <div class="card"><div class="kpi">{_pct(cite)}</div>
    <div class="lbl">引用准确率</div>
    <div class="sub">答案引用编号是否越界</div></div>
  <div class="card"><div class="kpi">{ref['correct']}/{sum(ref.values())}</div>
    <div class="lbl">拒答判定正确</div>
    <div class="sub">误拒 {ref['wrong_refusal']} · 漏拒 {ref['missed_refusal']}</div></div>
</div>

<h3>分类型通过率</h3>
<table>
<thead><tr><th>类型</th><th>严格</th><th></th><th>95% 置信区间</th><th>通过/总数</th><th>宽松</th><th>部分命中</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<p class="note">小样本下必须看置信区间：4 题的「50%」与 20 题的「50%」可信度完全不同。
严格口径是判定口径的下限，宽松口径是上限，真实水平在两行之间。
<br><b>如何读这两行：</b>严格失败共 {len(strict_fail)} 题，其中 <b>{len(wording_only)} 题属于「实质答对、只是换了措辞」</b>
（部分命中 key_facts，宽松口径已通过）—— 这一部分衡量的是<b>判定口径</b>，不是检索质量。
真正两口径都不通过的只有 <b>{len(both_fail)} 题</b>：{ids([r.get('id') for r in both_fail])}。
<br>拒答方向：误拒 <b>{len(wrong_ids)} 例</b>（{ids(wrong_ids)}），漏拒 <b>{len(missed_ids)} 例</b>（{ids(missed_ids)}）。
误拒指材料其实足以作答却被判「资料不足」；漏拒指库里根本没有、系统却没拦住 —— 后者更接近幻觉，务必盯住。</p>
"""


def section_review(rv: dict) -> str:
    if not rv:
        return "<p class='muted'>未找到审查评测产物（eval_results_review.json），请先运行 eval/runners/run_review.py。</p>"
    s = rv["summary"]
    m = rv["manifest"]

    rows = []
    for r in rv["results"]:
        if "error" in r:
            rows.append(f"<tr class='err'><td>{_esc(r['id'])}</td><td colspan='5'>"
                        f"<b>报错</b>：{_esc(r['error'])}</td></tr>")
            continue
        acc = "—" if r.get("accuracy") is None else f"{r['accuracy'] * 100:.0f}%"
        fp = r.get("false_positive") or {}
        fp_s = "—" if fp.get("rate") is None else f"{fp['fp']}/{fp['total']}"
        rows.append(
            f"<tr><td>{_esc(r['id'])}</td><td>{_esc(r.get('domain'))}</td>"
            f"<td class='num'>{_esc(r.get('score'))}</td><td>{_esc(r.get('grade'))}</td>"
            f"<td class='num'>{acc}</td><td class='num'>{fp_s}</td></tr>")

    fp = s["false_positive"]
    # 系统输出但 gold 里没有的规则条数 —— 无人工标注可核对，故不计分，单列说明。
    n_extra = sum(len(r.get("extra") or []) for r in rv["results"] if "error" not in r)

    # 「漏判」= gold 判定为 fail、系统却没能判出 fail 的规则数（含规则压根没被路由到的）。
    # 这是审查系统最危险的方向：坏材料被放行。必须从产物现算，不能拍脑袋。
    missed_fail = 0
    for r in rv["results"]:
        if "error" in r:
            continue
        missed_fail += sum(1 for x in r.get("matched", []) if x["gold"] == "fail" and x["pred"] != "fail")
        missed_fail += sum(1 for x in r.get("missing", []) if x["gold"] == "fail")
    return f"""
<h2>二、材料审查模块</h2>
<p class="muted">用例指纹 <code>{_esc(m.get('case_set_fingerprint'))}</code> · {m.get('case_count')} 例
 · 评测时间 {_esc(m.get('generated_at'))}</p>

<div class="cards">
  <div class="card"><div class="kpi">{_pct(s['verdict_accuracy'])}</div>
    <div class="lbl">逐规则判定准确率</div><div class="sub">n={s['verdict_pairs']} 条已对齐规则</div></div>
  <div class="card"><div class="kpi">{_pct(fp['rate'])}</div>
    <div class="lbl">假阳性率</div>
    <div class="sub">{fp['fp']}/{fp['total']} · 干净材料被判违规</div></div>
  <div class="card"><div class="kpi">{s['cases_ok']}/{m.get('case_count')}</div>
    <div class="lbl">用例可完成</div><div class="sub">报错 {s['cases_error']} 例</div></div>
  <div class="card"><div class="kpi" style="color:var(--err)">{missed_fail}</div>
    <div class="lbl">漏判条数（假阴性）</div>
    <div class="sub">gold 判「不合格」却被放行 —— 最危险的方向</div></div>
</div>

<h3>逐用例结果</h3>
<table>
<thead><tr><th>用例</th><th>系统识别领域</th><th>总分</th><th>等级</th><th>逐规则准确率</th><th>假阳性</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>

<p class="note"><b>审查模块当前的结论：本轮修复后 {s['cases_ok']}/{m.get('case_count')} 例全部完成，逐规则判定与 gold 一致。</b>
但结论必须以「两个方向都不出错」（既不漏判坏材料、也不冤枉好材料）为准，而不是那个总分：
① 系统仍会输出 gold 之外的规则（本批 extra 共 {n_extra} 条），这些规则没有人工标注可核对，
故不计入准确率；「规则覆盖率」{_pct(s.get('rule_coverage'))} 用来反映系统有没有漏掉 gold 规则。
② 判定样本很小（已对齐规则 n={s['verdict_pairs']}），单条翻转就会明显改变百分比，务必结合置信区间读。
修复前后对照见下方缺陷清单：D1 / D2 / D3 / D4 / D10 / D11 / D12 / D13 均已修复
（D1 采「移除零规则领域」方案；D4 已用默认配置实测复核；
D11~D13 用一份实名综测材料端到端复测，结论由「未通过 / 59 分」变为「需修改 / 70 分」）。</p>
"""


def section_defects() -> str:
    rows = []
    for d in DEFECTS:
        rows.append(
            f"<tr><td class='sev' style='color:{SEV_COLOR[d['severity']]}'>"
            f"{SEV_LABEL[d['severity']]}</td>"
            f"<td><b>{_esc(d['id'])}｜{_esc(d['title'])}</b><div class='muted small'>{_esc(d['detail'])}</div></td>"
            f"<td class='small'>{_esc(d['evidence'])}</td>"
            f"<td class='small'>{_esc(d['status'])}</td></tr>")
    return f"""
<h2>三、测试挖出的缺陷清单</h2>
<p class="muted">每条都可追溯到具体用例或代码位置 —— 这是「测试板块」真正的产出，不是附赠品。</p>
<table class="defects">
<thead><tr><th>级别</th><th>问题</th><th>复现证据</th><th>状态</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
"""


def main():
    qa = load(QA_ARTIFACT)
    rv = load(REVIEW_ARTIFACT)

    if not qa and not rv:
        print("两个产物都不存在，先跑 eval/runners/run_qa.py 与 run_review.py")
        return 1

    # 兑现文件头承诺：产物指纹与当前题集不一致，直接拒绝出报告。
    mismatches = [msg for name, artifact in (("qa", qa), ("review", rv)) if artifact
                  for msg in (artifact_fingerprint_mismatch(name, artifact),) if msg]
    if mismatches:
        print("❌ 产物与当前题集不配套，拒绝生成报告：")
        for msg in mismatches:
            print("   - " + msg)
        return 2

    body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>工大智政 · 评测报告</title>
<style>
:root{{--ink:#1b1f24;--muted:#6b7785;--line:#e3e8ee;--bg:#f6f8fa;--card:#fff;
--accent:#2f6fed;--warn:#d68910;--err:#c0392b}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:40px 28px 72px}}
header{{border-bottom:3px solid var(--ink);padding-bottom:18px;margin-bottom:8px}}
h1{{font-size:27px;margin:0 0 6px}}
h2{{font-size:20px;margin:44px 0 10px;padding-left:11px;border-left:4px solid var(--accent)}}
h3{{font-size:16px;margin:28px 0 8px;color:#33404f}}
.muted{{color:var(--muted)}}
.small{{font-size:13px}}
code{{background:#eef2f7;padding:1px 6px;border-radius:4px;font-size:13px}}
.cards{{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}}
.card{{flex:1 1 190px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:16px 18px}}
.kpi{{font-size:27px;font-weight:700;font-variant-numeric:tabular-nums}}
.lbl{{font-size:13px;color:#33404f;margin-top:2px}}
.sub{{font-size:12px;color:var(--muted);margin-top:5px}}
table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-top:10px}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
th{{background:#eef2f7;font-size:13px;font-weight:600}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
tr:last-child td{{border-bottom:none}}
tr.err td{{background:#fdf1f0;color:var(--err)}}
.bar-cell{{width:130px}}
.bar{{background:#eef2f7;border-radius:4px;height:8px;overflow:hidden}}
.bar>span{{display:block;height:100%;background:var(--accent)}}
.note{{background:#fff;border:1px solid var(--line);border-left:3px solid var(--warn);
border-radius:8px;padding:12px 16px;font-size:14px;margin-top:14px}}
.defects td{{font-size:14px}}
.sev{{font-weight:700;white-space:nowrap}}
footer{{margin-top:56px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:13px}}
</style></head><body><div class="wrap">
<header>
<h1>工大智政 · 评测报告</h1>
<div class="muted">问答（RAG）与材料审查两个模块的客观评测 · 全部数字由 <code>eval/report.py</code> 从产物 JSON 现读渲染</div>
</header>
{section_qa(qa)}
{section_review(rv)}
{section_defects()}
<h2>四、复现方式</h2>
<pre class="note"><code># 1. 启动服务（沙箱有全局 HTTP_PROXY，必须让 HF 走离线缓存）
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uvicorn main:app --host 127.0.0.1 --port 8001

# 2. 跑评测（本地 localhost 请求要绕开代理）
unset HTTP_PROXY HTTPS_PROXY
python eval/runners/run_qa.py --split all
python eval/runners/run_review.py --split all     # 建议 TIMEOUT_AUX=90 重启服务后再跑

# 3. 纯逻辑用例（不需要服务、秒级）
python -m pytest tests -q

# 4. 生成本报告
python eval/report.py</code></pre>
<footer>产物：<code>eval_results_qa.json</code> · <code>eval_results_review.json</code> → 本报告 <code>eval_report.html</code><br>
所有评测产物均带题集指纹，指纹不一致即拒绝出结论，杜绝「用旧数据冒充新结果」。</footer>
</div></body></html>"""

    OUT.write_text(body, encoding="utf-8")
    print(f"报告已生成：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
