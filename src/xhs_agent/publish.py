"""发布到小红书：调用 xiaohongshu-mcp（外部浏览器自动化服务，非小红书官方 API）的 MCP 工具。

登录状态（cookies）是 xiaohongshu-mcp 自己管的，存在它自己的数据目录里，不进这个项目的数据库——
MVP 阶段这边只负责转发「拿二维码 / 查状态 / 退出登录 / 发布」这几个动作，不做多账号、不落库。
一个部署实例对应一个已登录的小红书账号；同一账号同时只能在一处网页端登录。

发布不在生成流程里自动触发——只在店主看过草稿、通过审核之后，前端有一次显式的「确认发布」点击才会调用这里。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from dataclasses import dataclass

from .config import Settings

_URL_RE = re.compile(r"https?://\S+")


class PublishError(RuntimeError):
    """调用失败：连不上服务、超时，或 xiaohongshu-mcp 自己报错（例如登录态过期）。"""


class LoginExpiredError(PublishError):
    """小红书登录失效（常见：主站登录还在，但发帖用的创作者中心会话建立不起来），需要退出后重新扫码。"""


# xiaohongshu-mcp 登录失效时的报错文字，例如「发布失败: 创作者中心登录失效，请重新扫码登录」
_LOGIN_EXPIRED_HINTS = ("登录失效", "重新扫码", "未登录", "登录已过期", "登录态过期")


@dataclass
class PublishResult:
    message: str
    post_url: str | None = None


@dataclass
class LoginStatus:
    logged_in: bool
    message: str = ""


@dataclass
class LoginQrCode:
    image: str  # data URL，前端直接 <img src> 用；already_logged_in 时为空
    expires_in: int | None = None
    message: str = ""
    already_logged_in: bool = False


async def _connect_and_call(url: str, tool: str, arguments: dict | None = None) -> object:
    from mcp import ClientSession
    from mcp.client import streamable_http as transport

    # 不同版本的 mcp SDK 里这个函数名不一样（streamablehttp_client / streamable_http_client），
    # 返回的也可能是 2 元组或 3 元组（多一个 get_session_id），这里只取前两个流。
    connect = getattr(transport, "streamablehttp_client", None) or transport.streamable_http_client
    async with connect(url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool(tool, arguments=arguments or {})


async def _call_tool(settings: Settings, tool: str, arguments: dict | None = None) -> object:
    if not settings.publish_enabled:
        raise PublishError("没有配置 XHS_MCP_URL，没有启用发布功能")
    try:
        return await asyncio.wait_for(
            _connect_and_call(settings.xhs_mcp_url, tool, arguments), timeout=settings.xhs_mcp_timeout
        )
    except asyncio.TimeoutError as e:
        raise PublishError(f"发布服务 {settings.xhs_mcp_timeout:.0f} 秒没有响应，浏览器自动化可能卡住了") from e
    except Exception as e:  # noqa: BLE001 — 网络/进程/协议问题统一包装成失败，不让原始异常炸到接口层
        raise PublishError(f"连不上发布服务：{type(e).__name__}: {e}") from e


def _is_error(result: object) -> bool:
    # mcp SDK 2.x 把字段改成了 is_error，1.x 是 isError；只读其中一个会把所有报错都当成成功
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))


def _raise_error(text: str, fallback: str) -> None:
    if any(h in text for h in _LOGIN_EXPIRED_HINTS):
        raise LoginExpiredError(f"小红书登录失效了，退出后重新扫码登录再发（{text[:200]}）")
    raise PublishError(text or fallback)


def _text_of(result: object) -> str:
    return "\n".join(
        getattr(b, "text", "") for b in getattr(result, "content", []) if getattr(b, "type", None) == "text"
    ).strip()


def _image_of(result: object) -> str | None:
    for b in getattr(result, "content", []):
        if getattr(b, "type", None) == "image" and getattr(b, "data", None):
            mime = getattr(b, "mime_type", None) or getattr(b, "mimeType", None) or "image/png"
            return f"data:{mime};base64,{b.data}"
    return None


async def publish_note(settings: Settings, *, title: str, content: str, tags: list[str], images: list[str]) -> PublishResult:
    """调用 xiaohongshu-mcp 发布一篇图文笔记。images 必须是 xiaohongshu-mcp 服务能直接访问到的地址。"""
    if not images:
        raise PublishError("没有封面图，不能发布")
    arguments: dict = {"title": title, "content": content, "images": images}
    if tags:
        arguments["tags"] = tags

    result = await _call_tool(settings, "publish_content", arguments)
    text = _text_of(result)
    if _is_error(result) or text.startswith("发布失败"):
        _raise_error(text, "xiaohongshu-mcp 返回了错误，但没有说明原因")
    m = _URL_RE.search(text)
    # xiaohongshu-mcp 成功时只回「内容发布成功: {...}」，不带笔记链接，所以认这句话；
    # 既没这句话也没链接的就不能算成功，把原始返回带出去，让店主自己去 App 确认。
    if not m and "发布成功" not in text:
        raise PublishError(
            f"xiaohongshu-mcp 没有报错，但也没说发布成功，不确定这篇发出去没有——"
            f"去小红书 App 确认一下，原始返回：{text[:300]!r}"
        )
    return PublishResult(message=text or "已发布", post_url=m.group(0) if m else None)


async def check_login_status(settings: Settings) -> LoginStatus:
    """查小红书登录状态。xiaohongshu-mcp 具体怎么表达"已登录"没有公开的接口文档，这里尽量兼容
    结构化 JSON（{"logged_in": true, ...} / {"login": true, ...}）和纯文字两种返回。
    """
    result = await _call_tool(settings, "check_login_status")
    text = _text_of(result)
    if _is_error(result):
        raise PublishError(text or "查登录状态失败")
    logged_in = _parse_bool_field(text, ("logged_in", "login", "isLoggedIn", "loggedIn"))
    if logged_in is None:
        # xiaohongshu-mcp 实际返回「✅ 已登录\n用户名: xxx」或「❌ 未登录 ...」
        logged_in = ("已登录" in text or "logged in" in text.lower()) and "未登录" not in text and "not logged" not in text.lower()
    return LoginStatus(logged_in=logged_in, message=text)


async def get_login_qrcode(settings: Settings) -> LoginQrCode:
    """要一张登录二维码。优先取 MCP 返回的图片内容块；退化成从文字里解析 base64/超时时间。"""
    result = await _call_tool(settings, "get_login_qrcode")
    text = _text_of(result)
    if _is_error(result):
        raise PublishError(text or "获取登录二维码失败")

    image = _image_of(result)
    if not image and "已处于登录状态" in text:
        return LoginQrCode(image="", message=text, already_logged_in=True)
    expires_in = _parse_int_field(text, ("expires_in", "timeout", "expiresIn"))
    if image:
        return LoginQrCode(image=image, expires_in=expires_in, message=text)

    # 没有图片内容块：从文字里找 data URL，或裸 base64（拼成 data URL）
    m = re.search(r"data:image/\w+;base64,[A-Za-z0-9+/=]+", text)
    if m:
        return LoginQrCode(image=m.group(0), expires_in=expires_in, message=text)
    b64 = _parse_str_field(text, ("qrcode", "qr_code", "image", "base64"))
    if b64:
        if not b64.startswith("data:"):
            b64 = f"data:image/png;base64,{b64}"
        return LoginQrCode(image=b64, expires_in=expires_in, message=text)

    raise PublishError(f"没能从 xiaohongshu-mcp 的返回里解析出二维码图片，原始返回：{text[:300]!r}")


_PHONE_RE = re.compile(r"^1\d{10}$")
_CODE_RE = re.compile(r"^\d{4,8}$")
# 填完验证码后，分支版会在小红书要求「安全验证扫码」时最多等 120 秒，再加上跳主站同步登录态的 30 秒
_VERIFY_TIMEOUT = 180.0


@dataclass
class LoginStep:
    message: str
    screenshot: str | None = None  # 后台浏览器当时的页面截图（data URL），让店主看到小红书实际显示了什么


async def send_login_code(settings: Settings, phone: str) -> LoginStep:
    """用手机号登录创作者中心的第一步：让 xiaohongshu-mcp-pro 在后台浏览器里填手机号、点「发送验证码」。

    点完之后小红书可能真发了短信，也可能弹滑块/安全验证，工具本身分不出来，所以把页面截图一起带回去。
    """
    phone = phone.strip()
    if not _PHONE_RE.match(phone):
        raise PublishError("手机号格式不对，要 11 位大陆手机号")
    result = await _call_tool(settings, "creator_phone_login", {"phone": phone})
    text = _text_of(result)
    if _is_error(result):
        raise PublishError(text or "发送验证码失败")
    return LoginStep(message=text, screenshot=_image_of(result))


async def verify_login_code(settings: Settings, code: str) -> LoginStep:
    """第二步：把短信验证码填进同一个后台浏览器完成登录，登录态存在 xiaohongshu-mcp 自己的数据卷里。"""
    code = code.strip()
    if not _CODE_RE.match(code):
        raise PublishError("验证码格式不对")
    if settings.xhs_mcp_timeout < _VERIFY_TIMEOUT:
        settings = dataclasses.replace(settings, xhs_mcp_timeout=_VERIFY_TIMEOUT)
    result = await _call_tool(settings, "creator_verify_otp", {"otp": code})
    text = _text_of(result)
    if _is_error(result):
        raise PublishError(text or "验证码登录失败")
    return LoginStep(message=text, screenshot=_image_of(result))


async def logout(settings: Settings) -> None:
    result = await _call_tool(settings, "delete_cookies")
    if _is_error(result):
        raise PublishError(_text_of(result) or "退出登录失败")


def _as_json(text: str) -> dict | None:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _parse_bool_field(text: str, keys: tuple[str, ...]) -> bool | None:
    obj = _as_json(text)
    if obj:
        for k in keys:
            if k in obj:
                return bool(obj[k])
    return None


def _parse_int_field(text: str, keys: tuple[str, ...]) -> int | None:
    obj = _as_json(text)
    if obj:
        for k in keys:
            if isinstance(obj.get(k), (int, float)):
                return int(obj[k])
    return None


def _parse_str_field(text: str, keys: tuple[str, ...]) -> str | None:
    obj = _as_json(text)
    if obj:
        for k in keys:
            if isinstance(obj.get(k), str) and obj[k]:
                return obj[k]
    return None
