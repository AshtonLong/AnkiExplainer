"""All ``gui_hooks`` registration in one place.

Import cost is kept tiny: this module only defines callbacks. The services
(settings, cache, HTTP client, controller, panel) are created lazily after the
profile opens, and nothing touches the network until the user asks.
"""

from __future__ import annotations

import os
from typing import Any

from aqt import gui_hooks, mw
from aqt.qt import QAction

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_FILES = os.path.join(ADDON_DIR, "user_files")
PANEL_PRELOAD_MS = 1200


class Services:
    """Lazily-built singletons for the current profile."""

    def __init__(self, module: str) -> None:
        from .store.cache import Cache
        from .store.secrets import SecretStore
        from .store.settings import Settings
        from .ui.reviewer_button import ShortcutManager
        from .util import log

        self.module = module
        self.package = mw.addonManager.addonFromModule(module)
        log.configure(USER_FILES)
        self.settings = Settings(mw.addonManager.getConfig(module) or {}, writer=self._write_config)
        self.secrets = SecretStore(USER_FILES)
        self.cache = Cache(os.path.join(USER_FILES, "cache.sqlite3"))
        self.shortcuts = ShortcutManager(self.settings, os.path.join(USER_FILES, "state.json"))
        self.controller: Any = None
        self.panel: Any = None
        self._build_controller()

    def _write_config(self, data: dict[str, Any]) -> None:
        mw.addonManager.writeConfig(self.module, data)

    def _build_controller(self) -> None:
        from .controller import Controller
        from .ui.anki_host import AnkiHost

        self.host = AnkiHost(self.settings, self.open_settings)
        self.controller = Controller(self.host, self.settings, self.secrets, self.cache)

    def ensure_panel(self) -> Any:
        if self.panel is None:
            from .ui.panel import ExplainerPanel

            self.panel = ExplainerPanel(self.controller.handle_message, self.settings, self.package)
            self.controller.attach_panel(self.panel)
            self.shortcuts.build_panel_shortcuts(
                self.panel.dock,
                on_escape=self.on_escape,
                on_stop=self.controller.stop_pressed,
                on_chip=self.controller.chip_shortcut,
            )
            self.panel.dock.visibilityChanged.connect(lambda _v: self.update_shortcuts())
            self.panel.dock.topLevelChanged.connect(lambda _v: self.update_shortcuts())
            self.update_shortcuts()
        return self.panel

    def update_shortcuts(self) -> None:
        if self.panel is None:
            return
        self.shortcuts.update_enabled(
            reviewing=mw.state == "review",
            panel_visible=self.panel.dock.isVisible(),
            floating=self.panel.dock.isFloating(),
        )

    def on_escape(self) -> None:
        handled = self.controller.escape_pressed()
        if not handled:
            try:
                mw.web.onEsc()  # Anki's default: leave the type-in box
            except Exception:
                pass

    def open_settings(self, page: str | None = None) -> None:
        from .ui.settings_dialog import open_dialog

        open_dialog(self, page)

    def on_settings_saved(self) -> None:
        if self.panel is not None:
            self.shortcuts.build_panel_shortcuts(
                self.panel.dock,
                on_escape=self.on_escape,
                on_stop=self.controller.stop_pressed,
                on_chip=self.controller.chip_shortcut,
            )
            self.update_shortcuts()
            self.panel.apply_settings()
        if mw.state == "review" and mw.reviewer is not None:
            try:
                mw.clearStateShortcuts()
                mw.setStateShortcuts(mw.reviewer._shortcutKeys())  # re-run our shortcut hook
            except Exception:
                pass
        self.controller.on_settings_changed()

    def shutdown(self) -> None:
        try:
            self.controller.shutdown()
        finally:
            self.shortcuts.teardown()
            if self.panel is not None:
                self.panel.cleanup()
                self.panel = None


_services: Services | None = None
_module = __name__


def services() -> Services:
    global _services
    if _services is None:
        _services = Services(_module)
    return _services


def _explain(selection: str | None = None, source: str = "key") -> None:
    s = services()
    s.ensure_panel()
    if selection:
        s.controller.explain_selection(selection)
    else:
        s.controller.explain_pressed(source=source)


