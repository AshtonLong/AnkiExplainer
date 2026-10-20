"""Settings dialog (spec §6): a native ``QDialog`` with a page list.

Changes apply on **Save**; every page has "Reset page to defaults". Pages
bind widgets to config paths, so load/save/reset are uniform.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from aqt import mw
from aqt.qt import (
    QAbstractItemView,
    QCheckBox,
    QColor,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QKeySequence,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    Qt,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import askUser, openLink, tooltip

from ..llm import prompts
from ..llm.models import all_models, get_model
from ..llm.openrouter import (
    CREDITS_URL,
    KEYS_URL,
    PRIVACY_SETTINGS_URL,
    PROVIDER_SETTINGS_URL,
    ApiError,
    OpenRouterClient,
    describe_key_info,
)
from ..store.secrets import mask
from ..store.settings import EFFORT_LABELS, EFFORTS
from .reviewer_button import norm, reviewer_shortcut_keys

PAGES = [
    ("account", "Account"),
    ("model", "Model"),
    ("reasoning", "Reasoning"),
    ("output", "Output"),
    ("behavior", "Behavior"),
    ("chips", "Learning moves"),
    ("shortcuts", "Shortcuts"),
    ("context", "Card context & cache"),
    ("prompts", "Prompts (advanced)"),
    ("usage", "Usage"),
]

LANGUAGES = [
    ("same_as_card", "Same as card"), ("English", "English"), ("Spanish", "Spanish"),
    ("French", "French"), ("German", "German"), ("Portuguese", "Portuguese"), ("Italian", "Italian"),
    ("Dutch", "Dutch"), ("Polish", "Polish"), ("Russian", "Russian"), ("Ukrainian", "Ukrainian"),
    ("Turkish", "Turkish"), ("Arabic", "Arabic"), ("Hebrew", "Hebrew"), ("Hindi", "Hindi"),
    ("Chinese (Simplified)", "Chinese (Simplified)"), ("Chinese (Traditional)", "Chinese (Traditional)"),
    ("Japanese", "Japanese"), ("Korean", "Korean"), ("Vietnamese", "Vietnamese"), ("Indonesian", "Indonesian"),
]


def _get(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        node = node[part]
    return node


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(placeholder-text); font-size: 90%;")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    label.setOpenExternalLinks(False)
    label.linkActivated.connect(openLink)
    return label


def _link(text: str, url: str) -> QLabel:
    label = QLabel(f'<a href="{url}">{text}</a>')
    label.linkActivated.connect(openLink)
    return label


class Page(QWidget):
    def __init__(self, dialog: "SettingsDialog", key: str, title: str) -> None:
        super().__init__()
        self.dialog = dialog
        self.key = key
        self.bindings: list[tuple[str, Callable[[], Any], Callable[[Any], None]]] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.layout_ = QVBoxLayout(inner)
        self.layout_.setContentsMargins(16, 12, 16, 12)
        heading = QLabel(f"<h3 style='margin:0'>{title}</h3>")
        self.layout_.addWidget(heading)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(16, 4, 16, 8)
        bottom.addStretch(1)
        reset = QPushButton("Reset page to defaults")
        reset.clicked.connect(self.reset)
        bottom.addWidget(reset)
        outer.addLayout(bottom)

    def form(self) -> QFormLayout:
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.layout_.addLayout(form)
        return form

    def bind(self, path: str, getter: Callable[[], Any], setter: Callable[[Any], None]) -> None:
        self.bindings.append((path, getter, setter))

    def load(self, data: dict[str, Any]) -> None:
        for path, _, setter in self.bindings:
            setter(_get(data, path))
        self.after_load()

    def store(self, data: dict[str, Any]) -> None:
        for path, getter, _ in self.bindings:
            _set(data, path, getter())

    def reset(self) -> None:
        defaults = self.dialog.settings.defaults
        for path, _, setter in self.bindings:
            setter(copy.deepcopy(_get(defaults, path)))
        self.after_load()
        self.on_reset()

    def after_load(self) -> None:
        pass

    def on_reset(self) -> None:
        pass

    # widget helpers --------------------------------------------------------
    def check(self, path: str, label: str) -> QCheckBox:
        box = QCheckBox(label)
        self.bind(path, box.isChecked, lambda v: box.setChecked(bool(v)))
        return box

    def combo(self, path: str, options: list[tuple[Any, str]]) -> QComboBox:
        box = QComboBox()
        for value, label in options:
            box.addItem(label, value)

        def setter(v: Any) -> None:
            idx = box.findData(v)
            box.setCurrentIndex(idx if idx >= 0 else 0)

        self.bind(path, box.currentData, setter)
        return box

    def spin(self, path: str, lo: int, hi: int, suffix: str = "") -> QSpinBox:
        box = QSpinBox()
        box.setRange(lo, hi)
        if suffix:
            box.setSuffix(suffix)
        self.bind(path, box.value, lambda v: box.setValue(int(v)))
        return box


class SettingsDialog(QDialog):
    def __init__(self, services: Any, page: str | None = None) -> None:
        super().__init__(mw)
        self.services = services
        self.settings = services.settings
        self.data = self.settings.snapshot()
        self.setWindowTitle("Anki Explainer Settings")
        self.setMinimumSize(780, 560)
        self.pages: list[Page] = []

        layout = QVBoxLayout(self)
        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setFixedWidth(190)
        self.stack = QStackedWidget()
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        layout.addLayout(body, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        builders = {
            "account": self._account,
            "model": self._model,
            "reasoning": self._reasoning,
            "output": self._output,
            "behavior": self._behavior,
            "chips": self._chips,
            "shortcuts": self._shortcuts,
            "context": self._context,
            "prompts": self._prompts,
            "usage": self._usage,
        }
        for key, title in PAGES:
            widget = Page(self, key, title)
            builders[key](widget)
            widget.layout_.addStretch(1)
            widget.load(self.data)
            self.pages.append(widget)
            self.stack.addWidget(widget)
            self.nav.addItem(QListWidgetItem(title))
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        keys = [k for k, _ in PAGES]
        self.nav.setCurrentRow(keys.index(page) if page in keys else 0)

    # ------------------------------------------------------------------
    def save(self) -> None:
        for page in self.pages:
            page.store(self.data)
        if self._shortcut_errors():
            self.nav.setCurrentRow([k for k, _ in PAGES].index("shortcuts"))
            if not askUser("Some shortcuts conflict with Anki's keys or each other. Save anyway?", parent=self):
                return
        new_key = self.key_edit.text().strip()
        if new_key:
            self.services.secrets.set(new_key)
        self.settings.replace(self.data)
        self.settings.save()
        self.services.on_settings_saved()
        self.accept()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def _account(self, page: Page) -> None:
        secrets = self.services.secrets
        saved = secrets.get()
        form = page.form()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(f"Saved: {mask(saved)}" if saved else "sk-or-v1-…")
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(
            lambda on: (self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password),
                        show.setText("Hide" if on else "Show"))
        )
        row = QHBoxLayout()
        row.addWidget(self.key_edit, 1)
        row.addWidget(show)
        form.addRow("OpenRouter API key", row)
        self.key_status = QLabel()
        self.key_status.setWordWrap(True)
        test = QPushButton("Test connection")
        test.clicked.connect(self._test_key)
        remove = QPushButton("Remove key")
        remove.setEnabled(bool(saved))
        remove.clicked.connect(lambda: self._remove_key(remove))
        buttons = QHBoxLayout()
        buttons.addWidget(test)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        form.addRow("", buttons)
        form.addRow("", self.key_status)
        links = QHBoxLayout()
        links.addWidget(_link("Get a key", KEYS_URL))
        links.addWidget(_link("Add credits", CREDITS_URL))
        links.addStretch(1)
        form.addRow("", links)
        page.layout_.addWidget(_hint(
            "Your key is stored only on this computer (the OS keyring when available, otherwise the "
            "add-on's user_files folder). It is never synced through AnkiWeb and is sent only to "
            "openrouter.ai over HTTPS. After saving, only the last four characters are shown."
        ))
        page.on_reset = lambda: self.key_edit.clear()  # type: ignore[method-assign]

    def _test_key(self) -> None:
        key = self.key_edit.text().strip() or self.services.secrets.get() or ""
        if not key:
            self.key_status.setText("Paste a key first.")
            return
        self.key_status.setText("Checking…")
        client = OpenRouterClient(key)
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["info"] = client.key_info()
            except ApiError as err:
                result["error"] = err
            except Exception:
                result["error"] = None

        def done(_fut: Any) -> None:
            if "info" in result:
                self.key_status.setText("✓ Connected.\n" + describe_key_info(result["info"]))
                return
            err = result.get("error")
            if isinstance(err, ApiError):
                guidance = {
                    "auth": "OpenRouter didn't accept this key. Copy it again from openrouter.ai/keys; it starts with sk-or-.",
                    "credits": "The key works, but the account has no credits. Add credits on openrouter.ai.",
                    "offline": "Couldn't reach openrouter.ai. Check your internet connection or proxy.",
                    "timeout": "OpenRouter took too long to answer. Try again in a moment.",
                    "rate_limit": "Rate limited. Wait a few seconds and test again.",
                }.get(err.kind, err.message)
                self.key_status.setText("✗ " + guidance)
            else:
                self.key_status.setText("✗ Something went wrong. See the add-on log.")

        mw.taskman.run_in_background(work, done, uses_collection=False)

    def _remove_key(self, button: QPushButton) -> None:
        if not askUser("Remove the saved OpenRouter key from this computer?", parent=self):
            return
        self.services.secrets.remove()
        self.key_edit.clear()
        self.key_edit.setPlaceholderText("sk-or-v1-…")
        self.key_status.setText("Key removed.")
        button.setEnabled(False)
        self.services.on_settings_saved()

    # ------------------------------------------------------------------
    # Model
    # ------------------------------------------------------------------
    def _model(self, page: Page) -> None:
        form = page.form()
        models = [(m.id, m.display_name) for m in all_models()]
        box = page.combo("model", models)
        form.addRow("Model", box)
        caps = QLabel()
        caps.setWordWrap(True)

        def update() -> None:
            m = get_model(box.currentData())
            bits = ["text input"]
            if m.vision:
                bits.append("image input")
            if m.reasoning:
                bits.append("reasoning")
            caps.setText(f"OpenRouter ID: <code>{m.id}</code><br>Capabilities: {', '.join(bits)}")

        box.currentIndexChanged.connect(lambda _i: update())
        update()
        form.addRow("", caps)
        page.layout_.addWidget(_hint(
            "This alias always routes to the newest DeepSeek Flash release, so upgrades happen "
            "automatically without an add-on update. More models will be added in a future version. "
            "The model chip in the panel header opens this list too."
        ))

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------
    def _reasoning(self, page: Page) -> None:
        supported = get_model(self.data["model"]).reasoning
        form = page.form()
        efforts = [(e, EFFORT_LABELS[e]) for e in EFFORTS]
        rows = [
            ("reasoning.hint.effort", "Hint"),
            ("reasoning.concise.effort", "Concise explanation"),
            ("reasoning.followup.effort", "Follow-ups and chips"),
            ("reasoning.deep.effort", "Go deeper / Check this card / Quiz grading"),
        ]
        combos = []
        for path, label in rows:
            box = page.combo(path, efforts)
            box.setEnabled(supported)
            if not supported:
                box.setToolTip("The selected model doesn't support reasoning.")
            combos.append(box)
            form.addRow(label, box)
        page.layout_.addWidget(_hint(
            "Reasoning improves hard explanations but adds latency. Hints default to Off so they stay "
            "instant; concise explanations use Minimal for a fast first word."
        ))

        group = QGroupBox("Token budget (advanced)")
        g = QFormLayout(group)
        use_budget = QCheckBox("Use a reasoning token budget instead of effort levels")
        budget = QSpinBox()
        budget.setRange(256, 64000)
        budget.setSingleStep(256)
        budget.setSuffix(" tokens")
        g.addRow(use_budget)
        g.addRow("Budget", budget)
        g.addRow(_hint("OpenRouter rejects effort and budget together, so only one is sent. Modes set to Off still skip reasoning."))
        group.setEnabled(supported)
        page.layout_.addWidget(group)

        def get_budget() -> Any:
            return int(budget.value()) if use_budget.isChecked() else None

        def set_budget(v: Any) -> None:
            use_budget.setChecked(v is not None)
            budget.setValue(int(v) if v else 2048)
            budget.setEnabled(v is not None)

        use_budget.toggled.connect(budget.setEnabled)
        page.bind("reasoning.budget_tokens", get_budget, set_budget)

        disp = QGroupBox("Show reasoning")
        d = QVBoxLayout(disp)
        radios = {
            "hidden": QRadioButton("Hidden: show only a “Thinking… Ns” indicator (default)"),
            "collapsible": QRadioButton("Collapsible: a collapsed “Thinking” section above the answer"),
            "off": QRadioButton("Off: ask the provider to omit reasoning (it is still billed)"),
        }
        for r in radios.values():
            d.addWidget(r)
        d.addWidget(_hint("With Off, reasoning tokens are still generated and billed by the provider; they're just not returned."))
        disp.setEnabled(supported)
        page.layout_.addWidget(disp)
        page.bind(
            "reasoning.display",
            lambda: next(k for k, r in radios.items() if r.isChecked()),
            lambda v: radios.get(v, radios["hidden"]).setChecked(True),
        )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def _output(self, page: Page) -> None:
        form = page.form()
        form.addRow("Concise length", page.combo("output.concise_length", [("short", "Short (≤60 words)"), ("standard", "Standard (≤120 words)")]))
        form.addRow("Student level", page.combo("output.level", [("intro", "Intro"), ("undergraduate", "Undergraduate"), ("graduate", "Graduate / Professional")]))
        studying = QLineEdit()
        studying.setPlaceholderText("e.g. USMLE Step 1, JLPT N3 Japanese, AWS SA Associate")
        page.bind("output.studying", studying.text, lambda v: studying.setText(v or ""))
        form.addRow("What I'm studying", studying)
        lang = QComboBox()
        lang.setEditable(True)
        for value, label in LANGUAGES:
            lang.addItem(label, value)

        def get_lang() -> str:
            text = lang.currentText().strip()
            for value, label in LANGUAGES:
                if text == label:
                    return value
            return text or "same_as_card"

        def set_lang(v: Any) -> None:
            idx = lang.findData(v)
            if idx >= 0:
                lang.setCurrentIndex(idx)
            else:
                lang.setEditText(str(v))

        page.bind("output.language", get_lang, set_lang)
        form.addRow("Explanation language", lang)
        form.addRow("", page.check("output.memory_hook", "Include a memory hook when useful"))
        temp = QDoubleSpinBox()
        temp.setRange(0.0, 1.0)
        temp.setSingleStep(0.05)
        temp.setDecimals(2)
        page.bind("output.temperature", temp.value, lambda v: temp.setValue(float(v)))
        form.addRow("Temperature", temp)
        form.addRow("", page.check("output.stream", "Stream responses"))
        page.layout_.addWidget(_hint("Lower temperature keeps explanations stable and accurate. “What I'm studying” is added to the system prompt and greatly improves relevance."))
        adv = QGroupBox("Max output tokens per mode (advanced)")
        a = QFormLayout(adv)
        for tier, label in (("hint", "Hint"), ("concise", "Concise"), ("followup", "Follow-up"), ("deep", "Deep")):
            a.addRow(label, page.spin(f"output.max_tokens.{tier}", 50, 64000))
        a.addRow(_hint("These include reasoning tokens on models that count them together."))
        page.layout_.addWidget(adv)

    # ------------------------------------------------------------------
    # Behavior
    # ------------------------------------------------------------------
    def _behavior(self, page: Page) -> None:
        form = page.form()
        form.addRow("Question side", page.combo("behavior.question_side", [
            ("hint", "Hint (recommended)"),
            ("explain_no_reveal", "Explain without revealing the answer"),
            ("disabled", "Disabled"),
        ]))
        form.addRow("", page.check("behavior.auto_open_on_answer", "Open the panel when the answer is revealed (it never requests anything until you press Explain)"))
        nudge = page.check("behavior.struggle_nudge", "Offer an explanation after repeated lapses")
        lapses = page.spin("behavior.struggle_nudge_lapses", 1, 100, " lapses")
        nudge.toggled.connect(lapses.setEnabled)
        row = QHBoxLayout()
        row.addWidget(nudge)
        row.addWidget(lapses)
        row.addStretch(1)
        form.addRow("Struggle nudge", row)
        form.addRow("Panel position", page.combo("behavior.panel_position", [("right", "Right"), ("left", "Left"), ("floating", "Floating")]))
        form.addRow("Panel font size", page.spin("behavior.font_size", 10, 28, " px"))
        form.addRow("", page.check("behavior.reduce_motion", "Reduce motion"))
        form.addRow("", page.check("behavior.show_cost", "Show cost in the footer"))
        color_btn = QPushButton()
        reset_color = QPushButton("Use Anki blue")
        state = {"value": ""}

        def paint() -> None:
            value = state["value"]
            color_btn.setText(value or "Anki blue (default)")
            color_btn.setStyleSheet(f"background:{value}; color: white;" if value else "")

        def pick() -> None:
            c = QColorDialog.getColor(QColor(state["value"] or "#3584e4"), self, "Accent color")
            if c.isValid():
                state["value"] = c.name()
                paint()

        color_btn.clicked.connect(pick)
        reset_color.clicked.connect(lambda: (state.update(value=""), paint()))
        row2 = QHBoxLayout()
        row2.addWidget(color_btn)
        row2.addWidget(reset_color)
        row2.addStretch(1)
        form.addRow("Accent color", row2)
        page.bind("behavior.accent_color", lambda: state["value"], lambda v: (state.update(value=v or ""), paint()))
        page.after_load = lambda: lapses.setEnabled(nudge.isChecked())  # type: ignore[method-assign]
        page.layout_.addWidget(_hint("The struggle nudge is a visual cue only: a dot on the Explain button. Nothing is requested until you press it."))

    # ------------------------------------------------------------------
    # Learning moves (chips)
    # ------------------------------------------------------------------
    def _chips(self, page: Page) -> None:
        page.layout_.addWidget(_hint(
            "Choose which learning moves appear and in what order. The first six are always visible "
            "(Alt+1…Alt+6); the rest sit under More. Step by step and Mnemonic are promoted automatically "
            "on math/code cards and list-like answers."
        ))
        lst = QListWidget()
        lst.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        lst.setMinimumHeight(300)
        row = QHBoxLayout()
        up = QPushButton("Move up")
        down = QPushButton("Move down")
        row.addWidget(lst, 1)
        side = QVBoxLayout()
        side.addWidget(up)
        side.addWidget(down)
        side.addStretch(1)
        row.addLayout(side)
        page.layout_.addLayout(row)

        def move(delta: int) -> None:
            r = lst.currentRow()
            if r < 0 or not 0 <= r + delta < lst.count():
                return
            item = lst.takeItem(r)
            lst.insertItem(r + delta, item)
            lst.setCurrentRow(r + delta)

        up.clicked.connect(lambda: move(-1))
        down.clicked.connect(lambda: move(1))

        def setter(order: Any) -> None:
            lst.clear()
            enabled = [c for c in order if c in prompts.CHIPS]
            rest = [c for c in prompts.CHIPS if c not in enabled]
            for cid in enabled + rest:
                chip = prompts.CHIPS[cid]
                item = QListWidgetItem(f"{chip.label} — {chip.description}")
                item.setData(Qt.ItemDataRole.UserRole, cid)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if cid in enabled else Qt.CheckState.Unchecked)
                lst.addItem(item)

        def getter() -> list[str]:
            out = []
            for i in range(lst.count()):
                item = lst.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    out.append(item.data(Qt.ItemDataRole.UserRole))
            return out

        page.bind("chips", getter, setter)

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------
    def _shortcuts(self, page: Page) -> None:
        form = page.form()
        self._sc_edits: dict[str, QKeySequenceEdit] = {}
        rows = [
            ("shortcuts.explain", "Explain / hint / toggle panel"),
            ("shortcuts.focus_input", "Open panel and focus the follow-up box"),
            ("shortcuts.stop", "Stop generation"),
            ("shortcuts.focus_reviewer", "Return focus to the reviewer (twice closes the panel)"),
        ]
        for path, label in rows:
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1) if hasattr(edit, "setMaximumSequenceLength") else None
            self._sc_edits[path] = edit
            page.bind(path, lambda e=edit: e.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
                      lambda v, e=edit: e.setKeySequence(QKeySequence(v or "")))
            edit.keySequenceChanged.connect(lambda _s: self._update_conflicts())
            form.addRow(label, edit)
        chip_edits: list[QKeySequenceEdit] = []
        for i in range(6):
            edit = QKeySequenceEdit()
            chip_edits.append(edit)
            self._sc_edits[f"chip{i}"] = edit
            edit.keySequenceChanged.connect(lambda _s: self._update_conflicts())
            form.addRow(f"Chip {i + 1}", edit)
        page.bind(
            "shortcuts.chips",
            lambda: [e.keySequence().toString(QKeySequence.SequenceFormat.PortableText) for e in chip_edits],
            lambda v: [e.setKeySequence(QKeySequence(k or "")) for e, k in zip(chip_edits, list(v) + [""] * 6)],
        )
        self.conflict_label = QLabel()
        self.conflict_label.setWordWrap(True)
        page.layout_.addWidget(self.conflict_label)
        page.layout_.addWidget(_hint("1–4 and Space are Anki's grading and flipping keys. They're never intercepted and can't be rebound here. Enter sends a follow-up; Shift+Enter adds a new line."))
        page.after_load = self._update_conflicts  # type: ignore[method-assign]

    def _shortcut_errors(self) -> list[str]:
        if not hasattr(self, "_sc_edits"):
            return []
        anki_keys = set(reviewer_shortcut_keys())
        seen: dict[str, str] = {}
        errors = []
        names = {
            "shortcuts.explain": "Explain", "shortcuts.focus_input": "Focus follow-up",
            "shortcuts.stop": "Stop", "shortcuts.focus_reviewer": "Return focus",
        }
        for key, edit in self._sc_edits.items():
            seq = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            if not seq:
                continue
            n = norm(seq)
            label = names.get(key, f"Chip {int(key[4:]) + 1}" if key.startswith("chip") else key)
            if n in anki_keys and key in ("shortcuts.explain", "shortcuts.focus_input"):
                errors.append(f"{label}: {seq} is already one of Anki's reviewer keys.")
            if n in seen:
                errors.append(f"{label}: {seq} is also used for {seen[n]}.")
            seen[n] = label
        return errors

    def _update_conflicts(self) -> None:
        errors = self._shortcut_errors()
        if errors:
            self.conflict_label.setText("⚠ " + "<br>⚠ ".join(errors))
            self.conflict_label.setStyleSheet("color: #c62828;")
        else:
            self.conflict_label.setText("✓ No conflicts.")
            self.conflict_label.setStyleSheet("color: palette(placeholder-text);")

    # ------------------------------------------------------------------
    # Card context & cache
    # ------------------------------------------------------------------
    def _context(self, page: Page) -> None:
        group = QGroupBox("What is sent with each request")
        g = QFormLayout(group)
        send_images = page.check("context.send_images", "Send images")
        max_images = page.spin("context.max_images", 0, 8, " images per request")
        send_images.toggled.connect(max_images.setEnabled)
        row = QHBoxLayout()
        row.addWidget(send_images)
        row.addWidget(max_images)
        row.addStretch(1)
        g.addRow(row)
        g.addRow(page.check("context.allow_remote_images", "Allow remote (web) images"))
        g.addRow(page.check("context.include_deck", "Include deck name"))
        g.addRow(page.check("context.include_tags", "Include tags"))
        g.addRow(page.check("context.include_hidden_fields", "Include fields not shown on the card"))
        g.addRow(page.check("context.include_typed_answer", "Include typed answers"))
        g.addRow(page.check("context.drop_old_images", "Drop images from long conversations (after 6 turns) to save tokens"))
        page.layout_.addWidget(group)
        page.after_load = lambda: max_images.setEnabled(send_images.isChecked())  # type: ignore[method-assign]
        page.layout_.addWidget(_hint(
            "These settings control what goes into the prompt. They are not data-retention controls."
        ))

        cache = QGroupBox("Cache and history")
        c = QFormLayout(cache)
        c.addRow(page.check("context.cache_enabled", "Cache explanations locally (re-opening an explained card is instant and free)"))
        c.addRow("Keep for", page.spin("context.cache_retention_days", 1, 3650, " days"))
        size = QLabel()
        clear = QPushButton("Clear cache")

        def refresh() -> None:
            n = self.services.cache.size_bytes()
            size.setText(f"{n / 1024 / 1024:.1f} MB on disk" if n > 1024 * 1024 else f"{n / 1024:.0f} KB on disk")

        def do_clear() -> None:
            if askUser("Delete all cached explanations and history?", parent=self):
                self.services.cache.clear()
                refresh()
                tooltip("Cache cleared.", parent=self)

        clear.clicked.connect(do_clear)
        refresh()
        row2 = QHBoxLayout()
        row2.addWidget(clear)
        row2.addWidget(size)
        row2.addStretch(1)
        c.addRow(row2)
        page.layout_.addWidget(cache)

        privacy = QGroupBox("Privacy")
        p = QVBoxLayout(privacy)
        p.addWidget(_hint(
            "When you press Explain, this card's content is sent to OpenRouter using your account's "
            "settings. Nothing is sent before you press Explain, and nothing is sent for other cards."
        ))
        p.addWidget(_hint(
            "Provider routing and data-retention preferences are not configured in the add-on; they come "
            f"from your OpenRouter account: <a href='{PRIVACY_SETTINGS_URL}'>privacy settings</a> · "
            f"<a href='{PROVIDER_SETTINGS_URL}'>provider preferences</a>."
        ))
        page.layout_.addWidget(privacy)

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------
    def _prompts(self, page: Page) -> None:
        page.layout_.addWidget(_hint(
            "Edit the system prompt and per-mode prompts. Your edits survive add-on updates. Changing a "
            "prompt invalidates cached explanations made with the old one."
        ))
        picker = QComboBox()
        for key, (label, _default, _vars) in prompts.EDITABLE.items():
            picker.addItem(label, key)
        editor = QPlainTextEdit()
        editor.setMinimumHeight(260)
        variables = QLabel()
        variables.setWordWrap(True)
        warning = QLabel()
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b26a00;")
        reset = QPushButton("Reset to default")
        page.layout_.addWidget(picker)
        page.layout_.addWidget(editor, 1)
        page.layout_.addWidget(variables)
        page.layout_.addWidget(warning)
        row = QHBoxLayout()
        row.addWidget(reset)
        row.addStretch(1)
        page.layout_.addLayout(row)
        overrides: dict[str, Any] = {}
        state = {"key": None}

        def commit() -> None:
            key = state["key"]
            if key is None:
                return
            text = editor.toPlainText()
            default = prompts.EDITABLE[key][1]
            if text.strip() == default.strip() or not text.strip():
                overrides.pop(key, None)
            else:
                prev = overrides.get(key, {})
                overrides[key] = {"text": text, "default_hash": prev.get("default_hash") or prompts.default_hash(key)}

        def show(_i: int = 0) -> None:
            commit()
            key = picker.currentData()
            state["key"] = key
            o = overrides.get(key)
            editor.setPlainText(o["text"] if o else prompts.EDITABLE[key][1])
            docs = prompts.EDITABLE[key][2]
            variables.setText(
                "Variables: " + ", ".join(f"<code>{{{{{v}}}}}</code> — {prompts.VARIABLE_DOCS[v]}" for v in docs)
                if docs else "No variables in this prompt."
            )
            if o and o.get("default_hash") and o["default_hash"] != prompts.default_hash(key):
                warning.setText("⚠ The default for this prompt changed in an add-on update since you edited it (prompt version "
                                f"{prompts.PROMPT_VERSION}). Review your edit or reset it.")
            else:
                warning.setText("")

        def reset_one() -> None:
            key = state["key"]
            overrides.pop(key, None)
            state["key"] = None
            show()

        def setter(v: Any) -> None:
            overrides.clear()
            overrides.update(copy.deepcopy(v or {}))
            state["key"] = None
            show()

        def getter() -> dict[str, Any]:
            commit()
            return copy.deepcopy(overrides)

        picker.currentIndexChanged.connect(show)
        reset.clicked.connect(reset_one)
        page.bind("prompts.overrides", getter, setter)

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------
    def _usage(self, page: Page) -> None:
        services = self.services
        session = services.controller.session if services.controller else {}
        month = services.cache.month_usage()
        metrics = services.cache.metrics()

        def fmt_cost(v: Any) -> str:
            return f"${float(v or 0):.4f}"

        form = page.form()
        form.addRow("<b>This session</b>", QLabel(""))
        form.addRow("Requests", QLabel(str(session.get("requests", 0))))
        form.addRow("Tokens", QLabel(f"{session.get('prompt_tokens', 0):,} in · {session.get('completion_tokens', 0):,} out"))
        form.addRow("Spend", QLabel(fmt_cost(session.get("cost"))))
        form.addRow("<b>This month</b>", QLabel(month.get("month", "")))
        form.addRow("Requests", QLabel(str(month.get("requests", 0))))
        form.addRow("Tokens", QLabel(f"{month.get('prompt_tokens', 0):,} in · {month.get('completion_tokens', 0):,} out"))
        form.addRow("Spend", QLabel(fmt_cost(month.get("cost_usd"))))

        cap_box = QCheckBox("Ask before each request once this month's spend reaches")
        cap = QDoubleSpinBox()
        cap.setRange(0.01, 10000)
        cap.setDecimals(2)
        cap.setPrefix("$")
        cap_box.toggled.connect(cap.setEnabled)
        row = QHBoxLayout()
        row.addWidget(cap_box)
        row.addWidget(cap)
        row.addStretch(1)
        page.layout_.addLayout(row)

        def set_cap(v: Any) -> None:
            cap_box.setChecked(v is not None)
            cap.setValue(float(v) if v is not None else 2.0)
            cap.setEnabled(v is not None)

        page.bind("usage.monthly_soft_cap_usd", lambda: float(cap.value()) if cap_box.isChecked() else None, set_cap)

        group = QGroupBox("How it's helping (this month, private)")
        g = QFormLayout(group)

        def pct(v: Any) -> str:
            return "—" if v is None else f"{v * 100:.0f}%"

        def secs(v: Any) -> str:
            return "—" if v is None else f"{v:.0f} s"

        g.addRow("Explanations", QLabel(str(metrics["explanations"])))
        g.addRow("Median time from Explain to grade", QLabel(secs(metrics["median_seconds_to_grade"])))
        g.addRow("Depth rate (followed by a chip or question)", QLabel(pct(metrics["depth_rate"])))
        g.addRow("Hint resolution (Good/Easy after a hint)", QLabel(pct(metrics["hint_resolution"])))
        g.addRow("Median cost per explanation", QLabel("—" if metrics["median_cost"] is None else f"${metrics['median_cost']:.5f}"))
        page.layout_.addWidget(group)
        page.layout_.addWidget(_hint("Costs come from OpenRouter's usage report on each response. None of these numbers leave your computer."))


def open_dialog(services: Any, page: str | None = None) -> None:
    dialog = SettingsDialog(services, page)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.show()
