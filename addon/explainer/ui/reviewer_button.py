"""Reviewer integration: bottom-bar button, shortcuts, context menu (§4.1).

* The Explain/Hint button is injected into the reviewer's bottom bar via
  ``webview_will_set_content`` and talks back with ``pycmd("explainer:…")``.
* ``X`` and ``Shift+X`` are registered for the ``review`` state only, so they
  never interfere with the editor, browser or deck list. At registration the
  keys are checked against Anki's reviewer shortcuts and other add-ons'; on a
  conflict the add-on warns once, suggests an alternative and leaves the
  existing binding alone.
* Panel keys (Esc, Ctrl+., Alt+1…6) are separate ``QShortcut`` objects that
  are only enabled while reviewing with the panel open, so Esc keeps its usual
  meaning everywhere else.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Sequence

from aqt import mw
from aqt.qt import QKeySequence, QMenu, QShortcut, Qt
from aqt.utils import showInfo

from ..util.log import get_logger

log = get_logger("reviewer_button")

CANDIDATES = ["X", "H", "J", "K", "Q", "W", "G", "Y", "Z", "Shift+E", "Ctrl+Shift+X", "F2"]


def norm(key: Any) -> str:
    try:
        seq = QKeySequence(key)
    except TypeError:
        try:
            seq = QKeySequence(int(key))
        except Exception:
            return str(key).lower()
    return seq.toString(QKeySequence.SequenceFormat.PortableText).lower()


def inject_bottom_bar(web_content: Any, package: str) -> None:
    web_content.css.append(f"/_addons/{package}/web/bottombar.css")
    web_content.js.append(f"/_addons/{package}/web/bottombar.js")


class ShortcutManager:
    def __init__(self, settings: Any, state_file: str) -> None:
        self.settings = settings
        self.state_file = state_file
        self._panel_shortcuts: list[QShortcut] = []
        self._dock_shortcuts: list[QShortcut] = []
        self.conflicts: dict[str, str] = {}

    # -- state shortcuts (X, Shift+X) ---------------------------------------
    def extend_review_shortcuts(
        self,
        shortcuts: list[tuple[Any, Callable]],
        on_explain: Callable[[], None],
        on_focus: Callable[[], None],
    ) -> None:
        taken = {norm(k) for k, _ in shortcuts}
        self.conflicts = {}
        for name, fn in (("explain", on_explain), ("focus_input", on_focus)):
            key = self.settings.get(f"shortcuts.{name}") or ""
            if not key:
                continue
            if norm(key) in taken:
                self.conflicts[name] = key
                continue
            shortcuts.append((key, fn))
            taken.add(norm(key))
        if self.conflicts:
            self._warn_once(taken)

    def _warn_once(self, taken: set[str]) -> None:
        state = self._load_state()
        sig = json.dumps(sorted(self.conflicts.items()))
        if state.get("warned_conflicts") == sig:
            return
        state["warned_conflicts"] = sig
        self._save_state(state)
        suggestion = next((c for c in CANDIDATES if norm(c) not in taken), "")
        lines = [
            f"The key {key} for “{'Explain' if name == 'explain' else 'Focus follow-up'}” is already used "
            "by Anki or another add-on, so Anki Explainer didn't register it."
            for name, key in self.conflicts.items()
        ]
        if suggestion:
            lines.append(f"Suggestion: use {suggestion} instead (Tools → Anki Explainer Settings… → Shortcuts).")
        lines.append("You can still use the Explain button in the bottom bar.")
        mw.progress.single_shot(500, lambda: showInfo("\n\n".join(lines), title="Anki Explainer"))

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except OSError:
            pass

    # -- panel shortcuts (Esc, Ctrl+., Alt+1…6) -----------------------------------
    def build_panel_shortcuts(
        self,
        dock: Any,
        on_escape: Callable[[], None],
        on_stop: Callable[[], None],
        on_chip: Callable[[int], None],
    ) -> None:
        self.teardown()
        bindings: list[tuple[str, Callable[[], None]]] = []
        sc_cfg = self.settings.data["shortcuts"]
        if sc_cfg.get("focus_reviewer"):
            bindings.append((sc_cfg["focus_reviewer"], on_escape))
        if sc_cfg.get("stop"):
            bindings.append((sc_cfg["stop"], on_stop))
        for i, key in enumerate(sc_cfg.get("chips", [])[:6]):
            if key:
                bindings.append((key, lambda i=i: on_chip(i)))
        for key, fn in bindings:
            main = QShortcut(QKeySequence(key), mw)
            main.setContext(Qt.ShortcutContext.WindowShortcut)
            main.setAutoRepeat(False)
            main.activated.connect(fn)
            main.setEnabled(False)
            self._panel_shortcuts.append(main)
            floating = QShortcut(QKeySequence(key), dock)
            floating.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            floating.setAutoRepeat(False)
            floating.activated.connect(fn)
            floating.setEnabled(False)
            self._dock_shortcuts.append(floating)

    def teardown(self) -> None:
        for sc in self._panel_shortcuts + self._dock_shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._panel_shortcuts, self._dock_shortcuts = [], []

    def update_enabled(self, reviewing: bool, panel_visible: bool, floating: bool) -> None:
        active = reviewing and panel_visible
        for sc in self._panel_shortcuts:
            sc.setEnabled(active and not floating)
        # A floating panel is its own window: give it its own copies (never both
        # at once, or Qt treats the key as ambiguous).
        for sc in self._dock_shortcuts:
            sc.setEnabled(active and floating)


def reviewer_shortcut_keys() -> list[str]:
    """Keys Anki's reviewer uses (for live conflict detection in settings)."""
    try:
        keys: Sequence[tuple[Any, Any]] = mw.reviewer._shortcutKeys()
        return [norm(k) for k, _ in keys]
    except Exception:
        return [norm(k) for k in ("e", " ", "m", "r", "*", "=", "-", "!", "@", "v", "o", "i", "u", "1", "2", "3", "4", "5", "6", "7")]


def add_menu_items(menu: QMenu, selection: str, on_explain: Callable[[], None], on_selection: Callable[[str], None]) -> None:
    menu.addSeparator()
    explain = menu.addAction("Explain this card")
    explain.triggered.connect(lambda: on_explain())
    sel = (selection or "").strip()
    if sel:
        short = sel if len(sel) <= 30 else sel[:29] + "…"
        action = menu.addAction(f"Explain selection: “{short}”")
        action.triggered.connect(lambda: on_selection(sel))