def _focus_input() -> None:
    s = services()
    s.ensure_panel()
    s.controller.focus_input_pressed()


# --- hook callbacks ---------------------------------------------------------------


def on_profile_open() -> None:
    s = services()
    # Build the panel shortly after startup, off the critical path, so the first
    # X press shows it instantly. This loads local files only.
    mw.progress.single_shot(PANEL_PRELOAD_MS, s.ensure_panel)


def on_profile_close() -> None:
    global _services
    if _services is not None:
        _services.shutdown()
        _services = None


def on_show_question(card: Any) -> None:
    if _services is not None:
        _services.controller.on_card_shown(card, "question")


def on_show_answer(card: Any) -> None:
    if _services is not None:
        _services.controller.on_card_shown(card, "answer")


def on_reviewer_end() -> None:
    if _services is not None:
        _services.controller.on_reviewer_end()


def on_answer_card(reviewer: Any, card: Any, ease: int) -> None:
    if _services is not None:
        _services.controller.on_card_answered(card.id, ease)


def on_state_shortcuts(state: str, shortcuts: list[tuple[str, Any]]) -> None:
    if state != "review":
        return
    s = services()
    s.shortcuts.extend_review_shortcuts(shortcuts, on_explain=_explain, on_focus=_focus_input)


def on_state_did_change(new_state: str, old_state: str) -> None:
    if _services is not None:
        _services.update_shortcuts()


def on_webview_will_set_content(web_content: Any, context: Any) -> None:
    from aqt.reviewer import ReviewerBottomBar

    if isinstance(context, ReviewerBottomBar):
        from .ui.reviewer_button import inject_bottom_bar

        inject_bottom_bar(web_content, mw.addonManager.addonFromModule(_module))


def on_js_message(handled: tuple[bool, Any], message: str, context: Any) -> tuple[bool, Any]:
    if not message.startswith("explainer:"):
        return handled
    from aqt.reviewer import ReviewerBottomBar

    if isinstance(context, ReviewerBottomBar):
        if message == "explainer:explain":
            _explain(source="button")
        return (True, None)
    return handled


def on_reviewer_menu(reviewer: Any, menu: Any) -> None:
    from .ui.reviewer_button import add_menu_items

    selection = ""
    try:
        selection = reviewer.web.selectedText()
    except Exception:
        pass
    add_menu_items(menu, selection, lambda: _explain(source="menu"), lambda sel: _explain(selection=sel))


def on_webview_menu(webview: Any, menu: Any) -> None:
    if mw.state != "review" or mw.reviewer is None or webview is not mw.reviewer.web:
        return
    on_reviewer_menu(mw.reviewer, menu)


def on_theme_change() -> None:
    if _services is not None and _services.panel is not None:
        from aqt.theme import theme_manager

        _services.panel.set_theme(theme_manager.night_mode)


def on_main_window_init() -> None:
    action = QAction("Anki Explainer Settings…", mw)
    action.triggered.connect(lambda: services().open_settings())
    mw.form.menuTools.addAction(action)


def register(module: str) -> None:
    global _module
    _module = module
    mw.addonManager.setWebExports(module, r"web/.*\.(js|css|html|svg|png)")
    mw.addonManager.setConfigAction(module, lambda: services().open_settings())
    gui_hooks.main_window_did_init.append(on_main_window_init)
    gui_hooks.profile_did_open.append(on_profile_open)
    gui_hooks.profile_will_close.append(on_profile_close)
    gui_hooks.reviewer_did_show_question.append(on_show_question)
    gui_hooks.reviewer_did_show_answer.append(on_show_answer)
    gui_hooks.reviewer_will_end.append(on_reviewer_end)
    gui_hooks.reviewer_did_answer_card.append(on_answer_card)
    gui_hooks.state_shortcuts_will_change.append(on_state_shortcuts)
    gui_hooks.state_did_change.append(on_state_did_change)
    gui_hooks.webview_will_set_content.append(on_webview_will_set_content)
    gui_hooks.webview_did_receive_js_message.append(on_js_message)
    gui_hooks.reviewer_will_show_context_menu.append(on_reviewer_menu)
    gui_hooks.webview_will_show_context_menu.append(on_webview_menu)
    gui_hooks.theme_did_change.append(on_theme_change)
