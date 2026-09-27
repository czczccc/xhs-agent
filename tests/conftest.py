import pytest

from xhs_agent.config import Settings
from xhs_agent.graph import XhsAgent
from xhs_agent.llm import FakeLLM


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.output_dir = tmp_path / "outputs"
    s.log_file = tmp_path / "logs" / "runs.jsonl"
    s.events_file = tmp_path / "logs" / "events.jsonl"
    s.shops_file = tmp_path / "logs" / "shops.json"
    s.require_shop_code = False  # 老的 Web 测试不带码；试用码见 test_shops.py
    return s


@pytest.fixture
def agent(settings):
    return XhsAgent(settings=settings, llm=FakeLLM())
