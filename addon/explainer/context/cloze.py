"""Cloze target marking (spec §5.3).

Anki renders the active deletion as ``<span class="cloze" data-ordinal="N">``
(question side: ``[...]`` or ``[hint]``, plus ``data-cloze`` holding the
answer; answer side: the revealed text) and inactive ones as
``<span class="cloze-inactive">``. Image Occlusion uses the same classes on
``<div data-shape=…>`` elements, which are handled by ``image_occlusion.py``.

* Question side: the active span becomes ``⟦BLANK⟧`` / ``⟦BLANK: hint⟧``.
* Answer side: it becomes ``⟦TARGET: answer⟧`` and the answer is recorded.
* Several deletions sharing the ordinal are all marked; the target joins them
  with " / ".
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from .html_to_text import Node

Side = Literal["question", "answer"]
_TAG_RE = re.compile(r"<[^>]+>")


def active_ordinal(card_ord: int) -> int:
    return card_ord + 1


def _is_active_cloze(node: "Node") -> bool:
    return "cloze" in node.classes() and "data-shape" not in node.attrs


def _blank_marker(inner: str) -> str:
    s = inner.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    if not s or s in ("...", "…"):
        return "⟦BLANK⟧"
    return f"⟦BLANK: {s}⟧"


@dataclass
class ClozeState:
    side: Side
    targets: list[str] = field(default_factory=list)
    # Answers of question-side blanks (from data-cloze). Never sent to the model;
    # used only by the hint leak guard.
    hidden_answers: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    _depth: int = 0

    def handle(self, node: "Node", inline_text: Callable[["Node"], str]) -> str | None:
        if not _is_active_cloze(node):
            return None
        if self.side == "question":
            hidden = node.attrs.get("data-cloze")
            if hidden:
                # data-cloze holds the answer's HTML, e.g. "<b>competitive</b>".
                plain = " ".join(html.unescape(_TAG_RE.sub(" ", hidden)).split())
                if plain:
                    self.hidden_answers.append(plain)
            inner = inline_text(node)
            marker = _blank_marker(inner)
            if marker != "⟦BLANK⟧":
                self.hints.append(marker[len("⟦BLANK: "):-1])
            return marker
        # Answer side: nested active spans inside a target are just text.
        if self._depth > 0:
            return inline_text(node)
        self._depth += 1
        try:
            inner = inline_text(node)
        finally:
            self._depth -= 1
        if not inner:
            return ""
        self.targets.append(inner)
        return f"⟦TARGET: {inner}⟧"

    @property
    def target(self) -> str | None:
        uniq = list(dict.fromkeys(t for t in self.targets if t))
        return " / ".join(uniq) if uniq else None
