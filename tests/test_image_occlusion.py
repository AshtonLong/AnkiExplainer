import os

import pytest

from explainer.context import image_occlusion as io
from conftest import FIXTURES, load_fixture


def test_detect_notetype():
    d = load_fixture("io_c1")
    assert io.is_io_notetype(d["notetype"])
    assert not io.is_io_notetype(load_fixture("basic")["notetype"])
    assert io.is_io_notetype({"flds": [{"name": "Image"}, {"name": "Occlusion"}]})
    assert io.is_legacy_ioe({"flds": [{"name": n} for n in ("Image", "Question Mask", "Answer Mask", "Header")]})


def test_parse_field_syntax_all_shape_kinds():
    shapes = io.parse_occlusion_field(load_fixture("io_c1")["fields"]["Occlusion"], active_ordinal=1)
    assert [(s.kind, s.ordinal, s.active) for s in shapes] == [
        ("rect", 1, True), ("ellipse", 2, False), ("polygon", 1, True), ("text", 3, False),
    ]
    rect = shapes[0]
    assert rect.props["left"] == pytest.approx(0.0625) and rect.props["height"] == pytest.approx(0.3)
    assert rect.occlude_inactive
    assert shapes[2].props["points"] == ".7,.05 .9,.05 .8,.3"
    assert shapes[3].text == "Ventricle"


def test_parse_text_with_colons():
    shapes = io.parse_occlusion_field("{{c1::image-occlusion:text:left=.1:top=.2:text=Ratio 1:2 here:scale=1:fs=0.05}}")
    assert shapes[0].text == "Ratio 1:2 here"
    assert shapes[0].props["scale"] == 1.0


def test_parse_rendered_question_and_answer_sides():
    q = io.parse_rendered_shapes(load_fixture("io_c1")["q_html"])
    a = io.parse_rendered_shapes(load_fixture("io_c1")["a_html"])
    assert [s.active for s in q] == [True, False, True, False]
    assert [s.active for s in a] == [True, False, True, False]  # cloze-highlight on answer side
    assert q[3].props["fs"] == pytest.approx(0.05)
    assert q[0].occlude_inactive
    q2 = io.parse_rendered_shapes(load_fixture("io_c2")["q_html"])
    assert [s.active for s in q2] == [False, True, False, False]


def test_find_image_src():
    assert io.find_io_image_src(load_fixture("io_c1")["q_html"]).startswith("heart")


def test_geometry_normalized():
    shapes = io.parse_occlusion_field(load_fixture("io_c1")["fields"]["Occlusion"])
    g = io.shape_geometry(shapes[0], 640, 400)
    assert (g.x, g.y, g.w, g.h) == pytest.approx((40, 40, 200, 120))
    e = io.shape_geometry(shapes[1], 640, 400)
    assert (e.x, e.y, e.w, e.h) == pytest.approx((380, 220, 160, 120))
    p = io.shape_geometry(shapes[2], 640, 400)
    assert p.points == pytest.approx([(448, 20), (576, 20), (512, 120)])
    assert p.bounds() == pytest.approx((448, 20, 128, 100))


def test_geometry_legacy_pixels_rescaled():
    shape = io.Shape("rect", 1, {"left": 100.0, "top": 50.0, "width": 200.0, "height": 100.0})
    assert io._uses_pixels([shape])
    g = io.shape_geometry(shape, 500, 250, pixels=True, source_size=(1000, 500))
    assert (g.x, g.y, g.w, g.h) == pytest.approx((50, 25, 100, 50))


def test_text_shapes_described():
    shapes = io.parse_rendered_shapes(load_fixture("io_c1")["q_html"])
    assert io.describe_text_shapes(shapes) == ['"Ventricle" (at 40% across, 85% down)']


def _color(img, x, y):
    c = img.pixelColor(x, y)
    return c.red(), c.green(), c.blue()


def test_annotate_question_hides_target_and_outlines(qapp):
    from PyQt6.QtGui import QImage

    img = QImage(os.path.join(FIXTURES, "heart.png"))
    shapes = io.parse_rendered_shapes(load_fixture("io_c1")["q_html"])
    out = io.annotate(img, shapes, "question")
    # Inside the active rect (blue area originally) is now filled with the mask color.
    assert _color(out, 140, 120) == (0xFF, 0x8E, 0x8E)
    # The outline is magenta.
    assert _color(out, 140, 40) == (0xFF, 0x00, 0xCC)
    # Inactive ellipse is occluded (oi=1) with the light fill.
    assert _color(out, 460, 280) == (0xFF, 0xEB, 0xA2)
    # Original untouched.
    assert _color(img, 140, 120) == (0x33, 0x66, 0xCC)


def test_annotate_answer_shows_target(qapp):
    from PyQt6.QtGui import QImage

    img = QImage(os.path.join(FIXTURES, "heart.png"))
    shapes = io.parse_rendered_shapes(load_fixture("io_c1")["a_html"])
    out = io.annotate(img, shapes, "answer")
    assert _color(out, 140, 120) == (0x33, 0x66, 0xCC)  # visible on the answer side
    assert _color(out, 140, 40) == (0xFF, 0x00, 0xCC)
