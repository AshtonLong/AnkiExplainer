"""Native Image Occlusion support (spec §5.4).

Shapes are read from the card's *rendered* HTML (the hidden
``<div data-shape=…>`` list Anki emits, whose classes already say which shapes
are active on this side) and, as a fallback, from the ``Occlusion`` field
syntax::

    {{c1::image-occlusion:rect:left=.1:top=.2:width=.3:height=.4:oi=1}}
    {{c2::image-occlusion:ellipse:left=.5:top=.5:rx=.1:ry=.1:oi=1}}
    {{c1::image-occlusion:polygon:points=.1,.1 .2,.1 .15,.3:oi=1}}
    {{c3::image-occlusion:text:left=.6:top=.1:text=Aorta:scale=1:fs=0.05:oi=1}}

Coordinates are normalized to the image size (Anki 23.10.1+); values above
1.5 are treated as legacy absolute pixels. Both syntaxes were checked against
Anki 26.09 output and are covered by fixture tests.

Annotation draws, at the downscaled resolution that will be sent:
* active shape(s): thick magenta outline and a small "TARGET" label; on the
  question side the shape is also filled so the answer stays hidden, matching
  what the student sees;
* other shapes: a thin gray outline (filled light yellow when the note uses
  "occlude inactive", as Anki shows them).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .html_to_text import parse_html

ShapeKind = Literal["rect", "ellipse", "polygon", "text"]

IO_STOCK_KIND = 6  # ORIGINAL_STOCK_KIND_IMAGE_OCCLUSION
LEGACY_IOE_FIELDS = ("Image", "Question Mask", "Answer Mask")

ACTIVE_OUTLINE = "#ff00cc"
ACTIVE_FILL = "#ff8e8e"
INACTIVE_OUTLINE = "#8a8a8a"
INACTIVE_FILL = "#ffeba2"


@dataclass
class Shape:
    kind: str
    ordinal: int
    props: dict[str, Any] = field(default_factory=dict)
    active: bool = False

    @property
    def text(self) -> str:
        return str(self.props.get("text", ""))

    @property
    def occlude_inactive(self) -> bool:
        return str(self.props.get("oi", self.props.get("occludeinactive", ""))) in ("1", "true")


@dataclass
class Geometry:
    kind: str
    # rect / ellipse: bounding box (x, y, w, h); polygon: points; text: anchor
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    points: list[tuple[float, float]] = field(default_factory=list)
    angle: float = 0.0

    def bounds(self) -> tuple[float, float, float, float]:
        if self.kind == "polygon" and self.points:
            xs = [p[0] for p in self.points]
            ys = [p[1] for p in self.points]
            return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        return self.x, self.y, self.w, self.h


def is_io_notetype(notetype: dict[str, Any]) -> bool:
    if notetype.get("originalStockKind") == IO_STOCK_KIND:
        return True
    names = {f.get("name") for f in notetype.get("flds", [])}
    return {"Image", "Occlusion"} <= names


def is_legacy_ioe(notetype: dict[str, Any]) -> bool:
    names = {f.get("name") for f in notetype.get("flds", [])}
    return set(LEGACY_IOE_FIELDS) <= names


# --- parsing ----------------------------------------------------------------

_FIELD_SHAPE_RE = re.compile(r"\{\{c(\d+)::image-occlusion:(\w+):(.*?)\}\}", re.S)
_PROP_SPLIT_RE = re.compile(r":(?=[A-Za-z_][A-Za-z0-9_]*=)")
_NUMERIC = {"left", "top", "width", "height", "rx", "ry", "scale", "fs", "angle"}


def _coerce(key: str, value: str) -> Any:
    if key in _NUMERIC:
        try:
            return float(value)
        except ValueError:
            return 0.0
    return value


def parse_occlusion_field(text: str, active_ordinal: int | None = None) -> list[Shape]:
    """Parse the ``Occlusion`` field's ``{{cN::image-occlusion:…}}`` list."""
    shapes: list[Shape] = []
    for m in _FIELD_SHAPE_RE.finditer(text or ""):
        ordinal = int(m.group(1))
        kind = m.group(2)
        props: dict[str, Any] = {}
        for part in _PROP_SPLIT_RE.split(m.group(3)):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip()
            props[k] = _coerce(k, v.strip())
        shapes.append(
            Shape(
                kind=kind,
                ordinal=ordinal,
                props=props,
                active=active_ordinal is not None and ordinal == active_ordinal,
            )
        )
    return shapes


