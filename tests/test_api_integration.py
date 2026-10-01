"""端到端集成用例（默认跳过）。

为什么默认跳过：
    导入 main 会加载嵌入模型、精排模型与 OCR 引擎（数 GB、数十秒），
    并需要真实的向量库与 .env。这属于「要跑就必须在有完整环境时跑」的一类，
    不适合混进随手的 pytest。

开启方式：
    RUN_INTEGRATION=1 pytest tests/test_api_integration.py

前置：
    1) 复用桌面版虚拟环境（含 langchain / sentence-transformers / rapidocr）
    2) .env 里配好 ARK_API_KEY；chroma_db 与 auto_rules.json 存在于项目根
"""

import io
import os
import tempfile
import uuid

import pytest

pytestmark = pytest.mark.integration

RUN = os.getenv("RUN_INTEGRATION") == "1"

if not RUN:
    pytest.skip("设 RUN_INTEGRATION=1 才运行端到端用例（需要模型与向量库）",
                allow_module_level=True)

fastapi_testclient = pytest.importorskip("fastapi.testclient")

# 集成测试使用独立数据库，避免修改开发者本机的账号与历史记录。
os.environ["AUTH_DB_PATH"] = os.path.join(
    tempfile.gettempdir(), f"gdut_auth_integration_{uuid.uuid4().hex}.db")
os.environ["AUTH_DEMO_PASSWORD"] = "Integration@123"
import main as app_module
from fastapi.testclient import TestClient

client = TestClient(app_module.app)


@pytest.fixture(scope="module", autouse=True)
def login_admin():
    resp = client.post(
        "/auth/login",
        json={"username": "admin", "password": "Integration@123"},
    )
    assert resp.status_code == 200, resp.text
    yield
    client.post("/auth/logout")


def test_stats_endpoint():
    resp = client.get("/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "llm" in body and "cache" in body and "config" in body
    assert "calls" in body["llm"]


def test_upload_rejects_bad_extension():
    files = {"file": ("bad.exe", io.BytesIO(b"MZ"), "application/octet-stream")}
    resp = client.post("/upload", files=files)
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_upload_rejects_empty_file():
    files = {"file": ("empty.txt", io.BytesIO(b""), "text/plain")}
    resp = client.post("/upload", files=files)
    assert "error" in resp.json()


def test_review_returns_structured_report_or_clear_error():
    """最小端到端：上传一份纯文本材料，必须拿到 review_result 或一个明确的错误。

    注意不校验具体分数（那依赖模型），只校验「结构契约」不被破坏：
    等级 / 总评分 / 硬规则得分 / 适用规则数 这几个前端依赖的字段。
    """
    content = (
        "广东工业大学学生干部时长证明表\n"
        "学院：计算机学院 姓名：张三 学号：3123001234\n"
        "组织单位：学生会 部门职位：学习委员 表现情况：表现良好\n"
        "日期：2026年09月30日\n"
    ).encode("utf-8")
    files = {"file": ("材料.txt", io.BytesIO(content), "text/plain")}
    resp = client.post("/review", files=files)
    assert resp.status_code == 200
    body = resp.json()

    if "error" in body:
        # 允许「规则库未初始化」这类前置缺失的明确报错，但必须是可读的中文提示
        assert "规则库" in body["error"] or "审查失败" in body["error"]
        return

    r = body["review_result"]
    for key in ("材料类型", "审查结论", "总评分", "等级", "硬规则得分", "事实清单",
                "硬规则校验", "软审查", "适用规则数", "已核验规则数", "待核验规则数",
                "下一步建议"):
        assert key in r, f"审查结果缺少字段：{key}"
    assert isinstance(r["硬规则校验"], list)
    if r["硬规则校验"]:
        rule = r["硬规则校验"][0]
        for key in ("rule_id", "rule_name", "passed", "not_applicable", "severity"):
            assert key in rule
