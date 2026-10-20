"""Media resolution, resize and encoding for images sent to the model (§5.5).

* ``<img src>`` is resolved against the collection media folder; remote URLs
  are skipped unless "Allow remote images" is on.
* Images are downscaled so the long side is ≤ 1568 px. PNGs with
  transparency (or small images) stay PNG, everything else becomes JPEG
  quality 85. Re-encoding through Qt drops EXIF.
* SVG is rasterized with ``QSvgRenderer``; GIFs use their first frame.

Qt imports are local so this module can be imported without a GUI.
"""

from __future__ import annotations

import base64
import hashlib
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

MAX_LONG_SIDE = 1568
JPEG_QUALITY = 85
SMALL_PIXELS = 400 * 400
REMOTE_MAX_BYTES = 8 * 1024 * 1024
LOCAL_MAX_BYTES = 40 * 1024 * 1024

ImageRole = Literal["card_image", "io_target"]


@dataclass
class ImagePart:
    data_b64: str
    mime: str
    role: str
    name: str
    width: int
    height: int

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.data_b64}"


def is_remote(src: str) -> bool:
    return src.lower().startswith(("http://", "https://", "//"))


def is_data_url(src: str) -> bool:
    return src.lower().startswith("data:")


def resolve_local(src: str, media_dir: str) -> str | None:
    """Map an ``<img src>`` to a file inside the media folder, or ``None``."""
    if not src or is_remote(src) or is_data_url(src) or not media_dir:
        return None
    if src.lower().startswith("file:"):
        return None
    name = urllib.parse.unquote(src.split("?", 1)[0].split("#", 1)[0])
    if os.path.isabs(name) or name.startswith(("/", "\\")):
        name = name.lstrip("/\\")
    base = os.path.realpath(media_dir)
    path = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([base, path]) != base:
        return None  # path traversal
    return path if os.path.isfile(path) else None


def image_identity(src: str, media_dir: str) -> str:
    """Cheap, stable identity for cache hashing (no decoding)."""
    path = resolve_local(src, media_dir)
    if path:
        try:
            st = os.stat(path)
            return f"{os.path.basename(path)}:{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            pass
    if is_data_url(src):
        return "data:" + hashlib.sha256(src.encode("utf-8", "replace")).hexdigest()[:16]
    return src


def load_bytes(src: str, media_dir: str, allow_remote: bool) -> bytes | None:
    if is_data_url(src):
        try:
            header, payload = src.split(",", 1)
            if ";base64" in header:
                return base64.b64decode(payload)
            return urllib.parse.unquote_to_bytes(payload)
        except Exception:
            return None
    if is_remote(src):
        if not allow_remote:
            return None
        return _fetch_remote(src if not src.startswith("//") else "https:" + src)
    path = resolve_local(src, media_dir)
    if not path:
        return None
    try:
        if os.path.getsize(path) > LOCAL_MAX_BYTES:
            return None
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _fetch_remote(url: str) -> bytes | None:
    try:
        import requests

        with requests.get(url, timeout=(5, 10), stream=True) as resp:
            if resp.status_code != 200:
                return None
            chunks = []
            total = 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > REMOTE_MAX_BYTES:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception:
        return None


def _looks_like_svg(data: bytes, name: str) -> bool:
    if name.lower().split("?")[0].endswith(".svg"):
        return True
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower())


def decode(data: bytes, name: str = "") -> Any:
    """Decode bytes into a ``QImage`` (or ``None``)."""
    from PyQt6.QtCore import QByteArray, QSize
    from PyQt6.QtGui import QColor, QImage, QPainter

    if _looks_like_svg(data, name):
        from PyQt6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(QByteArray(data))
        if not renderer.isValid():
            return None
        size = renderer.defaultSize()
        if size.isEmpty():
            size = QSize(1024, 1024)
        scale = min(1.0, MAX_LONG_SIDE / max(size.width(), size.height()))
        if max(size.width(), size.height()) < 512:
            scale = 512 / max(size.width(), size.height())
        target = QSize(max(1, int(size.width() * scale)), max(1, int(size.height() * scale)))
        img = QImage(target, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        painter = QPainter(img)
        renderer.render(painter)
        painter.end()
        return img
    img = QImage()
    # QImage reads the first frame of animated GIFs.
    if not img.loadFromData(data):
        return None
    return img


def downscale(img: Any, max_side: int = MAX_LONG_SIDE) -> Any:
    from PyQt6.QtCore import Qt

    w, h = img.width(), img.height()
    if max(w, h) <= max_side:
        return img
    if w >= h:
        return img.scaledToWidth(max_side, Qt.TransformationMode.SmoothTransformation)
    return img.scaledToHeight(max_side, Qt.TransformationMode.SmoothTransformation)


def _has_transparency(img: Any) -> bool:
    if not img.hasAlphaChannel():
        return False
    # Sample a grid; a fully opaque image with an alpha channel can go to JPEG.
    w, h = img.width(), img.height()
    step_x = max(1, w // 24)
    step_y = max(1, h // 24)
    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            if img.pixelColor(x, y).alpha() < 255:
                return True
    return False


def encode(img: Any, prefer_png: bool = False) -> tuple[bytes, str]:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QImage

    small = img.width() * img.height() <= SMALL_PIXELS
    use_png = prefer_png or small or _has_transparency(img)
    buf = QByteArray()
    qbuf = QBuffer(buf)
    qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
    if use_png:
        img.save(qbuf, "PNG")
        mime = "image/png"
    else:
        if img.hasAlphaChannel():
            img = img.convertToFormat(QImage.Format.Format_RGB32)
        img.save(qbuf, "JPEG", JPEG_QUALITY)
        mime = "image/jpeg"
    qbuf.close()
    return bytes(buf.data()), mime


def to_part(img: Any, role: str, name: str, prefer_png: bool = False) -> ImagePart:
    data, mime = encode(img, prefer_png=prefer_png)
    return ImagePart(
        data_b64=base64.b64encode(data).decode("ascii"),
        mime=mime,
        role=role,
        name=name,
        width=img.width(),
        height=img.height(),
    )


def prepare(data: bytes, name: str, role: str = "card_image") -> ImagePart | None:
    img = decode(data, name)
    if img is None or img.isNull():
        return None
    return to_part(downscale(img), role, os.path.basename(name.split("?")[0]) or "image")