def parse_rendered_shapes(html: str) -> list[Shape]:
    """Read shapes from the rendered card HTML (``data-shape`` elements)."""
    shapes: list[Shape] = []
    for node in parse_html(html or "").iter():
        if "data-shape" not in node.attrs:
            continue
        classes = node.classes()
        props: dict[str, Any] = {}
        for k, v in node.attrs.items():
            if not k.startswith("data-") or k in ("data-shape", "data-ordinal"):
                continue
            key = k[len("data-"):]
            if key == "font-size":
                key = "fs"
            elif key == "occludeinactive":
                key = "oi"
            props[key] = _coerce(key, v)
        try:
            ordinal = int(node.attrs.get("data-ordinal", "0"))
        except ValueError:
            ordinal = 0
        shapes.append(
            Shape(
                kind=node.attrs["data-shape"],
                ordinal=ordinal,
                props=props,
                active="cloze" in classes or "cloze-highlight" in classes,
            )
        )
    return shapes


def find_io_image_src(html: str) -> str | None:
    root = parse_html(html or "")
    for node in root.iter():
        if node.attrs.get("id") == "image-occlusion-container":
            for child in node.iter():
                if child.tag == "img" and child.attrs.get("src"):
                    return child.attrs["src"]
    return None


# --- geometry -----------------------------------------------------------------


def _uses_pixels(shapes: list[Shape]) -> bool:
    for s in shapes:
        for k in ("left", "top", "width", "height", "rx", "ry"):
            v = s.props.get(k)
            if isinstance(v, float) and v > 1.5:
                return True
        if s.kind == "polygon":
            for x, y in _parse_points(str(s.props.get("points", ""))):
                if x > 1.5 or y > 1.5:
                    return True
    return False


