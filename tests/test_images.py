import base64
import os

from explainer.context import images as imgs
from conftest import FIXTURES


def _qimage(w, h, alpha=False):
    from PyQt6.QtGui import QColor, QImage

    fmt = QImage.Format.Format_ARGB32 if alpha else QImage.Format.Format_RGB32
    img = QImage(w, h, fmt)
    img.fill(QColor(0, 0, 0, 0) if alpha else QColor("#447799"))
    return img


def _bytes(img, fmt):
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice

    buf = QByteArray()
    b = QBuffer(buf)
    b.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(b, fmt)
    return bytes(buf.data())


def test_resolve_local_and_traversal(tmp_path):
    (tmp_path / "a b.png").write_bytes(b"x")
    assert imgs.resolve_local("a%20b.png", str(tmp_path)).endswith("a b.png")
    assert imgs.resolve_local("../secret.png", str(tmp_path)) is None
    assert imgs.resolve_local("https://x.org/a.png", str(tmp_path)) is None
    assert imgs.resolve_local("missing.png", str(tmp_path)) is None


def test_remote_skipped_unless_allowed(tmp_path):
    assert imgs.load_bytes("https://example.invalid/a.png", str(tmp_path), allow_remote=False) is None


def test_downscale_long_side(qapp):
    out = imgs.downscale(_qimage(4000, 1000))
    assert (out.width(), out.height()) == (1568, 392)
    tall = imgs.downscale(_qimage(500, 3136))
    assert (tall.width(), tall.height()) == (250, 1568)
    small = _qimage(100, 100)
    assert imgs.downscale(small) is small


def test_encode_jpeg_for_large_opaque_png_for_alpha_or_small(qapp):
    big = imgs.prepare(_bytes(_qimage(1200, 900), "PNG"), "big.png")
    assert big.mime == "image/jpeg"
    alpha = imgs.prepare(_bytes(_qimage(1200, 900, alpha=True), "PNG"), "alpha.png")
    assert alpha.mime == "image/png"
    small = imgs.prepare(_bytes(_qimage(120, 90), "PNG"), "s.png")
    assert small.mime == "image/png"
    assert big.data_url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(big.data_b64)[:2] == b"\xff\xd8"


def test_svg_rasterized(qapp):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="red"/></svg>'
    part = imgs.prepare(svg, "shape.svg")
    assert part is not None and (part.width, part.height) == (512, 256)


def test_gif_first_frame(qapp):
    # A tiny known GIF (1x1); Qt reads the first frame.
    data = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    part = imgs.prepare(data, "dot.gif")
    assert part is not None and (part.width, part.height) == (1, 1)


def test_invalid_image_returns_none(qapp):
    assert imgs.prepare(b"not an image", "x.png") is None


def test_identity_changes_with_file(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"1")
    a = imgs.image_identity("x.png", str(tmp_path))
    p.write_bytes(b"12")
    assert imgs.image_identity("x.png", str(tmp_path)) != a
