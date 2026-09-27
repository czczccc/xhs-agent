"""封面渲染：1080×1440（3:4）PNG，小红书竖版封面尺寸。

- 有照片：第一张照片裁切铺满，叠加封面大字 + 副标题（店名 · 位置），三种样式：
  big（左上大字）/ bottom（底部白色标题栏）/ badge（左上红色角标，照片遮挡最少）
- 没照片：纯色底 + 大字

中文字体不打包进仓库：按 COVER_FONT → 系统常见中文字体的顺序查找
（部署镜像里 apt 装 fonts-noto-cjk 即可）。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

SIZE = W, H = 1080, 1440
STYLES = ("big", "bottom", "badge")
PAD = 72
RED = (255, 36, 66)
PALETTES = [  # 无照片时的底色 / 文字色
    ((255, 241, 230), (255, 36, 66)),
    ((234, 244, 255), (31, 111, 235)),
    ((241, 255, 240), (45, 164, 78)),
    ((255, 248, 214), (183, 121, 31)),
]
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",  # Debian/Ubuntu: fonts-noto-cjk
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",  # macOS
    "C:/Windows/Fonts/msyhbd.ttc",  # Windows 微软雅黑粗体
    "C:/Windows/Fonts/NotoSansSC-VF.ttf",
    "C:/Windows/Fonts/simhei.ttf",
]


def find_font(explicit: str | None = None) -> str:
    for p in ([explicit] if explicit else []) + FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    raise RuntimeError("找不到中文字体：请安装 fonts-noto-cjk，或在 .env 里用 COVER_FONT 指定字体文件路径")


@lru_cache(maxsize=64)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    try:  # 可变字体（如 NotoSansSC-VF）切到粗体
        font.set_variation_by_name("Bold")
    except (OSError, ValueError, AttributeError):
        pass
    return font


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """中文按字换行；空格视为优先断行点（「周日2小时 带饭一整周」断成两行）。"""
    lines: list[str] = []
    for part in text.split():
        line = ""
        for ch in part:
            if line and draw.textlength(line + ch, font=font) > max_w:
                lines.append(line)
                line = ch
            else:
                line += ch
        if line:
            lines.append(line)
    return lines or [""]


def _balanced(draw, text: str, font, max_w: int) -> list[str]:
    """行数不变的前提下把各行长度拉平，避免「秋天上新板栗烧 / 鸡」这种孤字。"""
    lines = _wrap(draw, text, font, max_w)
    if len(lines) < 2 or len(text.split()) > 1:  # 有空格的按作者断行意图来
        return lines
    lo, hi = int(sum(draw.textlength(line, font=font) for line in lines) / len(lines)), max_w
    while lo < hi:  # 二分找仍是同样行数的最窄宽度
        mid = (lo + hi) // 2
        if len(_wrap(draw, text, font, mid)) <= len(lines):
            hi = mid
        else:
            lo = mid + 1
    return _wrap(draw, text, font, hi)


def _fit(draw, text: str, font_path: str, max_w: int, max_lines: int, size: int, min_size: int):
    """从 size 开始缩小字号，直到能在 max_lines 行内放下；各行长度尽量均匀。"""
    while True:
        font = _font(font_path, size)
        lines = _balanced(draw, text, font, max_w)
        if len(lines) <= max_lines or size <= min_size:
            return font, lines[:max_lines]
        size -= 6


def _gradient(height: int, top_alpha: int, bottom_alpha: int) -> Image.Image:
    """竖直方向的半透明黑色渐变，用来保证照片上的白字看得清。"""
    mask = Image.linear_gradient("L").resize((W, height))
    mask = mask.point(lambda v: int(top_alpha + (bottom_alpha - top_alpha) * v / 255))
    layer = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    layer.putalpha(mask)
    return layer


def _text_block(img: Image.Image, xy, lines, font, fill, line_gap=1.18, shadow=True) -> int:
    """逐行画字，返回下一行的 y。白字带柔和阴影。"""
    x, y = xy
    step = int(font.size * line_gap)
    if shadow:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for i, line in enumerate(lines):
            d.text((x + 3, y + 5 + i * step), line, font=font, fill=(0, 0, 0, 150))
        img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((x, y + i * step), line, font=font, fill=fill)
    return y + len(lines) * step


def _load_photo(path: Path) -> Image.Image:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        return ImageOps.fit(im, SIZE, method=Image.LANCZOS, centering=(0.5, 0.45)).convert("RGBA")


def render_cover(
    cover_text: str,
    subtitle: str,
    out_path: Path,
    photo: Path | None = None,
    style: str = "big",
    font_path: str | None = None,
) -> Path:
    font_file = find_font(font_path)
    cover_text = (cover_text or "").strip() or subtitle
    style = style if style in STYLES else "big"

    if photo is None:
        bg, accent = PALETTES[int(hashlib.md5(cover_text.encode()).hexdigest(), 16) % len(PALETTES)]
        img = Image.new("RGBA", SIZE, bg + (255,))
        draw = ImageDraw.Draw(img)
        font, lines = _fit(draw, cover_text, font_file, W - 2 * PAD, 4, 150, 80)
        block_h = len(lines) * int(font.size * 1.18)
        y = _text_block(img, (PAD, (H - block_h) // 2 - 60), lines, font, accent + (255,), shadow=False)
        draw.rectangle((PAD, y + 30, PAD + 120, y + 42), fill=accent)
        sub = _font(font_file, 44)
        draw.text((PAD, y + 80), subtitle, font=sub, fill=(90, 90, 90))
    else:
        img = _load_photo(photo)
        draw = ImageDraw.Draw(img)
        sub = _font(font_file, 44)
        if style == "big":
            img.alpha_composite(_gradient(620, 170, 0), (0, 0))
            img.alpha_composite(_gradient(360, 0, 190), (0, H - 360))
            font, lines = _fit(draw, cover_text, font_file, W - 2 * PAD, 3, 150, 90)
            _text_block(img, (PAD, PAD + 20), lines, font, (255, 255, 255, 255))
            _text_block(img, (PAD, H - PAD - 60), [subtitle], sub, (255, 255, 255, 235))
        elif style == "bottom":
            font, lines = _fit(draw, cover_text, font_file, W - 2 * PAD, 2, 108, 70)
            # 面板高度随字数变：红条 + 大字 + 副标题 + 下边距
            panel_h = 64 + 12 + 34 + len(lines) * int(font.size * 1.18) + 24 + 50 + PAD
            top = H - panel_h
            draw.rectangle((0, top, W, H), fill=(255, 255, 255, 255))
            draw.rectangle((PAD, top + 64, PAD + 90, top + 76), fill=RED)
            y = _text_block(img, (PAD, top + 110), lines, font, (28, 28, 30, 255), shadow=False)
            draw.text((PAD, y + 24), subtitle, font=sub, fill=(110, 110, 115))
        else:  # badge
            img.alpha_composite(_gradient(300, 0, 170), (0, H - 300))
            font, lines = _fit(draw, cover_text, font_file, int(W * 0.62), 2, 84, 56)
            step = int(font.size * 1.18)
            box_w = int(max(draw.textlength(line, font=font) for line in lines)) + 64
            box_h = len(lines) * step + 44
            draw.rounded_rectangle((PAD, PAD, PAD + box_w, PAD + box_h), radius=24, fill=RED + (240,))
            _text_block(img, (PAD + 32, PAD + 18), lines, font, (255, 255, 255, 255), shadow=False)
            _text_block(img, (PAD, H - PAD - 60), [subtitle], sub, (255, 255, 255, 235))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path
