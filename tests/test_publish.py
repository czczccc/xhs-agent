import asyncio

import pytest

from xhs_agent import publish as publish_mod
from xhs_agent.config import Settings
from xhs_agent.publish import PublishError, publish_note


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResult:
    def __init__(self, text, is_error=False):
        self.content = [FakeBlock(text)]
        self.isError = is_error


def _settings():
    s = Settings()
    s.xhs_mcp_url = "http://fake-xhs-mcp:18060/mcp"
    return s


@pytest.mark.asyncio
async def test_publish_note_disabled_without_url():
    with pytest.raises(PublishError, match="XHS_MCP_URL"):
        await publish_note(Settings(), title="t", content="c", tags=[], images=["http://x/1.png"])


@pytest.mark.asyncio
async def test_publish_note_requires_images():
    with pytest.raises(PublishError, match="封面"):
        await publish_note(_settings(), title="t", content="c", tags=[], images=[])


@pytest.mark.asyncio
async def test_publish_note_success(monkeypatch):
    s = _settings()

    async def fake_call(url, arguments):
        assert url == s.xhs_mcp_url
        assert arguments["title"] == "t" and arguments["tags"] == ["美食"]
        return FakeResult("发布成功 https://www.xiaohongshu.com/explore/abc")

    monkeypatch.setattr(publish_mod, "_call_publish_content", fake_call)
    result = await publish_note(s, title="t", content="c", tags=["美食"], images=["http://x/1.png"])
    assert result.post_url == "https://www.xiaohongshu.com/explore/abc"


@pytest.mark.asyncio
async def test_publish_note_tool_error(monkeypatch):
    async def fake_call(url, arguments):
        return FakeResult("登录态过期，请重新登录", is_error=True)

    monkeypatch.setattr(publish_mod, "_call_publish_content", fake_call)
    with pytest.raises(PublishError, match="登录态过期"):
        await publish_note(_settings(), title="t", content="c", tags=[], images=["http://x/1.png"])


@pytest.mark.asyncio
async def test_publish_note_timeout(monkeypatch):
    s = _settings()
    s.xhs_mcp_timeout = 0.01

    async def fake_call(url, arguments):
        await asyncio.sleep(1)

    monkeypatch.setattr(publish_mod, "_call_publish_content", fake_call)
    with pytest.raises(PublishError, match="没有响应"):
        await publish_note(s, title="t", content="c", tags=[], images=["http://x/1.png"])
