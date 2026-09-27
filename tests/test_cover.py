"""封面渲染和照片上传。"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from xhs_agent import uploads
from xhs_agent.cover import SIZE, STYLES, render_cover
from xhs_agent.schemas import NoteRequest

SHOP = {"name": "巷口小馆", "shop_type": "家常菜", "area": "文三路", "signature": ["酸菜鱼"]}


def jpeg_bytes(size=(1600, 1200), color=(180, 100, 40), exif_orientation=None) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        im.save(buf, "JPEG", exif=exif)
    else:
        im.save(buf, "JPEG")
    return buf.getvalue()


@pytest.mark.parametrize("style", STYLES)
def test_cover_with_photo_is_3_4_png(tmp_path, style):
    photo = tmp_path / "p.jpg"
    photo.write_bytes(jpeg_bytes())
    out = render_cover("天凉了 板栗烧鸡上桌", "巷口小馆 · 文三路", tmp_path / f"{style}.png", photo, style)
    with Image.open(out) as im:
        assert im.format == "PNG" and im.size == SIZE
        # 左上角区域：big/badge 有字或角标，颜色不应和纯照片一样
        assert im.getpixel((20, 20)) != (180, 100, 40) or style == "bottom"


def test_cover_without_photo_and_long_text(tmp_path):
    out = render_cover("周日两小时备菜带饭一整周都不重样还超级省钱" * 2, "巷口小馆", tmp_path / "c.png")
    with Image.open(out) as im:
        assert im.size == SIZE


def test_upload_normalizes_image(tmp_path):
    pid = uploads.save_upload(tmp_path, jpeg_bytes(size=(4000, 3000), exif_orientation=6))  # 6 = 需顺时针转 90°
    with Image.open(uploads.path_for(tmp_path, pid)) as im:
        assert max(im.size) == uploads.MAX_SIDE
        assert im.size[1] > im.size[0]  # 按 EXIF 摆正成竖图
        assert 0x0112 not in im.getexif()  # EXIF 已去掉


def test_upload_rejects_bad_input(tmp_path):
    with pytest.raises(uploads.UploadError):
        uploads.save_upload(tmp_path, b"not an image")
    with pytest.raises(uploads.UploadError):
        uploads.path_for(tmp_path, "../../etc/passwd")


def test_pipeline_uses_first_photo_and_style_endpoint(agent, settings):
    from xhs_agent.web import app as web

    web._agent = agent
    c = TestClient(web.app)
    r = c.post("/api/uploads", files={"file": ("a.jpg", jpeg_bytes(color=(10, 200, 10)), "image/jpeg")})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert c.get(r.json()["url"]).headers["content-type"] == "image/jpeg"
    assert c.post("/api/uploads", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 400

    run = c.post("/api/runs", json={"topic": "板栗烧鸡上新", "shop": SHOP, "photos": [pid], "cover_style": "badge"}).json()
    assert run["photos"] == [f"/api/uploads/{pid}"] and run["cover_style"] == "badge"
    cover = c.get(run["cover_url"])
    assert cover.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(cover.content)) as im:
        # 角标样式右下方主要是照片（绿色）
        r_, g, b = im.convert("RGB").getpixel((900, 700))
        assert g > 150 and r_ < 80

    other = c.get(f"/api/runs/{run['run_id']}/cover?style=bottom")
    assert other.status_code == 200 and other.content != cover.content
    assert (settings.output_dir / f"{run['run_id']}_cover_bottom.png").exists()
    assert c.get(f"/api/runs/{run['run_id']}/cover?style=nope").status_code == 400


def test_missing_photo_falls_back(agent, settings):
    state = agent.start(NoteRequest(topic="板栗烧鸡上新", shop=SHOP, photos=["0" * 32]))
    assert state["status"] == "pending_review"
    with Image.open(state["cover_path"]) as im:
        assert im.size == SIZE


def test_line_breaks_are_balanced():
    from PIL import ImageDraw

    from xhs_agent.cover import _balanced, _font, find_font

    d, f = ImageDraw.Draw(Image.new("RGB", (10, 10))), _font(find_font(), 150)
    assert _balanced(d, "秋天上新板栗烧鸡", f, 936) == ["秋天上新", "板栗烧鸡"]  # 不出现「…烧 / 鸡」孤字
    assert _balanced(d, "天凉了 板栗烧鸡上桌", f, 936) == ["天凉了", "板栗烧鸡上桌"]  # 有空格按空格断
    assert _balanced(d, "短标题", f, 936) == ["短标题"]
