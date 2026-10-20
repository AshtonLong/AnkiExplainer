"""Regenerate tests/fixtures/*.json from a real Anki collection.

Each fixture captures what ``extractor.gather_raw()`` would read for one card:
rendered question/answer HTML, fields, notetype info, template and ordinal.
Run with a Python that has the ``anki`` and ``PyQt6`` packages installed:

    python scripts/make_fixtures.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

from anki.collection import Collection

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures")


def make_png(path: str, w: int = 640, h: int = 400) -> None:
    from PyQt6.QtGui import QColor, QImage, QPainter

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.fillRect(40, 40, 200, 120, QColor("#3366cc"))
    p.fillRect(380, 220, 160, 120, QColor("#cc3333"))
    p.end()
    img.save(path)


def dump(name: str, card, extra: dict | None = None) -> None:  # type: ignore[no-untyped-def]
    note = card.note()
    nt = card.note_type()
    tmpls = nt["tmpls"]
    tmpl = tmpls[0] if nt["type"] == 1 else tmpls[card.ord]
    data = {
        "card_id": card.id,
        "note_id": note.id,
        "ord": card.ord,
        "notetype": {
            "name": nt["name"],
            "type": nt["type"],
            "originalStockKind": nt.get("originalStockKind"),
            "flds": [{"name": f["name"]} for f in nt["flds"]],
        },
        "template": {"qfmt": tmpl["qfmt"], "afmt": tmpl["afmt"]},
        "deck_name": card.col.decks.name(card.did),
        "tags": list(note.tags),
        "q_html": card.question(),
        "a_html": card.answer(),
        "fields": dict(note.items()),
        "audio_refs": [getattr(t, "filename", "") for t in card.question_av_tags() + card.answer_av_tags() if getattr(t, "filename", None)],
    }
    data.update(extra or {})
    with open(os.path.join(OUT, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def main() -> None:
    from PyQt6.QtGui import QGuiApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)  # noqa: F841
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp()
    col = Collection(os.path.join(tmp, "collection.anki2"))
    media = col.media.dir()
    make_png(os.path.join(media, "heart.png"))
    shutil.copy(os.path.join(media, "heart.png"), os.path.join(OUT, "heart.png"))
    deck = col.decks.id("Biochem::Enzymes")

    basic = col.models.by_name("Basic")
    n = col.new_note(basic)
    n["Front"] = "What does <b>ATP</b> stand for?"
    n["Back"] = 'Adenosine <i>triphosphate</i> [sound:atp.mp3]<br><img src="heart.png">'
    n.tags = ["biochem", "energy"]
    col.add_note(n, deck)
    dump("basic", n.cards()[0])

    rev = col.models.by_name("Basic (and reversed card)")
    n = col.new_note(rev)
    n["Front"] = "perro"
    n["Back"] = "dog"
    col.add_note(n, deck)
    dump("reversed_fwd", n.cards()[0])
    dump("reversed_rev", n.cards()[1])

    cloze = col.models.by_name("Cloze")
    n = col.new_note(cloze)
    n["Text"] = (
        "A {{c1::<b>competitive</b>::type of inhibitor}} inhibitor raises apparent {{c2::K<sub>m</sub>}} "
        "but leaves {{c1::V<sub>max</sub>}} unchanged."
    )
    n["Back Extra"] = "Lineweaver–Burk lines intersect on the y-axis."
    col.add_note(n, deck)
    dump("cloze_c1", n.cards()[0])
    dump("cloze_c2", n.cards()[1])

    typing = col.models.by_name("Basic (type in the answer)")
    n = col.new_note(typing)
    n["Front"] = "Non-selective beta blocker used for performance anxiety?"
    n["Back"] = "propranolol"
    col.add_note(n, deck)
    dump("type_in", n.cards()[0], {"typed_answer": "metoprolol", "type_correct": "propranolol"})

    n = col.new_note(basic)
    n["Front"] = (
        "Solve \\(x^2 - 5x + 6 = 0\\)<ul><li>use factoring</li><li>check roots</li></ul>"
        "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "<pre><code class=\"language-python\">print(roots)</code></pre>"
    )
    n["Back"] = "\\[x = 2 \\text{ or } x = 3\\]"
    col.add_note(n, deck)
    dump("math_code", n.cards()[0])

    col.add_image_occlusion_notetype()
    io_nt = col.models.by_name("Image Occlusion")
    occ = (
        "{{c1::image-occlusion:rect:left=.0625:top=.1:width=.3125:height=.3:oi=1}}<br>"
        "{{c2::image-occlusion:ellipse:left=.59375:top=.55:rx=.125:ry=.15:oi=1}}<br>"
        "{{c1::image-occlusion:polygon:points=.7,.05 .9,.05 .8,.3:oi=1}}<br>"
        "{{c3::image-occlusion:text:left=.4:top=.85:text=Ventricle:scale=1:fs=0.05:oi=1}}"
    )
    col.add_image_occlusion_note(io_nt["id"], os.path.join(OUT, "heart.png"), occ, "Heart anatomy", "Blue = right atrium", [])
    nid = col.find_notes('"note:Image Occlusion"')[0]
    note = col.get_note(nid)
    cards = note.cards()
    dump("io_c1", cards[0])
    dump("io_c2", cards[1])
    col.close()
    print("fixtures written to", OUT)


if __name__ == "__main__":
    main()