def _parse_points(points: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for pair in points.replace(";", " ").split():
        if "," not in pair:
            continue
        a, b = pair.split(",", 1)
        try:
            out.append((float(a), float(b)))
        except ValueError:
            continue
    return out


def shape_geometry(
    shape: Shape,
    width: float,
    height: float,
    pixels: bool = False,
    source_size: tuple[float, float] | None = None,
) -> Geometry:
    """Map a shape to pixel geometry on an image of ``width`` × ``height``.

    ``pixels`` means the shape coordinates are absolute pixels of an image of
    ``source_size`` (legacy format); they are rescaled to the target size.
    """
    if pixels:
        sw, sh = source_size or (width, height)
        fx, fy = width / (sw or 1), height / (sh or 1)
    else:
        fx, fy = width, height
    p = shape.props
    angle = float(p.get("angle", 0.0) or 0.0)
    if shape.kind == "rect":
        return Geometry(
            "rect", p.get("left", 0.0) * fx, p.get("top", 0.0) * fy,
            p.get("width", 0.0) * fx, p.get("height", 0.0) * fy, angle=angle,
        )
    if shape.kind == "ellipse":
        rx, ry = p.get("rx", 0.0) * fx, p.get("ry", 0.0) * fy
        return Geometry(
            "ellipse", p.get("left", 0.0) * fx, p.get("top", 0.0) * fy, 2 * rx, 2 * ry,
            angle=angle,
        )
    if shape.kind == "polygon":
        pts = [(x * fx, y * fy) for x, y in _parse_points(str(p.get("points", "")))]
        return Geometry("polygon", points=pts, angle=0.0)
    return Geometry("text", p.get("left", 0.0) * fx, p.get("top", 0.0) * fy)


def describe_text_shapes(shapes: list[Shape]) -> list[str]:
    """Text-type shapes contribute their text (with a rough position)."""
    out = []
    for s in shapes:
        if s.kind != "text" or not s.text:
            continue
        left, top = s.props.get("left", 0.0), s.props.get("top", 0.0)
        pos = ""
        if isinstance(left, float) and isinstance(top, float) and left <= 1.5 and top <= 1.5:
            pos = f" (at {round(left * 100)}% across, {round(top * 100)}% down)"
        out.append(f'"{s.text}"{pos}')
    return out


# --- annotation (Qt) ------------------------------------------------------------


def annotate(image: Any, shapes: list[Shape], side: str, source_size: tuple[int, int] | None = None) -> Any:
    """Return a target-annotated copy of ``image`` (a ``QImage``)."""
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF

    out = image.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = out.width(), out.height()
    pixels = _uses_pixels(shapes)
    long_side = max(w, h)
    active_width = 4.0 if long_side >= 800 else 3.0
    inactive_width = max(1.0, active_width / 3)

    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    def path_for(g: Geometry) -> QPainterPath:
        path = QPainterPath()
        if g.kind == "rect":
            path.addRect(QRectF(0, 0, g.w, g.h) if g.angle else QRectF(g.x, g.y, g.w, g.h))
        elif g.kind == "ellipse":
            path.addEllipse(QRectF(0, 0, g.w, g.h) if g.angle else QRectF(g.x, g.y, g.w, g.h))
        elif g.kind == "polygon" and g.points:
            path.addPolygon(QPolygonF([QPointF(x, y) for x, y in g.points]))
            path.closeSubpath()
        return path

    def draw(g: Geometry, pen: QPen, brush: QBrush | None) -> None:
        painter.save()
        if g.angle and g.kind in ("rect", "ellipse"):
            painter.translate(g.x, g.y)
            painter.rotate(g.angle)
        painter.setPen(pen)
        painter.setBrush(brush if brush is not None else QBrush(Qt.BrushStyle.NoBrush))
        painter.drawPath(path_for(g))
        painter.restore()

    # Inactive first so active outlines are always on top.
    ordered = sorted((s for s in shapes if s.kind != "text"), key=lambda s: s.active)
    label_boxes: list[tuple[float, float, float, float]] = []
    for shape in ordered:
        g = shape_geometry(shape, w, h, pixels=pixels, source_size=source_size)
        if shape.active:
            fill = QBrush(QColor(ACTIVE_FILL)) if side == "question" else None
            pen = QPen(QColor(ACTIVE_OUTLINE), active_width)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            draw(g, pen, fill)
            label_boxes.append(g.bounds())
        else:
            fill = QBrush(QColor(INACTIVE_FILL)) if shape.occlude_inactive else None
            draw(g, QPen(QColor(INACTIVE_OUTLINE), inactive_width), fill)

    if label_boxes:
        font = QFont()
        font.setBold(True)
        font.setPixelSize(max(10, int(long_side / 60)))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text = "TARGET"
        tw = metrics.horizontalAdvance(text) + 8
        th = metrics.height() + 2
        # Label only the first active shape; extra labels add clutter.
        x, y, bw, bh = label_boxes[0]
        lx = min(max(0.0, x), max(0.0, w - tw))
        ly = y - th - 2 if y - th - 2 >= 0 else min(h - th, y + bh + 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(ACTIVE_OUTLINE)))
        painter.drawRect(QRectF(lx, ly, tw, th))
        painter.setPen(QColor("white"))
        painter.drawText(QRectF(lx, ly, tw, th), int(Qt.AlignmentFlag.AlignCenter), text)

    painter.end()
    return out


def composite_svg_mask(image: Any, svg_bytes: bytes) -> Any:
    """Legacy Image Occlusion Enhanced: rasterize the SVG mask over the image."""
    from PyQt6.QtCore import QByteArray, QRectF
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer

    out = image.convertToFormat(QImage.Format.Format_ARGB32)
    renderer = QSvgRenderer(QByteArray(svg_bytes))
    if not renderer.isValid():
        return out
    painter = QPainter(out)
    renderer.render(painter, QRectF(0, 0, out.width(), out.height()))
    painter.end()
    return out
