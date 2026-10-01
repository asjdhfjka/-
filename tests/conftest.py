"""pytest 公共配置。

要点：
    1. 把项目根目录加进 sys.path，让 `import config / prompts / review_engine` 生效；
    2. 显式关掉查询缓存与重试，避免测试结果被缓存或隐式重试影响。

这些用例**不加载任何模型**，毫秒级可跑完。需要模型与向量库的端到端用例
见 tests/test_api_integration.py（默认跳过）。
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试环境下不依赖真实密钥；配置项本身有默认值
os.environ.setdefault("CACHE_ENABLED", "0")
os.environ.setdefault("ARK_MAX_RETRIES", "0")
