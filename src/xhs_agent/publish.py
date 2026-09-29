"""发布到小红书：调用 xiaohongshu-mcp（外部浏览器自动化服务，非小红书官方 API）的 publish_content 工具。

不在生成流程里自动触发——只在店主看过草稿、通过审核之后，前端有一次显式的「确认发布」点击才会调用这里。
使用前需要单独运行 xiaohongshu-mcp 自带的登录工具扫码登录一次，见 README「发布到小红书」一节。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from .config import Settings

_URL_RE = re.compile(r"https?://\S+")


class PublishError(RuntimeError):
    """发布失败：连不上服务、超时，或 xiaohongshu-mcp 自己报错（例如登录态过期）。"""


@dataclass
class PublishResult:
    message: str
    post_url: str | None = None


async def _call_publish_content(url: str, arguments: dict) -> "object":
    from mcp import ClientSession
    from mcp.client import streamable_http as transport

    # 不同版本的 mcp SDK 里这个函数名不一样（streamablehttp_client / streamable_http_client），
    # 返回的也可能是 2 元组或 3 元组（多一个 get_session_id），这里只取前两个流。
    connect = getattr(transport, "streamablehttp_client", None) or transport.streamable_http_client
    async with connect(url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool("publish_content", arguments=arguments)


async def publish_note(settings: Settings, *, title: str, content: str, tags: list[str], images: list[str]) -> PublishResult:
    """调用 xiaohongshu-mcp 发布一篇图文笔记。images 必须是 xiaohongshu-mcp 服务能直接访问到的地址。"""
    if not settings.publish_enabled:
        raise PublishError("没有配置 XHS_MCP_URL，没有启用发布功能")
    if not images:
        raise PublishError("没有封面图，不能发布")

    arguments: dict = {"title": title, "content": content, "images": images}
    if tags:
        arguments["tags"] = tags

    try:
        result = await asyncio.wait_for(
            _call_publish_content(settings.xhs_mcp_url, arguments), timeout=settings.xhs_mcp_timeout
        )
    except asyncio.TimeoutError as e:
        raise PublishError(f"发布服务 {settings.xhs_mcp_timeout:.0f} 秒没有响应，浏览器自动化可能卡住了") from e
    except Exception as e:  # noqa: BLE001 — 网络/进程/协议问题统一包装成发布失败，不让原始异常炸到接口层
        raise PublishError(f"连不上发布服务：{type(e).__name__}: {e}") from e

    text = "\n".join(getattr(b, "text", "") for b in getattr(result, "content", []) if getattr(b, "type", None) == "text").strip()
    if getattr(result, "isError", False):
        raise PublishError(text or "xiaohongshu-mcp 返回了错误，但没有说明原因")

    m = _URL_RE.search(text)
    return PublishResult(message=text or "已发布", post_url=m.group(0) if m else None)
