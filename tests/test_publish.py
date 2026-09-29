import asyncio

import pytest

from xhs_agent import publish as publish_mod
from xhs_agent.config import Settings
from xhs_agent.publish import (
    LoginExpiredError,
    PublishError,
    check_login_status,
    get_login_qrcode,
    logout,
    publish_note,
    send_login_code,
    verify_login_code,
)


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeImageBlock:
    def __init__(self, data, mime_type="image/png"):
        self.type = "image"
        self.data = data
        self.mime_type = mime_type


class FakeResult:
    # 字段名跟 mcp SDK 2.x 的 CallToolResult 一致（is_error，不是 1.x 的 isError）
    def __init__(self, *blocks, is_error=False):
        self.content = list(blocks)
        self.is_error = is_error


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
async def test_publish_note_success_without_url(monkeypatch):
    # xiaohongshu-mcp 真实的成功返回：只有一句话 + 结构体，不带笔记链接
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("内容发布成功: &{Title:t Content:c Images:1 Status:发布完成}"))

    _patch_call(monkeypatch, fake)
    result = await publish_note(_settings(), title="t", content="c", tags=[], images=["http://x/1.png"])
    assert result.post_url is None and "发布成功" in result.message


@pytest.mark.asyncio
async def test_publish_note_creator_login_expired(monkeypatch):
    # 截图里的真实返回：创作者中心会话建不起来
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("发布失败: 创作者中心登录失效，请重新扫码登录"), is_error=True)

    _patch_call(monkeypatch, fake)
    with pytest.raises(LoginExpiredError, match="重新扫码"):
        await publish_note(_settings(), title="t", content="c", tags=[], images=["http://x/1.png"])


@pytest.mark.asyncio
async def test_publish_note_failure_text_without_error_flag(monkeypatch):
    # 就算错误标记没传过来，「发布失败」开头也不能当成功或「不确定」
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("发布失败: 标题长度超过限制"))

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="标题长度超过限制") as ei:
        await publish_note(_settings(), title="t", content="c", tags=[], images=["http://x/1.png"])
    assert not isinstance(ei.value, LoginExpiredError)


@pytest.mark.asyncio
async def test_publish_note_unclear_result_is_not_reported_as_success(monkeypatch):
    # 没报错，但既没说发布成功也没链接——不能当成功处理
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("已提交，等待处理"))

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="不确定"):
        await publish_note(_settings(), title="t", content="c", tags=[], images=["http://x/1.png"])


@pytest.mark.asyncio
async def test_publish_note_reads_legacy_isError(monkeypatch):
    # mcp SDK 1.x 的字段名
    class Legacy:
        def __init__(self):
            self.content = [FakeTextBlock("发布失败: 连接中断")]
            self.isError = True

    async def fake(url, tool, arguments):
        return Legacy()

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="连接中断"):
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
async def test_check_login_status_real_format(monkeypatch):
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("✅ 已登录\n用户名: 巷口小馆\n\n你可以使用其他功能了。"))

    _patch_call(monkeypatch, fake)
    assert (await check_login_status(_settings())).logged_in is True

    async def fake_out(url, tool, arguments):
        return FakeResult(FakeTextBlock("❌ 未登录\n\n请使用 get_login_qrcode 工具获取二维码进行登录。"))

    _patch_call(monkeypatch, fake_out)
    assert (await check_login_status(_settings())).logged_in is False


@pytest.mark.asyncio
async def test_get_login_qrcode_already_logged_in(monkeypatch):
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("你当前已处于登录状态"))

    _patch_call(monkeypatch, fake)
    qr = await get_login_qrcode(_settings())
    assert qr.already_logged_in is True and qr.image == ""


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


# ---------- 手机号 + 验证码登录（xiaohongshu-mcp-pro） ----------


@pytest.mark.asyncio
async def test_send_login_code(monkeypatch):
    async def fake(url, tool, arguments):
        assert tool == "creator_phone_login" and arguments == {"phone": "13800000000"}
        return FakeResult(FakeTextBlock("验证码已发送，请查看截图后调用 creator_verify_otp 填写验证码"), FakeImageBlock("Zm9v"))

    _patch_call(monkeypatch, fake)
    assert "验证码已发送" in await send_login_code(_settings(), " 13800000000 ")


@pytest.mark.asyncio
async def test_send_login_code_rejects_bad_phone(monkeypatch):
    async def fake(url, tool, arguments):
        raise AssertionError("格式不对不该调用服务")

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="手机号"):
        await send_login_code(_settings(), "12345")


@pytest.mark.asyncio
async def test_verify_login_code_uses_longer_timeout(monkeypatch):
    seen = {}

    async def fake_call(settings, tool, arguments=None):
        seen["timeout"], seen["tool"], seen["args"] = settings.xhs_mcp_timeout, tool, arguments
        return FakeResult(FakeTextBlock("creator 登录成功，cookies 已保存。"))

    monkeypatch.setattr(publish_mod, "_call_tool", fake_call)
    s = _settings()
    s.xhs_mcp_timeout = 30
    assert "登录成功" in await verify_login_code(s, "123456")
    assert seen == {"timeout": 180.0, "tool": "creator_verify_otp", "args": {"otp": "123456"}}
    assert s.xhs_mcp_timeout == 30  # 不改原配置


@pytest.mark.asyncio
async def test_verify_login_code_error(monkeypatch):
    async def fake(url, tool, arguments):
        return FakeResult(FakeTextBlock("验证码登录失败: 请先调用 creator_phone_login 发送验证码"), is_error=True)

    _patch_call(monkeypatch, fake)
    with pytest.raises(PublishError, match="请先调用"):
        await verify_login_code(_settings(), "123456")
