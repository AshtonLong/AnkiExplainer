"""``Host`` implementation backed by the running Anki instance.

All private reviewer APIs (``_showAnswer``, ``_getTypedAnswer``) are wrapped in
feature checks with fallbacks so an Anki update can't break review.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import aqt
from aqt import mw
from aqt.qt import QApplication, QKeySequence, QMenu, QTimer, QCursor
from aqt.utils import openLink, tooltip

from ..context import extractor
from ..context import image_occlusion as io
from ..context.extractor import KIND_LABELS
from ..llm.models import all_models
from ..util.log import get_logger

log = get_logger("host")


def key_label(key: str) -> str:
    if not key:
        return ""
    return QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText) or key


class AnkiHost:
    def __init__(self, settings: Any, open_settings: Callable[[str | None], None]) -> None:
        self.settings = settings
        self._open_settings = open_settings

    # -- reviewer state -----------------------------------------------------
    def current_card(self) -> Any:
        if mw is None or mw.state != "review" or mw.reviewer is None:
            return None
        return mw.reviewer.card

    def reviewer_side(self) -> str | None:
        if self.current_card() is None:
            return None
        state = getattr(mw.reviewer, "state", None)
        return state if state in ("question", "answer") else None

    def card_summary(self, card: Any) -> dict[str, Any]:
        try:
            nt = card.note_type()
            if io.is_io_notetype(nt) or io.is_legacy_ioe(nt):
                kind = KIND_LABELS["image_occlusion"]
            elif nt.get("type") == 1:
                kind = KIND_LABELS["cloze"]
            elif any("{{type:" in t.get("qfmt", "") for t in nt.get("tmpls", [])):
                kind = KIND_LABELS["type_in"]
            else:
                kind = KIND_LABELS["basic"]
            deck = mw.col.decks.name(card.current_deck_id()) if self.settings.ctx("include_deck") else ""
            return {"kind": kind, "deck": deck}
        except Exception:
            return {"kind": "Card", "deck": ""}

    def gather_raw(self, card: Any, side: str) -> extractor.RawCard:
        return extractor.gather_raw(card, side, mw.reviewer)  # type: ignore[arg-type]

    def gather_raw_by_id(self, card_id: int, side: str) -> extractor.RawCard | None:
        try:
            card = mw.col.get_card(card_id)
        except Exception:
            return None
        return extractor.gather_raw(card, side, None)  # type: ignore[arg-type]

    def selected_text(self) -> str:
        try:
            return mw.reviewer.web.selectedText() or ""
        except Exception:
            return ""

    # -- threading -------------------------------------------------------------
    def run_in_background(self, fn: Callable[[], None]) -> None:
        def on_done(fut: Any) -> None:
            try:
                fut.result()
            except Exception:
                log.exception("background task failed")

        mw.taskman.run_in_background(fn, on_done, uses_collection=False)

    def start_timer(self, ms: int, cb: Callable[[], None]) -> QTimer:
        timer = QTimer(mw)
        timer.timeout.connect(cb)
        timer.start(ms)
        return timer

    # -- actions -------------------------------------------------------------------
    def flip_card(self) -> bool:
        reviewer = mw.reviewer
        if reviewer is None or getattr(reviewer, "state", None) != "question":
            return False
        try:
            if hasattr(reviewer, "_getTypedAnswer"):
                reviewer._getTypedAnswer()  # captures a typed answer, then shows the answer
                return True
            if hasattr(reviewer, "_showAnswer"):
                reviewer._showAnswer()
                return True
        except Exception:
            log.exception("flip via private API failed; falling back")
        try:
            reviewer.onEnterKey()  # same as pressing Space
            return True
        except Exception:
            log.exception("flip failed")
            return False

    def focus_reviewer(self) -> None:
        try:
            if not mw.isActiveWindow():
                mw.activateWindow()
            mw.web.setFocus()
        except Exception:
            pass

    def open_settings(self, page: str | None = None) -> None:
        self._open_settings(page)

    def open_link(self, url: str) -> None:
        openLink(url)

    def copy_text(self, text: str) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def edit_card(self, card_id: int) -> None:
        card = self.current_card()
        if card is not None and card.id == card_id:
            mw.onEditCurrent()
        else:
            self.open_browser(card_id)

    def open_browser(self, card_id: int) -> None:
        try:
            browser = aqt.dialogs.open("Browser", mw)
            browser.search_for(f"cid:{card_id}")
        except Exception:
            log.exception("open browser failed")

    def profile_name(self) -> str:
        try:
            return mw.pm.name or ""
        except Exception:
            return ""

    def tooltip(self, msg: str) -> None:
        tooltip(msg, parent=mw)

    def show_model_menu(self) -> None:
        menu = QMenu(mw)
        current = self.settings.model
        for model in all_models():
            action = menu.addAction(model.display_name)
            action.setCheckable(True)
            action.setChecked(model.id == current)
            action.triggered.connect(lambda _=False, mid=model.id: self._pick_model(mid))
        menu.addSeparator()
        more = menu.addAction("More models are coming in a future version")
        more.setEnabled(False)
        menu.addAction("Model settings…").triggered.connect(lambda: self._open_settings("model"))
        menu.popup(QCursor.pos())

    def _pick_model(self, model_id: str) -> None:
        if self.settings.model != model_id:
            self.settings.set("model", model_id)
            self.settings.save()

    def update_bar(self, state: dict[str, Any]) -> None:
        reviewer = mw.reviewer
        if reviewer is None or mw.state != "review":
            return
        try:
            reviewer.bottom.web.eval(f"window.ExplainerBar && ExplainerBar.update({json.dumps(state)});")
        except Exception:
            log.exception("bottom bar update failed")

    def shortcut_label(self, name: str) -> str:
        sc = self.settings.data["shortcuts"]
        if name.startswith("chip"):
            idx = int(name[4:])
            chips = sc.get("chips", [])
            return key_label(chips[idx]) if idx < len(chips) else ""
        return key_label(sc.get(name, ""))
