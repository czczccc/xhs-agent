import asyncio

import pytest

from xhs_agent import publish as publish_mod
from xhs_agent.config import Settings
from xhs_agent.publish import (
    PublishError,
    check_login_status,
    get_login_qrcode,
    logout,
    publish_note,
)


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeImageBlock:
    def __init__(self, data, mime_type="image/png"):
        self.type = "image"
        self.data = data
        self.mimeType = mime_type


class FakeResult:
    def __init__(self, *blocks, is_error=False):
        self.content = list(blocks)
        self.isError = is_error


def _settings():
    s = Settings()
    s.xhs_mcp_url = "http://fake-xhs-mcp:18060/mcp"
    return s


def _patch_call(monkeypatch, fn):
    """fn(url, tool, arguments) -> FakeResult；替换掉真正建立 MCP 连接的那一层。"""
    monkeypatch.setattr(publish_mod, "_connect_and_call", fn)


# ---------- publish_note ----------


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

    async def fake(url, tool, arguments):
        assert url == s.xhs_mcp_url and tool == "publish_content"
        assert arguments["title"] == "t" and arguments["tags"] == ["美食"]
        return FakeResult(FakeTextBlock("发布成功 https://www.xiaohongshu.com/explore/abc"))

    _patch_call(monkeypatch, fake)
    result = await publish_note(s, title="t", content="c", tags=["美食"], images=["http://x/1.png"])
    assert result.post_url == "https://www.xiaohongshu.com/explore/abc"


@pytest.mark.asyncio
async def test_publish_note_tool_error(monkeypatch):
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("登录态过期，请重新登录"), is_error=True)

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="登录态过期"):
        await publish_note(_settings(), title="t", content="c", tags=[], images=["http://x/1.png"])


@pytest.mark.asyncio
async def test_publish_note_timeout(monkeypatch):
    s = _settings()
    s.xhs_mcp_timeout = 0.01

    async def fake(url, tool, arguments):
        await asyncio.sleep(1)

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="没有响应"):
        await publish_note(s, title="t", content="c", tags=[], images=["http://x/1.png"])


# ---------- 登录状态 / 二维码 / 退出登录 ----------


@pytest.mark.asyncio
async def test_check_login_status_json(monkeypatch):
    async def fake(url, tool, arguments):
        assert tool == "check_login_status"
        return FakeResult(FakeTextBlock('{"logged_in": true, "nickname": "巷口小馆"}'))

    _patch_call(monkeypatch, fake)
    status = await check_login_status(_settings())
    assert status.logged_in is True


@pytest.mark.asyncio
async def test_check_login_status_plain_text_fallback(monkeypatch):
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("未登录，请先扫码"))

    _patch_call(monkeypatch, fake)
    status = await check_login_status(_settings())
    assert status.logged_in is False


@pytest.mark.asyncio
async def test_get_login_qrcode_from_image_block(monkeypatch):
    async def fake(url, tool, arguments):
        assert tool == "get_login_qrcode"
        return FakeResult(FakeImageBlock("Zm9v"), FakeTextBlock('{"timeout": 120}'))

    _patch_call(monkeypatch, fake)
    qr = await get_login_qrcode(_settings())
    assert qr.image == "data:image/png;base64,Zm9v"
    assert qr.expires_in == 120


@pytest.mark.asyncio
async def test_get_login_qrcode_from_text_json(monkeypatch):
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock('{"qrcode": "Zm9v", "expires_in": 90}'))

    _patch_call(monkeypatch, fake)
    qr = await get_login_qrcode(_settings())
    assert qr.image == "data:image/png;base64,Zm9v"
    assert qr.expires_in == 90


@pytest.mark.asyncio
async def test_get_login_qrcode_unparseable_raises(monkeypatch):
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("不知道是什么格式的返回"))

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="没能从"):
        await get_login_qrcode(_settings())


@pytest.mark.asyncio
async def test_logout_success(monkeypatch):
    async def fake(url, tool, arguments):
        assert tool == "delete_cookies"
        return FakeResult(FakeTextBlock("已退出登录"))

    _patch_call(monkeypatch, fake)
    await logout(_settings())  # 不抛异常就算过


@pytest.mark.asyncio
async def test_logout_error(monkeypatch):
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("删除失败"), is_error=True)

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="删除失败"):
        await logout(_settings())
