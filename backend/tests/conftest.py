import os

import pytest
from dotenv import load_dotenv

from planagent.config import REPO_ROOT

load_dotenv(REPO_ROOT / ".env", override=False)


def _has_deepseek_key() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def pytest_collection_modifyitems(config, items):
    skip_real_api = pytest.mark.skip(reason="DEEPSEEK_API_KEY not set")
    skip_real_wechat = pytest.mark.skip(reason="WECHAT_BOT_TOKEN not set")
    has_wechat = bool(os.environ.get("WECHAT_BOT_TOKEN"))
    has_deepseek = _has_deepseek_key()
    for item in items:
        if "real_api" in item.keywords and not has_deepseek:
            item.add_marker(skip_real_api)
        if "real_wechat" in item.keywords and not has_wechat:
            item.add_marker(skip_real_wechat)
