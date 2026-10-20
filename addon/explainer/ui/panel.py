"""The Explainer panel: a ``QDockWidget`` holding its own ``AnkiWebView`` (§4.2).

* Never injected into the card, so card templates/CSS/JS are unaffected.
* Docked right by default (left or floating are options); width remembered
  (default 380 px, min 300, max 50 % of the window).
* On windows narrower than 900 px it switches to a floating, always-on-top
  panel anchored to the right edge of the main window.
* Never steals grading keys: while the follow-up box is focused the panel
  claims key presses (so typing "1" or "x" types), otherwise Anki's shortcuts
  keep working even if the panel has focus.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from aqt import mw
from aqt.qt import (
    QDockWidget,
    QEvent,
    QObject,
    QRect,
    Qt,
    QTimer,
    QWidget,
)
from aqt.theme import theme_manager
from aqt.webview import AnkiWebView

from ..util.log import get_logger

log = get_logger("panel")

MIN_WIDTH = 300
NARROW_WINDOW = 900
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web")


class _KeyClaimFilter(QObject):
    """While the panel's text input has focus, claim every key press so that
    window-wide reviewer shortcuts (grading keys, X, …) don't fire while the
    student is typing a question."""

    def __init__(self, panel: "ExplainerPanel") -> None:
        super().__init__(panel.dock)
        self.panel = panel

    def eventFilter(self, obj: QObject | None, evt: QEvent | None) -> bool:  # noqa: N802
        if evt is not None and evt.type() == QEvent.Type.ShortcutOverride and self.panel.input_focused:
            evt.accept()
        return False


class _WindowWatcher(QObject):
    def __init__(self, panel: "ExplainerPanel") -> None:
        super().__init__(mw)
        self.panel = panel

    def eventFilter(self, obj: QObject | None, evt: QEvent | None) -> bool:  # noqa: N802
        if evt is not None and evt.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self.panel._on_window_geometry()
        return False


class ExplainerPanel:
    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], Any],
        settings: Any,
        addon_package: str,
    ) -> None:
        self._on_message = on_message
        self.settings = settings
        self.addon_package = addon_package
        self.input_focused = False
        self._auto_floating = False
        self._filter_installed = False

        self.dock = QDockWidget("Explainer", mw)
        self.dock.setObjectName("AnkiExplainerDock")
        self.dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self._empty_title = QWidget(self.dock)
        self.dock.setTitleBarWidget(self._empty_title)
        self.dock.setMinimumWidth(MIN_WIDTH)

        self.web = AnkiWebView(parent=self.dock, title="anki explainer")
        self.web.setMinimumWidth(MIN_WIDTH)
        self.web.set_bridge_command(self._on_bridge, self)
        self.dock.setWidget(self.web)

        area = (
            Qt.DockWidgetArea.LeftDockWidgetArea
            if settings.behavior("panel_position") == "left"
            else Qt.DockWidgetArea.RightDockWidgetArea
        )
        mw.addDockWidget(area, self.dock)
        self.dock.hide()
        self.dock.visibilityChanged.connect(self._on_visibility)
        self.dock.topLevelChanged.connect(self._on_top_level_changed)
        self.dock.dockLocationChanged.connect(self._on_location_changed)

        self._save_timer = QTimer(self.dock)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_width)
        self._watcher = _WindowWatcher(self)
        mw.installEventFilter(self._watcher)
        self._resize_watch = _ResizeWatch(self)
        self.dock.installEventFilter(self._resize_watch)

        self._load_page()

    # ------------------------------------------------------------------
    # page
    # ------------------------------------------------------------------
    def _url(self, name: str) -> str:
        return f"/_addons/{self.addon_package}/web/{name}"

    def _load_page(self) -> None:
        with open(os.path.join(WEB_DIR, "panel.html"), encoding="utf-8") as f:
            body = f.read()
        self.web.stdHtml(
            body,
            css=[self._url("panel.css")],
            js=[
                self._url("vendor/purify.min.js"),
                self._url("vendor/marked.min.js"),
                self._url("panel.js"),
            ],
            context=self,
        )

    def _eval(self, js: str) -> None:
        try:
            self.web.eval(js)
        except Exception:
            log.exception("panel eval failed")

    def _call(self, fn: str, *args: Any) -> None:
        payload = ", ".join(json.dumps(a, ensure_ascii=False, default=str) for a in args)
        self._eval(f"window.Explainer && Explainer.{fn}({payload});")

    def _on_bridge(self, cmd: str) -> Any:
        if not cmd.startswith("explainer:"):
            return None
        try:
            msg = json.loads(cmd[len("explainer:"):])
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None
        op = msg.get("op")
        if op == "ready":
            self._install_key_filter()
        elif op == "input_focus":
            self.input_focused = bool(msg.get("focused"))
            self._install_key_filter()
        try:
            return self._on_message(msg)
        except Exception:
            log.exception("panel message failed: %s", op)
            return None

    def _install_key_filter(self) -> None:
        if self._filter_installed:
            return
        proxy = self.web.focusProxy()
        if proxy is None:
            return
        self._key_filter = _KeyClaimFilter(self)
        proxy.installEventFilter(self._key_filter)
        self._filter_installed = True

    # ------------------------------------------------------------------
    # PanelView API used by the controller
    # ------------------------------------------------------------------
    def is_visible(self) -> bool:
        return self.dock.isVisible()

    def show(self) -> None:
        if self.dock.isVisible():
            return
        self._apply_position()
        self.dock.show()
        self.dock.raise_()
        self._restore_width()

    def hide(self) -> None:
        if self.web.hasFocus() or (self.web.focusProxy() and self.web.focusProxy().hasFocus()):
            mw.web.setFocus()
        self.input_focused = False
        self.dock.hide()

    def has_focus(self) -> bool:
        proxy = self.web.focusProxy()
        return bool(self.web.hasFocus() or (proxy is not None and proxy.hasFocus()))

    def set_state(self, state: dict[str, Any]) -> None:
        # AnkiWebView queues evals until the page reports domDone, so state can
        # be pushed before the panel script has said "ready".
        self._call("setState", state)

    def set_phase(self, thread_id: str, phase: str, info: dict[str, Any]) -> None:
        self._call("setPhase", thread_id, phase, info or {})

    def append_delta(self, thread_id: str, text: str, kind: str) -> None:
        self._call("appendDelta", thread_id, text, kind)

    def finish(self, thread_id: str, meta: dict[str, Any]) -> None:
        self._call("finish", thread_id, meta)

    def error(self, thread_id: str, err: dict[str, Any]) -> None:
        self._call("error", thread_id, err)

    def set_theme(self, dark: bool) -> None:
        self._call("setTheme", bool(dark))

    def focus_input(self) -> None:
        self.web.setFocus()
        self._call("focusInput")

    # ------------------------------------------------------------------
    # geometry: position, width, narrow windows
    # ------------------------------------------------------------------
    def _apply_position(self) -> None:
        pos = self.settings.behavior("panel_position")
        if pos == "floating" or self._should_auto_float():
            if not self.dock.isFloating():
                self.dock.setFloating(True)
            if pos != "floating":
                self._auto_floating = True
            self._place_floating()
            return
        self._auto_floating = False
        if self.dock.isFloating():
            self.dock.setFloating(False)
        area = Qt.DockWidgetArea.LeftDockWidgetArea if pos == "left" else Qt.DockWidgetArea.RightDockWidgetArea
        if mw.dockWidgetArea(self.dock) != area:
            mw.removeDockWidget(self.dock)
            mw.addDockWidget(area, self.dock)

    def _should_auto_float(self) -> bool:
        return mw.width() < NARROW_WINDOW

    def _place_floating(self) -> None:
        width = self._target_width()
        geo = mw.frameGeometry()
        height = max(400, int(mw.height() * 0.85))
        y = geo.top() + max(40, (mw.height() - height) // 2)
        screen = mw.screen().availableGeometry() if mw.screen() else None
        if screen is not None and geo.right() + 8 + width <= screen.right():
            x = geo.right() + 8  # beside the window: the card stays fully visible
        else:
            x = geo.right() - width - 12  # anchored to the right edge, on top
        if self._auto_floating:
            self.dock.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.dock.setGeometry(QRect(x, y, width, height))
        if self.dock.isVisible() or self._auto_floating:
            self.dock.show()

    def _target_width(self) -> int:
        width = int(self.settings.behavior("panel_width") or 380)
        return max(MIN_WIDTH, min(width, int(mw.width() * 0.5) if not self.dock.isFloating() else 900))

    def _restore_width(self) -> None:
        if self.dock.isFloating():
            return
        width = self._target_width()
        self.dock.setMaximumWidth(max(MIN_WIDTH, int(mw.width() * 0.5)))
        QTimer.singleShot(0, lambda: mw.resizeDocks([self.dock], [width], Qt.Orientation.Horizontal))

    def _on_window_geometry(self) -> None:
        if not self.dock.isFloating():
            self.dock.setMaximumWidth(max(MIN_WIDTH, int(mw.width() * 0.5)))
        if not self.dock.isVisible():
            return
        pos = self.settings.behavior("panel_position")
        if pos == "floating":
            return
        if self._should_auto_float() and not self.dock.isFloating():
            self._auto_floating = True
            self.dock.setFloating(True)
            self._place_floating()
        elif self._auto_floating and not self._should_auto_float():
            self._auto_floating = False
            self.dock.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
            self.dock.setFloating(False)
            self.dock.show()
            self._restore_width()
        elif self._auto_floating:
            self._place_floating()

    def _on_top_level_changed(self, floating: bool) -> None:
        # Our own title bar lives in the webview; floating panels need the
        # native one so they can be moved.
        self.dock.setTitleBarWidget(None if floating else self._empty_title)

    def _on_location_changed(self, area: Qt.DockWidgetArea) -> None:
        if self._auto_floating or self.dock.isFloating():
            return
        pos = "left" if area == Qt.DockWidgetArea.LeftDockWidgetArea else "right"
        if self.settings.behavior("panel_position") != pos:
            self.settings.set("behavior.panel_position", pos)
            self.settings.save()

    def _on_visibility(self, visible: bool) -> None:
        if not visible:
            self.input_focused = False

    def _on_dock_resized(self) -> None:
        if self.dock.isVisible() and not self.dock.isFloating():
            self._save_timer.start()

    def _save_width(self) -> None:
        width = self.dock.width()
        if MIN_WIDTH <= width and width != self.settings.behavior("panel_width"):
            self.settings.set("behavior.panel_width", int(width))
            self.settings.save()

    def apply_settings(self) -> None:
        if self.dock.isVisible():
            self.dock.hide()
            self._apply_position()
            self.dock.show()
            self._restore_width()

    def cleanup(self) -> None:
        """Tear the panel down completely (profile close / switch)."""
        try:
            mw.removeEventFilter(self._watcher)
            self._save_timer.stop()
            self.web.cleanup()
            self.dock.hide()
            mw.removeDockWidget(self.dock)
            self.dock.deleteLater()
        except Exception:
            log.exception("panel cleanup failed")


class _ResizeWatch(QObject):
    def __init__(self, panel: ExplainerPanel) -> None:
        super().__init__(panel.dock)
        self.panel = panel

    def eventFilter(self, obj: QObject | None, evt: QEvent | None) -> bool:  # noqa: N802
        if evt is not None and evt.type() == QEvent.Type.Resize:
            self.panel._on_dock_resized()
        return False


def is_dark() -> bool:
    return bool(theme_manager.night_mode)
