from pathlib import Path

from tools import batch_upload


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = ""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)

    def post(self, *args, **kwargs):
        return next(self.responses)


def test_discover_files_supports_recursion_deduplication_and_filtering(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    first = tmp_path / "a.txt"
    second = nested / "b.PDF"
    ignored = nested / "c.exe"
    first.write_text("a", encoding="utf-8")
    second.write_bytes(b"pdf")
    ignored.write_bytes(b"exe")

    files, problems = batch_upload.discover_files(
        [str(tmp_path), str(first)],
        recursive=True,
        extensions=batch_upload.SUPPORTED_EXTENSIONS,
    )

    assert files == sorted([first.resolve(), second.resolve()], key=lambda p: str(p).casefold())
    assert problems == []


def test_discover_files_reports_missing_and_unsupported_inputs(tmp_path):
    unsupported = tmp_path / "bad.exe"
    unsupported.write_bytes(b"x")

    files, problems = batch_upload.discover_files(
        [str(unsupported), str(tmp_path / "missing")],
        recursive=False,
        extensions=batch_upload.SUPPORTED_EXTENSIONS,
    )

    assert files == []
    assert len(problems) == 2
    assert any("不支持" in item for item in problems)
    assert any("不存在" in item for item in problems)


def test_upload_one_distinguishes_success_duplicate_and_application_error(tmp_path):
    path = tmp_path / "材料.txt"
    path.write_text("测试材料", encoding="utf-8")
    session = FakeSession([
        FakeResponse({"message": "✅ 上传成功！"}),
        FakeResponse({"message": "⚠️ 该文件内容已存在，无需重复上传！"}),
        FakeResponse({"error": "❌ 文件解析失败"}),
    ])

    success = batch_upload.upload_one(session, "http://test/upload", path, 1, 0, 0)
    duplicate = batch_upload.upload_one(session, "http://test/upload", path, 1, 0, 0)
    failed = batch_upload.upload_one(session, "http://test/upload", path, 1, 0, 0)

    assert success["status"] == "success"
    assert duplicate["status"] == "skipped"
    assert failed["status"] == "failed"
