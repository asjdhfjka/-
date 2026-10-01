"""config 单测：重点验证「.env 一定在任何 os.getenv 之前加载」这个时序修复。"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parents[1]


def test_base_config_sane():
    assert config.ARK_BASE_URL.startswith("http")
    assert config.REVIEW_MAX_MB > 0
    assert config.MAX_APPLICABLE_RULES > 0
    assert config.MIN_CHUNK_CHARS > 0
    assert config.RULES_FILE.endswith(".json")
    assert config.TIMEOUT_REVIEW >= 30
    assert config.REVIEW_CONTENT_MAX_CHARS > 1000
    assert config.REVIEW_MAX_TOKENS > 0
    assert config.RULE_DRAFTS_FILE.endswith("rule_drafts.json")
    assert config.AUTH_DB_PATH.endswith("auth.db")
    assert config.AUTH_SESSION_DAYS > 0


def test_summary_mentions_key_knobs():
    text = config.summary()
    for token in ("模型=", "召回k=", "重试=", "缓存=", "领域=", "规则上限="):
        assert token in text


def test_env_is_loaded_before_config_reads_it(tmp_path):
    """把 config.py 复制到临时目录，配一份 .env，验证 .env 的值能被读到。

    改造前 main.py 是「先 import config、再 load_dotenv()」，
    config 里所有 os.getenv 在 import 那一刻就求值完毕，.env 等于没生效。
    这条用例把那次事故钉死：只要 load_dotenv 被挪到 os.getenv 之后，它就会红。
    """
    shutil_target = tmp_path / "config.py"
    shutil_target.write_text((ROOT / "config.py").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text("ARK_MODEL=ep-test-from-dotenv\n", encoding="utf-8")

    code = textwrap.dedent("""
        import sys
        sys.path.insert(0, r"{tmp}")
        import config
        print(config.ARK_MODEL)
    """).format(tmp=tmp_path)
    clean_env = os.environ.copy()
    clean_env.pop("ARK_MODEL", None)
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        timeout=60, env=clean_env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ep-test-from-dotenv"
