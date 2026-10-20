"""Prompt eval set (spec §9.4): load ``cards.json`` into ``RawCard`` objects.

Card HTML is synthesized the way Anki renders it (checked against Anki 26.09
fixtures): ``<hr id=answer>`` for basic cards, ``<span class="cloze">`` spans
with ``data-cloze``/hints for clozes, ``[[type:Back]]`` for type-in cards. The
two Image Occlusion cards reuse the real rendered fixtures.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(ROOT, "addon"))

from explainer.context.extractor import RawCard  # noqa: E402

CLOZE_RE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)
NOTETYPES = {
    "basic": {"name": "Basic", "type": 0, "flds": [{"name": "Front"}, {"name": "Back"}]},
    "reversed": {"name": "Basic (and reversed card)", "type": 0, "flds": [{"name": "Front"}, {"name": "Back"}]},
    "type_in": {"name": "Basic (type in the answer)", "type": 0, "flds": [{"name": "Front"}, {"name": "Back"}]},
    "cloze": {"name": "Cloze", "type": 1, "originalStockKind": 5, "flds": [{"name": "Text"}, {"name": "Back Extra"}]},
}


@dataclass
class EvalCard:
    id: str
    domain: str
    type: str
    deliberately_wrong: bool
    answers: list[str]
    data: dict[str, Any]

    def raw(self, side: str) -> RawCard:
        return build_raw(self, side)


def load() -> list[EvalCard]:
    with open(os.path.join(HERE, "cards.json"), encoding="utf-8") as f:
        items = json.load(f)
    return [
        EvalCard(
            id=c["id"], domain=c["domain"], type=c["type"],
            deliberately_wrong=bool(c.get("deliberately_wrong")),
            answers=list(c.get("answers", [])), data=c,
        )
        for c in items
    ]


def render_cloze(text: str, ordinal: int, side: str) -> str:
    def repl(m: re.Match[str]) -> str:
        n, answer, hint = int(m.group(1)), m.group(2), m.group(3)
        if n != ordinal:
            return f'<span class="cloze-inactive" data-ordinal="{n}">{answer}</span>'
        if side == "question":
            shown = f"[{hint}]" if hint else "[...]"
            return f'<span class="cloze" data-cloze="{html.escape(answer, quote=True)}" data-ordinal="{n}">{shown}</span>'
        return f'<span class="cloze" data-ordinal="{n}">{answer}</span>'

    return CLOZE_RE.sub(repl, text)


def build_raw(card: EvalCard, side: str) -> RawCard:
    c = card.data
    media = os.path.join(TESTS, "fixtures")
    if card.type == "image_occlusion":
        with open(os.path.join(media, f"{c['fixture']}.json"), encoding="utf-8") as f:
            d = json.load(f)
        return RawCard(
            card_id=hash(card.id) & 0xFFFFFFF, note_id=1, ord=d["ord"], side=side,  # type: ignore[arg-type]
            notetype=d["notetype"], template=d["template"], deck_name=c["deck"], tags=[],
            q_html=d["q_html"], a_html=d["a_html"], fields=d["fields"], typed_answer=None,
            type_correct=None, audio_refs=[], media_dir=media,
        )
    nt = NOTETYPES[card.type]
    if card.type == "cloze":
        q = render_cloze(c["text"], 1, "question")
        a = render_cloze(c["text"], 1, "answer") + ("<br>\n" + c["extra"] if c.get("extra") else "")
        fields = {"Text": c["text"], "Back Extra": c.get("extra", "")}
        template = {"qfmt": "{{cloze:Text}}", "afmt": "{{cloze:Text}}<br>\n{{Back Extra}}"}
    elif card.type == "type_in":
        q = f"{c['front']}\n\n[[type:Back]]"
        a = f"{c['front']}\n\n<hr id=answer>\n\n[[type:Back]]"
        fields = {"Front": c["front"], "Back": c["back"]}
        template = {"qfmt": "{{Front}}\n\n{{type:Back}}", "afmt": "{{Front}}\n\n<hr id=answer>\n\n{{type:Back}}"}
    else:
        front, back = (c["back"], c["front"]) if card.type == "reversed" else (c["front"], c["back"])
        q = front
        a = f"{front}\n\n<hr id=answer>\n\n{back}"
        fields = {"Front": c["front"], "Back": c["back"]}
        template = {"qfmt": "{{Front}}", "afmt": "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}"}
    return RawCard(
        card_id=abs(hash(card.id)) & 0xFFFFFFF, note_id=1, ord=0, side=side,  # type: ignore[arg-type]
        notetype=nt, template=template, deck_name=c["deck"], tags=[], q_html=q, a_html=a,
        fields=fields,
        typed_answer=c.get("typed") if side == "answer" else None,
        type_correct=c.get("back") if card.type == "type_in" and side == "answer" else None,
        audio_refs=[], media_dir=media,
    )
