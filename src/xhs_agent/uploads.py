"""店主上传的照片：校验、摆正方向、缩到长边 ≤2000px、统一存成 JPEG。

重新编码会丢掉 EXIF（包括手机照片里的拍摄位置），只保留像素。
文件存在 <OUTPUT_DIR>/uploads/<id>.jpg，id 是 32 位十六进制。
"""

from __future__ import annotations

import io
import re
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_BYTES = 15 * 1024 * 1024  # 前端会先压缩；这里兜底防超大文件
MAX_SIDE = 2000
_ID = re.compile(r"^[0-9a-f]{32}$")


class UploadError(ValueError):
    pass


def upload_dir(output_dir: Path) -> Path:
    return output_dir / "uploads"


def path_for(output_dir: Path, photo_id: str) -> Path:
    if not _ID.match(photo_id or ""):
        raise UploadError("图片 id 不合法")
    return upload_dir(output_dir) / f"{photo_id}.jpg"


def save_upload(output_dir: Path, data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise UploadError("图片太大了，请换一张小于 15MB 的")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
    except (UnidentifiedImageError, OSError) as e:
        raise UploadError("这张图片的格式不支持，请换一张 JPG 或 PNG") from e
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    photo_id = uuid.uuid4().hex
    path = path_for(output_dir, photo_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=88, optimize=True)
    return photo_id
