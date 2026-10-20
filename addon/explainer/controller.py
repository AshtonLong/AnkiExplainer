"""Orchestration: card → context → request → panel (spec §8.3).

Every network request starts here and only from an explicit user action
(Explain/Hint, a chip, a follow-up, Regenerate, Retry, Connect). Nothing runs
on card show or answer reveal. At most one request is active; starting
another cancels it. Moving to another card cancels the in-flight request and
discards its partial text, so the cache only ever holds complete explanations.

The controller talks to Anki only through a ``Host`` and to the UI only through
a ``PanelView``, which keeps it testable outside Anki (see
``tests/test_controller.py``).
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .context.extractor import CardContext, RawCard, build_context, load_images
from .llm import leak_guard, prompts
from .llm.models import get_model
from .llm.openrouter import (
    CREDITS_URL,
    PROVIDER_SETTINGS_URL,
    ApiError,
    Cancelled,
    CancelToken,
    OpenRouterClient,
    build_body,
    make_error,
)
from .llm.threads import Thread, Turn
from .store.cache import Cache, CacheKey, selection_hash
from .store.secrets import SecretStore
from .store.settings import Settings
from .util.log import get_logger

log = get_logger("controller")

FLUSH_MS = 33
SESSION_THREADS = 60

# mode → (prompt key, reasoning tier, max-token tier)
MODES: dict[str, tuple[str, str, str]] = {
    "concise": ("concise", "concise", "concise"),
    "selection": ("selection", "concise", "concise"),
    "hint": ("hint", "hint", "hint"),
    "stronger_hint": ("stronger_hint", "hint", "hint"),
    "explain_no_reveal": ("explain_no_reveal", "concise", "concise"),
    "question": ("", "followup", "followup"),
    "ask": ("", "followup", "followup"),
    "quiz_grade": ("quiz_grade", "deep", "followup"),
}
CACHEABLE = {"concise", "selection", "hint", "explain_no_reveal"}
HINT_MODES = {"hint", "stronger_hint"}

TIPS = [
    "Select a word on the card, right-click, and choose “Explain selection”.",
    "Press {focus} to ask your own follow-up question.",
    "After an explanation, try the Example or Analogy chips.",
    "Alt+1…Alt+6 trigger the first six chips without leaving the keyboard.",
    "Explaining a card never changes its scheduling. Grade as usual.",
]


class Host:
    """Anki-facing operations. ``ui/anki_host.py`` implements this."""

    def current_card(self) -> Any: ...
    def reviewer_side(self) -> str | None: ...
    def card_summary(self, card: Any) -> dict[str, Any]: ...
    def gather_raw(self, card: Any, side: str) -> RawCard: ...
    def gather_raw_by_id(self, card_id: int, side: str) -> RawCard | None: ...
    def run_in_background(self, fn: Callable[[], None]) -> None: ...
    def start_timer(self, ms: int, cb: Callable[[], None]) -> Any: ...
    def flip_card(self) -> bool: ...
    def focus_reviewer(self) -> None: ...
    def open_settings(self, page: str | None = None) -> None: ...
    def open_link(self, url: str) -> None: ...
    def copy_text(self, text: str) -> None: ...
    def edit_card(self, card_id: int) -> None: ...
    def open_browser(self, card_id: int) -> None: ...
    def profile_name(self) -> str: ...
    def tooltip(self, msg: str) -> None: ...
    def show_model_menu(self) -> None: ...
    def update_bar(self, state: dict[str, Any]) -> None: ...
    def shortcut_label(self, name: str) -> str: ...


class PanelView:
    """Panel operations (``ui/panel.py``)."""

    def is_visible(self) -> bool: ...
    def show(self) -> None: ...
    def hide(self) -> None: ...
    def set_state(self, state: dict[str, Any]) -> None: ...
    def set_phase(self, thread_id: str, phase: str, info: dict[str, Any]) -> None: ...
    def append_delta(self, thread_id: str, text: str, kind: str) -> None: ...
    def finish(self, thread_id: str, meta: dict[str, Any]) -> None: ...
    def error(self, thread_id: str, err: dict[str, Any]) -> None: ...
    def focus_input(self) -> None: ...
    def has_focus(self) -> bool: ...


@dataclass
class Request:
    id: int
    thread: Thread
    turn: Turn
    mode: str
    tier: str
    token_tier: str
    ctx: CardContext | None
    needs_images: bool
    instruction: str | None  # for first turns: the mode instruction
    cache_key: CacheKey | None = None
    hint_attempt: int = 0
    token: CancelToken = field(default_factory=CancelToken)
    events: collections.deque = field(default_factory=collections.deque)
    started: float = field(default_factory=time.monotonic)
    first_token_at: float | None = None
    thinking: bool = False
    finished: bool = False
    discard: bool = False
    first_content_applied: bool = False


class Controller:
    def __init__(
        self,
        host: Host,
        settings: Settings,
        secrets: SecretStore,
        cache: Cache,
        client_factory: Callable[[str], OpenRouterClient] | None = None,
    ) -> None:
        self.host = host
        self.settings = settings
        self.secrets = secrets
        self.cache = cache
        self.client_factory = client_factory or (lambda key: OpenRouterClient(key))
        self.panel: PanelView | None = None
        self._req_seq = 0
        self.active: Request | None = None
        self._timer: Any = None
        self.current: tuple[int, str] | None = None  # (card_id, side)
        self.view: str = "idle"
        self.thread: Thread | None = None  # thread shown in the panel
        self.threads: collections.OrderedDict[tuple[int, str, str], Thread] = collections.OrderedDict()
        self.contexts: dict[str, CardContext] = {}
        self.history_items: list[dict[str, Any]] = []
        self.history_thread: Thread | None = None
        self.pending_after_flip: int | None = None
        self.pending_action: Callable[[], None] | None = None
        self.confirm_message: str | None = None
        self.onboarding: dict[str, Any] = {"busy": False, "error": None}
        self.session = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        self.metric_event: tuple[int, int, float] | None = None  # (event id, card id, t0)
        self.tip_index = 0
        self._evicted = False
        self.input_typing = False
        self.network_requests = 0  # for the on-demand guarantee tests

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------
    def attach_panel(self, panel: PanelView) -> None:
        self.panel = panel

    @property
    def model(self) -> Any:
        return get_model(self.settings.model)

    def _maybe_evict(self) -> None:
        if self._evicted:
            return
        self._evicted = True
        try:
            self.cache.evict(int(self.settings.ctx("cache_retention_days")))
        except Exception:
            log.exception("cache eviction failed")

    # ------------------------------------------------------------------
    # reviewer events (never trigger requests)
    # ------------------------------------------------------------------
    def on_card_shown(self, card: Any, side: str) -> None:
        self.current = (card.id, side)
        self._cancel_active(discard=True)
        self.thread = None
        self.history_thread = None
        if self.view in ("thread", "confirm", "history_thread"):
            self.view = "idle"
        self.confirm_message = None
        self.pending_action = None
        self.tip_index = (self.tip_index + 1) % len(TIPS)
        self._update_bar(card)
        if self.pending_after_flip == card.id and side == "answer":
            self.pending_after_flip = None
            self.explain_pressed(source="flip")
            return
        self.pending_after_flip = None
        if self.panel is None:
            return
        if self.panel.is_visible():
            self._render()
        elif side == "answer" and self.settings.behavior("auto_open_on_answer"):
            self.view = "idle" if self.view != "onboarding" else self.view
            self.panel.show()
            self._render()

    def on_reviewer_end(self) -> None:
        self._cancel_active(discard=True)
        self.current = None
        self.thread = None
        self.pending_after_flip = None
        if self.view not in ("history", "history_thread", "onboarding"):
            self.view = "idle"
        self._render()

    def on_card_answered(self, card_id: int, ease: int) -> None:
        if self.metric_event and self.metric_event[1] == card_id:
            event_id, _, t0 = self.metric_event
            try:
                self.cache.record_grade(event_id, int(ease), time.monotonic() - t0)
            except Exception:
                log.exception("metric record failed")
        self.metric_event = None

    def _update_bar(self, card: Any = None) -> None:
        side = self.current[1] if self.current else None
        if card is None:
            card = self.host.current_card()
        if card is None or side is None:
            return
        qs = self.settings.question_side
        if side == "question":
            visible = qs != "disabled"
            label = "Hint" if qs == "hint" else "Explain"
        else:
            visible, label = True, "Explain"
        nudge = bool(
            self.settings.behavior("struggle_nudge")
            and getattr(card, "lapses", 0) >= int(self.settings.behavior("struggle_nudge_lapses"))
        )
        busy = bool(self.active and not self.active.finished and self.active.thread.card_id == card.id)
        self.host.update_bar(
            {
                "visible": visible,
                "label": label,
                "busy": busy,
                "nudge": nudge,
                "shortcut": self.host.shortcut_label("explain"),
            }
        )

    # ------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------
    def explain_pressed(self, source: str = "key", selection: str | None = None) -> None:
        """X / bottom-bar button / context menu."""
        card = self.host.current_card()
        side = self.host.reviewer_side()
        if card is None or side is None or self.panel is None:
            return
        self.current = (card.id, side)
        if selection is None and source == "key":
            sel = self.host_selected_text()
            selection = sel or None
        sel_hash = selection_hash(selection)
        shown = self._shown_thread()
        same_thread = (
            shown is not None
            and shown.card_id == card.id
            and shown.side == side
            and selection_hash(shown.selection) == sel_hash
            and self.view == "thread"
        )
        busy = bool(self.active and not self.active.finished)
        if same_thread and busy and source != "flip":
            # Already explaining exactly this: just make sure it's visible.
            self.panel.show()
            return
        if source in ("key", "button") and self.panel.is_visible() and same_thread:
            self.panel.hide()
            self.host.focus_reviewer()
            return
        self.panel.show()
        if not self.secrets.has_key():
            self.pending_action = lambda: self.explain_pressed(source="pending", selection=selection)
            self.view = "onboarding"
            self._render()
            return
        if side == "question":
            question_side = self.settings.question_side
            if question_side == "disabled":
                self.view = "idle"
                self._render(notice="Question-side help is turned off in settings.")
                return
            if selection and self._selection_is_answer(card, selection):
                # Explaining the answer itself would spoil recall: hint rules apply.
                selection = None
                mode = "hint"
            elif selection:
                mode = "selection"
            else:
                mode = "hint" if question_side == "hint" else "explain_no_reveal"
        else:
            mode = "selection" if selection else "concise"
        self._start_first(card, side, mode, selection)

    def host_selected_text(self) -> str:
        fn = getattr(self.host, "selected_text", None)
        try:
            return (fn() or "").strip() if fn else ""
        except Exception:
            return ""

    def explain_selection(self, selection: str) -> None:
        self.explain_pressed(source="menu", selection=selection)

    def focus_input_pressed(self) -> None:
        if self.panel is None or self.host.current_card() is None:
            return
        self.panel.show()
        if not self.secrets.has_key():
            self.view = "onboarding"
            self._render()
            return
        if self.view not in ("thread", "idle"):
            self.view = "thread" if self._shown_thread() else "idle"
        self._render()
        self.panel.focus_input()

    def stop_pressed(self) -> None:
        if self.active and not self.active.finished:
            self.active.token.cancel("stop")

    def chip_shortcut(self, index: int) -> None:
        if self.panel is None or not self.panel.is_visible():
            return
        thread = self._shown_thread()
        if thread is None or self.view != "thread":
            return
        if thread.side == "question":
            if index == 0:
                self._stronger_hint()
            elif index == 1:
                self._show_answer_and_explain()
            return
        visible = self._chips_for(thread)[:6]
        if 0 <= index < len(visible):
            self.run_chip(visible[index])

    def escape_pressed(self) -> bool:
        """Esc in review: first press returns focus, a quick second press closes."""
        if self.panel is None or not self.panel.is_visible():
            return False
        now = time.monotonic()
        if self.panel.has_focus():
            self._last_esc = now
            self.host.focus_reviewer()
            return True
        if now - getattr(self, "_last_esc", 0.0) < 1.5:
            self._last_esc = 0.0
            self.panel.hide()
            return True
        self._last_esc = now
        return False  # let Anki's usual Esc behaviour run (e.g. leave the type-in box)

    # ------------------------------------------------------------------
    # first turns: cache, threads, request
    # ------------------------------------------------------------------
    def _selection_is_answer(self, card: Any, selection: str) -> bool:
        try:
            raw = self.host.gather_raw(card, "question")
            ctx = build_context(raw, self.settings)
        except Exception:
            return False
        answers = ctx.answer_hint_strings()
        norm = leak_guard.normalize(selection)
        return bool(norm) and any(
            norm == leak_guard.normalize(a) or leak_guard.leaks(selection, [a]) for a in answers
        )

    def _build_ctx(self, card: Any, side: str, selection: str | None) -> CardContext:
        raw = self.host.gather_raw(card, side)
        return build_context(raw, self.settings, selection)

    def _start_first(self, card: Any, side: str, mode: str, selection: str | None, force: bool = False) -> None:
        self._maybe_evict()
        if not force:
            self._undo = None
        try:
            ctx = self._build_ctx(card, side, selection)
        except Exception:
            log.exception("context extraction failed")
            self.view = "idle"
            self._render(notice="Couldn't read this card. See the add-on log for details.")
            return
        key = (card.id, side, selection_hash(selection))
        existing = self.threads.get(key)
        if (
            not force
            and existing is not None
            and existing.content_hash == ctx.content_hash
            and existing.has_first_answer
        ):
            self.threads.move_to_end(key)
            self.thread = existing
            self.contexts.setdefault(existing.id, ctx)
            self.view = "thread"
            self._render()
            self._record_metric(existing, mode, None)
            return

        prompt_set = prompts.PromptSet(self.settings)
        prompt_key = MODES[mode][0]
        instruction = prompt_set.mode(prompt_key, selection)
        cache_key = None
        if mode in CACHEABLE:
            cache_key = CacheKey(
                content_hash=ctx.content_hash,
                side=side,
                mode=mode,
                selection_hash=selection_hash(selection),
                model_requested=self.settings.model,
                prompt_version=prompt_set.fingerprint(prompt_key, selection),
            )
        thread = self._new_thread(ctx, mode, prompt_set)
        thread.add_user(self._first_content(ctx, instruction, with_images=False), display=self._first_display(mode, selection), kind="initial")

        if not force and cache_key is not None and self.settings.ctx("cache_enabled"):
            hit = None
            try:
                hit = self.cache.lookup(cache_key)
            except Exception:
                log.exception("cache lookup failed")
            if hit is not None:
                self._restore_from_cache(thread, ctx, hit)
                self._remember(key, thread, ctx)
                self.thread = thread
                self.view = "thread"
                self._render()
                self._record_metric(thread, mode, None)
                return

        self._remember(key, thread, ctx)
        self.thread = thread
        self._request(
            thread,
            mode=mode,
            ctx=ctx,
            needs_images=True,
            instruction=instruction,
            cache_key=cache_key,
        )

    def _new_thread(self, ctx: CardContext, mode: str, prompt_set: prompts.PromptSet) -> Thread:
        return Thread(
            card_id=ctx.card_id,
            note_id=ctx.note_id,
            side=ctx.side,
            mode=mode,
            content_hash=ctx.content_hash,
            selection=ctx.selection,
            title=ctx.title,
            deck=ctx.deck_name or "",
            model=self.settings.model,
            profile=self.host.profile_name(),
            system=prompt_set.system(),
            card_kind=ctx.card_kind,
            kind_label=ctx.kind_label,
            target=ctx.target,
            sent_summary=ctx.sent_summary(),
        )

    def _first_display(self, mode: str, selection: str | None) -> str:
        if mode == "selection" and selection:
            short = selection if len(selection) <= 60 else selection[:57] + "…"
            return f"Explain “{short}”"
        return {"hint": "Hint", "explain_no_reveal": "Explain the question"}.get(mode, "Explain")

    def _first_content(self, ctx: CardContext, instruction: str, with_images: bool) -> list[dict[str, Any]]:
        vision = self.model.vision
        if not with_images:
            saved = ctx.images
            ctx.images = []
            try:
                content = prompts.first_user_content(ctx, instruction, vision=vision)
            finally:
                ctx.images = saved
            return content
        return prompts.first_user_content(ctx, instruction, vision=vision)

    def _restore_from_cache(self, thread: Thread, ctx: CardContext, hit: dict[str, Any]) -> None:
        # Prefer a stored conversation (with follow-ups) for the same content.
        stored = None
        try:
            stored = self.cache.find_thread(
                ctx.card_id, ctx.side, selection_hash(ctx.selection), ctx.content_hash,
                self.host.profile_name(), mode=thread.mode, model=self.settings.model,
            )
        except Exception:
            log.exception("thread lookup failed")
        if stored and len(stored.get("turns", [])) >= 2:
            restored = Thread.from_dict(stored)
            thread.id = restored.id
            thread.turns = restored.turns
            thread.created_at = restored.created_at
            thread.awaiting_quiz = restored.awaiting_quiz
        else:
            a = thread.add_assistant("initial")
            a.content = hit["text"]
            a.reasoning = "" if thread.mode in HINT_MODES else (hit.get("reasoning") or "")
            a.reasoning_details = hit.get("reasoning_details")
            a.status = "done"
            usage = hit.get("usage") or {}
            a.meta = {"model": hit.get("model_resolved"), "cost": usage.get("cost")}
        # Everything shown came from the local cache: no new cost was incurred.
        for t in thread.turns:
            if t.role == "assistant":
                t.meta = dict(t.meta or {})
                t.meta["cached"] = True
        thread.images_stripped = True  # images reload lazily before a follow-up

    def _remember(self, key: tuple[int, str, str], thread: Thread, ctx: CardContext) -> None:
        self.threads[key] = thread
        self.threads.move_to_end(key)
        self.contexts[thread.id] = ctx
        while len(self.threads) > SESSION_THREADS:
            _, old = self.threads.popitem(last=False)
            self.contexts.pop(old.id, None)

    # ------------------------------------------------------------------
    # follow-ups
    # ------------------------------------------------------------------
    def _shown_thread(self) -> Thread | None:
        return self.thread

    def _chips_for(self, thread: Thread) -> list[str]:
        ctx = self.contexts.get(thread.id)
        return prompts.chip_order(self.settings, ctx)

    def run_chip(self, chip_id: str) -> None:
        thread = self._shown_thread()
        chip = prompts.CHIPS.get(chip_id)
        if chip is None:
            return
        if thread is None or not thread.has_first_answer:
            self.host.tooltip("Explain the card first (press X).")
            return
        if not self._ready_for_request():
            return
        self._snapshot(thread)
        thread.awaiting_quiz = False
        text = prompts.PromptSet(self.settings).chip(chip_id)
        thread.add_user(text, display=chip.label, kind="chip", chip=chip_id)
        self._mark_followed_up(thread)
        self._request(thread, mode="chip", chip=chip)

    def ask(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        thread = self._shown_thread()
        if thread is None or self.view != "thread":
            card = self.host.current_card()
            side = self.host.reviewer_side()
            if card is None or side is None:
                return
            if not self.secrets.has_key():
                self.view = "onboarding"
                self._render()
                return
            self._start_question(card, side, text)
            return
        if not self._ready_for_request():
            return
        self._snapshot(thread)
        if thread.awaiting_quiz:
            grade = prompts.PromptSet(self.settings).mode("quiz_grade")
            thread.add_user(f"My answer: {text}\n\n{grade}", display=text, kind="quiz_answer")
            thread.awaiting_quiz = False
            self._mark_followed_up(thread)
            self._request(thread, mode="quiz_grade")
            return
        content = text
        if thread.side == "question":
            content = f"{text}\n\n(The student has not seen the answer yet. Do not reveal it.)"
        thread.add_user(content, display=text, kind="ask")
        self._mark_followed_up(thread)
        self._request(thread, mode="ask")

    def _start_question(self, card: Any, side: str, text: str) -> None:
        try:
            ctx = self._build_ctx(card, side, None)
        except Exception:
            log.exception("context extraction failed")
            return
        guard = " Do not reveal or hint at the answer; the student has not seen it yet." if side == "question" else ""
        instruction = f"The student asks: {text}\nAnswer briefly and specifically for this card.{guard}"
        prompt_set = prompts.PromptSet(self.settings)
        thread = self._new_thread(ctx, "question", prompt_set)
        thread.add_user(self._first_content(ctx, instruction, with_images=False), display=text, kind="initial")
        key = (card.id, side, "q:" + selection_hash(text))  # never shadows Explain's thread
        self._remember(key, thread, ctx)
        self.thread = thread
        self._request(thread, mode="question", ctx=ctx, needs_images=True, instruction=instruction)

    def _stronger_hint(self) -> None:
        thread = self._shown_thread()
        if thread is None or thread.side != "question" or not thread.has_first_answer:
            return
        if not self._ready_for_request():
            return
        self._snapshot(thread)
        text = prompts.PromptSet(self.settings).mode("stronger_hint")
        thread.add_user(text, display="Stronger hint", kind="stronger_hint")
        self._request(thread, mode="stronger_hint")

    def _show_answer_and_explain(self) -> None:
        card = self.host.current_card()
        if card is None:
            return
        self._cancel_active(discard=True)
        self.pending_after_flip = card.id
        if not self.host.flip_card():
            self.pending_after_flip = None
            self.host.tooltip("Couldn't flip the card. Press Space, then X.")

    def _snapshot(self, thread: Thread) -> None:
        """Remember a thread's state before a user action mutates it, so a
        cancelled soft-cap confirmation can put everything back."""
        key = next((k for k, t in self.threads.items() if t is thread), None)
        self._undo = (thread, list(thread.turns), thread.awaiting_quiz, key)

    def _cancel_confirm(self) -> None:
        self.pending_action = None
        self.confirm_message = None
        undo = getattr(self, "_undo", None)
        self._undo = None
        current = self.thread
        if current is not None and len(current.turns) <= 1 and (undo is None or undo[0] is not current):
            # A brand-new thread that never got its first answer.
            for k, t in list(self.threads.items()):
                if t is current:
                    del self.threads[k]
            self.contexts.pop(current.id, None)
            self.thread = None
        if undo is not None:
            thread, turns, quiz, key = undo
            thread.turns = turns
            thread.awaiting_quiz = quiz
            if key is not None:
                self.threads[key] = thread
            self.thread = thread
        self.view = "thread" if self.thread else "idle"
        self._render()

    def retry(self) -> None:
        thread = self._shown_thread()
        last = thread.turns[-1] if thread and thread.turns else None
        if last is None or last.role != "assistant" or last.status not in ("error", "incomplete", "stopped"):
            return  # Retry only applies to the latest answer when it failed
        self.regenerate()

    def regenerate(self) -> None:
        thread = self._shown_thread()
        if thread is None or len(thread.turns) < 2 or not self._ready_for_request():
            return
        if self.active and not self.active.finished:
            return
        last = thread.turns[-1]
        if last.role != "assistant":
            return
        self._snapshot(thread)
        if thread.awaiting_quiz and last.kind == "chip":
            thread.awaiting_quiz = False
        if len(thread.turns) == 2:
            # Regenerate the first answer. Cacheable modes on the card on screen
            # rebuild from fresh card content (bypassing the cache); anything else
            # re-sends the original first turn.
            card = self.host.current_card()
            on_screen = card is not None and card.id == thread.card_id and self.current and self.current[1] == thread.side
            if on_screen and thread.mode in CACHEABLE:
                self._start_first(card, thread.side, thread.mode, thread.selection, force=True)
                return
            thread.turns.pop()
            ctx = self.contexts.get(thread.id)
            self._request(thread, mode=thread.mode if thread.mode in MODES else "concise", ctx=ctx, needs_images=ctx is not None)
            return
        thread.turns.pop()
        user = thread.turns[-1]
        mode = {"chip": "chip", "quiz_answer": "quiz_grade", "stronger_hint": "stronger_hint"}.get(user.kind, "ask")
        chip = prompts.CHIPS.get(user.chip or "") if mode == "chip" else None
        self._request(thread, mode=mode, chip=chip)

    # ------------------------------------------------------------------
    # request lifecycle
    # ------------------------------------------------------------------
    def _ready_for_request(self) -> bool:
        if not self.secrets.has_key():
            self.view = "onboarding"
            self._render()
            return False
        return True

    def _request(
        self,
        thread: Thread,
        mode: str,
        ctx: CardContext | None = None,
        needs_images: bool = False,
        instruction: str | None = None,
        cache_key: CacheKey | None = None,
        chip: prompts.Chip | None = None,
        hint_attempt: int = 0,
        confirmed: bool = False,
    ) -> None:
        cap = self.settings.soft_cap
        if cap and not confirmed:
            try:
                spent = float(self.cache.month_usage()["cost_usd"])
            except Exception:
                spent = 0.0
            if spent >= cap:
                self.confirm_message = (
                    f"You've reached your monthly soft cap (${cap:.2f}; spent ${spent:.4f}). "
                    "Send this request anyway?"
                )
                self.pending_action = lambda: self._request(
                    thread, mode, ctx, needs_images, instruction, cache_key, chip, hint_attempt, confirmed=True
                )
                self.thread = thread
                self.view = "confirm"
                self._render()
                return
        self._undo = None
        self._cancel_active(discard=False)
        if mode == "chip" and chip is not None:
            tier, token_tier = chip.tier, chip.tokens
        else:
            _, tier, token_tier = MODES.get(mode, ("", "followup", "followup"))
        if ctx is None:
            ctx = self.contexts.get(thread.id)
        if thread.images_stripped and ctx is not None and ctx.image_sources:
            needs_images = True
        turn = thread.add_assistant(mode)
        self._req_seq += 1
        req = Request(
            id=self._req_seq,
            thread=thread,
            turn=turn,
            mode=mode,
            tier=tier,
            token_tier=token_tier,
            ctx=ctx,
            needs_images=needs_images and ctx is not None,
            instruction=instruction,
            cache_key=cache_key,
            hint_attempt=hint_attempt,
        )
        self.active = req
        self.thread = thread
        self.view = "thread"
        self.confirm_message = None
        self._render()
        self.panel and self.panel.set_phase(thread.id, "reading" if req.needs_images else "asking", {})
        self._update_bar()
        body_args = self._body_args(req)
        api_key = self.secrets.get() or ""
        client = self.client_factory(api_key)
        first_turn_index = 0
        upto = len(thread.turns) - 1  # exclude the new assistant turn
        snapshot_turns = list(thread.turns[:upto])
        allow_remote = bool(self.settings.ctx("allow_remote_images"))
        drop_old = bool(self.settings.ctx("drop_old_images"))
        self.network_requests += 1

        def work() -> None:
            emit = req.events.append
            try:
                if req.needs_images and req.ctx is not None:
                    failed = load_images(req.ctx, allow_remote)
                    if failed:
                        emit(("images_failed", len(failed)))
                    first = snapshot_turns[first_turn_index]
                    instr = req.instruction or _instruction_of(first.content)
                    content = prompts.first_user_content(req.ctx, instr, vision=self.model.vision)
                    emit(("first_content", content))
                    messages = _messages_with_first(thread, content, upto, drop_old)
                else:
                    messages = thread.api_messages(drop_old_images=drop_old, upto=upto)
                body = build_body(messages=messages, **body_args)
                emit(("phase", "asking"))
                if req.token.cancelled:
                    raise Cancelled()
                result = client.chat(body, lambda kind, value: emit((kind, value)), req.token)
                emit(("done", result))
            except Cancelled:
                emit(("cancelled", req.token.reason))
            except ApiError as err:
                emit(("error", err))
            except Exception as exc:  # never let a bug escape into Anki
                log.exception("request failed: %s", type(exc).__name__)
                emit(("error", make_error("unknown", detail=type(exc).__name__)))

        self.host.run_in_background(work)
        self._ensure_timer()

    def _body_args(self, req: Request) -> dict[str, Any]:
        s = self.settings
        model = self.model
        display = s.reasoning_display
        return {
            "model": s.model,
            "stream": s.stream,
            "temperature": s.temperature,
            "max_tokens": s.max_tokens(req.token_tier),
            "effort": s.effort(req.tier),
            "budget_tokens": s.budget_tokens,
            "exclude_reasoning": display == "off",
            "supports_reasoning": model.reasoning,
        }

    def _ensure_timer(self) -> None:
        if self._timer is None:
            self._timer = self.host.start_timer(FLUSH_MS, self._flush)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def _cancel_active(self, discard: bool) -> None:
        req = self.active
        if req is None or req.finished:
            return
        req.discard = discard
        req.finished = True
        req.token.cancel("card_changed" if discard else "superseded")
        thread = req.thread
        if discard:
            if req.turn in thread.turns:
                idx = thread.turns.index(req.turn)
                if idx <= 1:
                    # The first explanation never completed: forget the thread.
                    for k, t in list(self.threads.items()):
                        if t is thread:
                            del self.threads[k]
                    self.contexts.pop(thread.id, None)
                    if self.thread is thread:
                        self.thread = None
                else:
                    thread.turns.remove(req.turn)
                    if thread.turns and thread.turns[-1].role == "user":
                        thread.turns.pop()
        else:
            if req.mode in HINT_MODES:
                req.turn.content = ""  # never show an unchecked hint
                req.turn.reasoning = ""
            req.turn.status = "stopped" if req.turn.content else "error"
            if not req.turn.content:
                req.turn.meta = {"error": {"message": "Cancelled.", "action": "retry"}}
            if req.needs_images and not req.first_content_applied:
                thread.images_stripped = True  # reload images for the next request
        self.active = None
        self._update_bar()

    def _flush(self) -> None:
        req = self.active
        if req is None:
            self._stop_timer()
            return
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        terminal: tuple[str, Any] | None = None
        while req.events:
            kind, value = req.events.popleft()
            if kind == "content":
                if req.first_token_at is None:
                    req.first_token_at = time.monotonic()
                content_parts.append(value)
            elif kind == "reasoning":
                reasoning_parts.append(value)
            elif kind == "phase":
                self.panel and self.panel.set_phase(req.thread.id, value, {})
            elif kind == "retry":
                self.panel and self.panel.set_phase(req.thread.id, "retrying", value)
            elif kind == "first_content":
                if req.thread.turns:
                    req.thread.turns[0].content = value
                req.thread.images_stripped = False
                req.first_content_applied = True
            elif kind == "images_failed":
                req.turn.meta["images_failed"] = value
            elif kind in ("done", "error", "cancelled"):
                terminal = (kind, value)
                break
        if reasoning_parts:
            text = "".join(reasoning_parts)
            req.turn.reasoning += text
            if not req.thinking:
                req.thinking = True
                self.panel and self.panel.set_phase(req.thread.id, "thinking", {})
            if self.settings.reasoning_display == "collapsible" and req.mode not in HINT_MODES:
                self.panel and self.panel.append_delta(req.thread.id, text, "reasoning")
        if content_parts:
            text = "".join(content_parts)
            req.turn.content += text
            if req.mode not in HINT_MODES:  # hints are checked before rendering
                self.panel and self.panel.append_delta(req.thread.id, text, "content")
        if terminal is not None:
            kind, value = terminal
            if kind == "done":
                self._on_done(req, value)
            elif kind == "error":
                self._on_error(req, value)
            else:
                self._on_cancelled(req, value)

    def _finish_request(self, req: Request) -> None:
        req.finished = True
        if self.active is req:
            self.active = None
        self._stop_timer()
        self._update_bar()

    def _on_done(self, req: Request, result: Any) -> None:
        thread, turn = req.thread, req.turn
        if req.discard:
            self._finish_request(req)
            return
        turn.content = result.content
        turn.reasoning = result.reasoning or turn.reasoning
        turn.reasoning_details = result.reasoning_details or None
        if req.mode in HINT_MODES:
            # Reasoning may spell out the answer; it is never stored or shown.
            turn.reasoning = ""
        usage = result.usage or {}
        self._record_usage(usage)
        if req.mode in HINT_MODES:
            ctx = self.contexts.get(thread.id) or req.ctx
            answers = ctx.answer_hint_strings() if ctx else []
            if answers and leak_guard.leaks(turn.content, answers):
                if req.hint_attempt == 0:
                    log.info("hint leaked the answer; retrying with a stricter instruction")
                    self._finish_request(req)
                    thread.turns.remove(turn)
                    strict = prompts.PromptSet(self.settings).mode("hint_strict")
                    if len(thread.turns) == 1:
                        instr = (req.instruction or "") + "\n\n" + strict
                        thread.turns[0].content = _replace_instruction(thread.turns[0].content, instr)
                        self._request(thread, mode=req.mode, ctx=ctx, needs_images=req.needs_images,
                                      instruction=instr, cache_key=req.cache_key, hint_attempt=1, confirmed=True)
                    else:
                        thread.turns[-1].content = f"{thread.turns[-1].content}\n\n{strict}"
                        self._request(thread, mode=req.mode, hint_attempt=1, confirmed=True)
                    return
                turn.content = leak_guard.GENERIC_HINT
                turn.reasoning_details = None
                turn.meta["generic_hint"] = True
                req.cache_key = None
        turn.status = "done"
        if result.finish_reason == "length" and not turn.content.strip():
            turn.status = "error"
            turn.meta["error"] = {
                "message": "The model used its whole token budget on reasoning. Try a lower reasoning effort or a higher token limit.",
                "action": "retry",
            }
        turn.meta.update(
            {
                "cost": usage.get("cost"),
                "tokens": usage.get("total_tokens"),
                "model": result.model,
                "seconds": round(time.monotonic() - req.started, 1),
                "first_token_ms": int((req.first_token_at - req.started) * 1000) if req.first_token_at else None,
            }
        )
        if req.mode == "chip" and thread.turns[-2].chip == "quiz":
            thread.awaiting_quiz = True
        if req.mode == "chip" and thread.turns[-2].chip == "check":
            turn.meta["edit_card"] = True
        if (
            req.cache_key is not None
            and turn.status == "done"
            and self.settings.ctx("cache_enabled")
            and len(thread.turns) == 2
        ):
            try:
                self.cache.store(
                    req.cache_key, thread.card_id, thread.note_id, turn.content, turn.reasoning,
                    usage, result.model, turn.reasoning_details,
                )
            except Exception:
                log.exception("cache store failed")
        self._save_thread(thread)
        if len(thread.turns) == 2:
            self._record_metric(thread, req.mode, usage.get("cost"))
        self._finish_request(req)
        if self.panel:
            self.panel.finish(thread.id, {"turnId": turn.id, "text": turn.content, "status": turn.status, "meta": turn.meta})
        self._render()

    def _on_error(self, req: Request, err: ApiError) -> None:
        if req.discard:
            self._finish_request(req)
            return
        turn = req.turn
        turn.status = "incomplete" if turn.content else "error"
        info = self._error_info(err)
        turn.meta["error"] = info
        if req.mode in HINT_MODES:
            # Never render an unchecked partial hint (or its reasoning).
            turn.reasoning = ""
            if turn.content:
                turn.content = ""
                turn.status = "error"
        log.info("request error kind=%s status=%s", err.kind, err.status)
        self._finish_request(req)
        if self.panel:
            self.panel.error(req.thread.id, {"turnId": turn.id, **info})
        self._render()

    def _on_cancelled(self, req: Request, reason: str) -> None:
        if req.discard:
            self._finish_request(req)
            return
        turn = req.turn
        if req.mode in HINT_MODES:
            turn.content = ""
            turn.reasoning = ""
        turn.status = "stopped" if turn.content else "error"
        if not turn.content:
            turn.meta["error"] = {"message": "Stopped.", "action": "retry", "actionLabel": "Retry"}
        self._finish_request(req)
        self._render()

    def _error_info(self, err: ApiError) -> dict[str, Any]:
        labels = {
            "retry": ("Retry", None),
            "settings": ("Open settings", None),
            "credits": ("Add credits", CREDITS_URL),
            "provider_settings": ("Open OpenRouter settings", PROVIDER_SETTINGS_URL),
        }
        label, url = labels.get(err.action, ("Retry", None))
        return {"message": err.message, "action": err.action, "actionLabel": label, "url": url, "kind": err.kind}

    def _record_usage(self, usage: dict[str, Any]) -> None:
        self.session["requests"] += 1
        self.session["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.session["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        self.session["cost"] += float(usage.get("cost") or 0.0)
        try:
            self.cache.record_usage(usage)
        except Exception:
            log.exception("usage record failed")

    def _record_metric(self, thread: Thread, mode: str, cost: float | None) -> None:
        try:
            event_id = self.cache.record_event(thread.card_id, thread.mode if thread.mode in MODES else mode, cost)
            self.metric_event = (event_id, thread.card_id, time.monotonic())
            thread_event = getattr(thread, "_metric_event", None)
            if thread_event is None:
                setattr(thread, "_metric_event", event_id)
        except Exception:
            log.exception("metric record failed")

    def _mark_followed_up(self, thread: Thread) -> None:
        event_id = getattr(thread, "_metric_event", None)
        if event_id:
            try:
                self.cache.mark_followed_up(event_id)
            except Exception:
                pass

    def _save_thread(self, thread: Thread) -> None:
        if not self.settings.ctx("cache_enabled"):
            return
        try:
            self.cache.save_thread(thread.to_dict(strip_images=True))
        except Exception:
            log.exception("thread save failed")

    # ------------------------------------------------------------------
    # panel messages (JS → Python)
    # ------------------------------------------------------------------
    def handle_message(self, msg: dict[str, Any]) -> Any:
        op = msg.get("op")
        refocus = False
        if op == "ready":
            self._render()
        elif op == "explain":
            self.explain_pressed(source="panel")
            refocus = True
        elif op == "chip":
            self.run_chip(str(msg.get("id", "")))
            refocus = not msg.get("typing")
        elif op == "ask":
            self.ask(str(msg.get("text", "")))
            refocus = True
        elif op == "stop":
            self.stop_pressed()
        elif op == "copy":
            thread = self.history_thread if self.view == "history_thread" else self._shown_thread()
            turn = thread.last_assistant() if thread else None
            if turn and turn.content:
                self.host.copy_text(turn.content)
                self.host.tooltip("Explanation copied.")
        elif op == "regenerate":
            self.regenerate()
            refocus = True
        elif op == "retry":
            self.retry()
            refocus = True
        elif op == "stronger_hint":
            self._stronger_hint()
            refocus = True
        elif op == "show_answer_explain":
            self._show_answer_and_explain()
            refocus = True
        elif op == "open_settings":
            self.host.open_settings(msg.get("page"))
        elif op == "open_link":
            url = str(msg.get("url", ""))
            if url.startswith(("https://", "http://")):
                self.host.open_link(url)
        elif op == "set_key":
            self._connect(str(msg.get("key", "")))
        elif op == "focus_reviewer":
            self.host.focus_reviewer()
        elif op == "close":
            self.panel and self.panel.hide()
            self.host.focus_reviewer()
        elif op == "history":
            self._open_history()
        elif op == "history_open":
            self._open_history_thread(str(msg.get("id", "")))
        elif op == "history_back":
            self._open_history()
        elif op == "history_close":
            self.history_thread = None
            self.view = "thread" if self.thread else "idle"
            self._render()
        elif op == "continue":
            self._continue_history()
        elif op == "open_browser":
            thread = self.history_thread or self.thread
            if thread:
                self.host.open_browser(thread.card_id)
        elif op == "edit_card":
            thread = self.thread
            if thread:
                self.host.edit_card(thread.card_id)
        elif op == "confirm_send":
            action = self.pending_action
            self.pending_action = None
            self.confirm_message = None
            if action:
                action()
            refocus = True
        elif op == "confirm_cancel":
            self._cancel_confirm()
        elif op == "model_menu":
            self.host.show_model_menu()
        elif op == "input_focus":
            self.input_typing = bool(msg.get("focused"))
        if refocus and not self.input_typing:
            self.host.focus_reviewer()
        return None

    def _connect(self, key: str) -> None:
        key = key.strip()
        if not key:
            self.onboarding = {"busy": False, "error": "Paste your OpenRouter API key first."}
            self._render()
            return
        self.onboarding = {"busy": True, "error": None}
        self._render()
        client = self.client_factory(key)
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["info"] = client.key_info()
            except ApiError as err:
                result["error"] = err
            except Exception as exc:
                log.exception("key check failed: %s", type(exc).__name__)
                result["error"] = make_error("unknown")

        def done() -> None:
            err = result.get("error")
            if err is not None:
                msg = err.message
                if err.kind == "auth":
                    msg = "OpenRouter didn't accept this API key. Check that you copied all of it."
                self.onboarding = {"busy": False, "error": msg}
                self._render()
                return
            self.secrets.set(key)
            self.onboarding = {"busy": False, "error": None}
            self.view = "idle"
            self.host.tooltip("Connected to OpenRouter.")
            action = self.pending_action
            self.pending_action = None
            if action:
                action()
            else:
                self._render()

        self._run_with_callback(work, done)

    def _run_with_callback(self, work: Callable[[], None], done: Callable[[], None]) -> None:
        state = {"done": False}

        def wrapped() -> None:
            try:
                work()
            finally:
                state["done"] = True

        holder: dict[str, Any] = {}

        def poll() -> None:
            if state["done"]:
                holder["timer"].stop()
                done()

        self.host.run_in_background(wrapped)
        holder["timer"] = self.host.start_timer(50, poll)

    def _open_history(self) -> None:
        try:
            self.history_items = self.cache.history(self.host.profile_name(), days=7)
        except Exception:
            log.exception("history failed")
            self.history_items = []
        self.history_thread = None
        self.view = "history"
        self._render()

    def _open_history_thread(self, thread_id: str) -> None:
        live = next((t for t in self.threads.values() if t.id == thread_id), None)
        data = None if live else self.cache.get_thread(thread_id)
        if live is None and data is None:
            self.host.tooltip("That conversation is no longer stored.")
            return
        self.history_thread = live or Thread.from_dict(data)  # type: ignore[arg-type]
        self.view = "history_thread"
        self._render()

    def _continue_history(self) -> None:
        thread = self.history_thread
        if thread is None:
            return
        self.history_thread = None
        self.thread = thread
        key = (thread.card_id, thread.side, selection_hash(thread.selection))
        if thread.id not in self.contexts:
            ctx = None
            try:
                raw = self.host.gather_raw_by_id(thread.card_id, thread.side)
                if raw is not None:
                    ctx = build_context(raw, self.settings, thread.selection)
            except Exception:
                log.exception("could not rebuild context for history thread")
            if ctx is not None:
                self.contexts[thread.id] = ctx
                thread.images_stripped = bool(ctx.image_sources)
        self.threads[key] = thread
        self.view = "thread"
        self._render()
        self.panel and self.panel.focus_input()

    # ------------------------------------------------------------------
    # settings & theme
    # ------------------------------------------------------------------
    def on_settings_changed(self) -> None:
        self._update_bar()
        self._render()

    # ------------------------------------------------------------------
    # view model
    # ------------------------------------------------------------------
    def _render(self, notice: str | None = None) -> None:
        if self.panel is None:
            return
        try:
            self.panel.set_state(self.view_state(notice))
        except Exception:
            log.exception("render failed")

    def view_state(self, notice: str | None = None) -> dict[str, Any]:
        s = self.settings
        view = self.view
        if not self.secrets.has_key() and view not in ("history", "history_thread"):
            view = "onboarding"
        card = self.host.current_card()
        side = self.host.reviewer_side() if card is not None else None
        state: dict[str, Any] = {
            "view": view,
            "notice": notice,
            "settings": {
                "fontSize": s.behavior("font_size"),
                "reduceMotion": s.behavior("reduce_motion"),
                "showCost": s.behavior("show_cost"),
                "accent": s.behavior("accent_color"),
                "reasoningDisplay": s.reasoning_display,
            },
            "shortcuts": {
                "explain": self.host.shortcut_label("explain"),
                "focus": self.host.shortcut_label("focus_input"),
                "stop": self.host.shortcut_label("stop"),
                "chips": [self.host.shortcut_label(f"chip{i}") for i in range(6)],
            },
            "model": {"name": self.model.short_name, "id": self.model.id},
            "hasCard": card is not None,
            "side": side,
            "onboarding": dict(self.onboarding),
            "tip": TIPS[self.tip_index].format(focus=self.host.shortcut_label("focus_input")),
            "session": dict(self.session),
        }
        if card is not None and view in ("idle", "thread", "confirm", "onboarding"):
            summary = self.host.card_summary(card)
            state["card"] = {"kind": summary.get("kind", "Card"), "deck": summary.get("deck", ""), "side": side}
        thread = self.history_thread if view == "history_thread" else self.thread
        if view in ("thread", "confirm", "history_thread") and thread is not None:
            state["thread"] = self._thread_view(thread, read_only=view == "history_thread")
            ctx = self.contexts.get(thread.id)
            strip = {
                "kind": thread.kind_label or "Card",
                "deck": thread.deck,
                "side": thread.side,
                "target": _strip_md(thread.target or thread.selection or ""),
                "sent": thread.sent_summary,
                "notes": [],
            }
            if ctx is not None and ctx.images_not_sent_reason:
                strip["notes"].append(ctx.images_not_sent_reason)
            if ctx is not None and (ctx.image_sources or ctx.skipped_images) and not self.model.vision:
                strip["notes"].append("This model can't see images. Explanation is based on text only.")
            state["card"] = strip
            state["notCurrent"] = bool(
                card is None or card.id != thread.card_id or (self.current and self.current[1] != thread.side)
            )
        if view == "confirm":
            state["confirm"] = {"message": self.confirm_message or ""}
        if view == "history":
            state["history"] = [
                {
                    "id": h["id"],
                    "title": h.get("title") or "(card)",
                    "deck": h.get("deck") or "",
                    "model": get_model(h.get("model") or "").short_name,
                    "time": h.get("updated_at"),
                    "side": h.get("side"),
                }
                for h in self.history_items
            ]
        return state

    def _thread_view(self, thread: Thread, read_only: bool) -> dict[str, Any]:
        busy = bool(self.active and not self.active.finished and self.active.thread is thread)
        chips: list[dict[str, Any]] = []
        if thread.side == "answer" and not read_only:
            labels = [self.host.shortcut_label(f"chip{i}") for i in range(6)]
            for i, cid in enumerate(self._chips_for(thread)):
                chip = prompts.CHIPS[cid]
                chips.append(
                    {"id": cid, "label": chip.label, "desc": chip.description, "key": labels[i] if i < 6 else ""}
                )
        turns = thread.view()
        if busy and self.active is not None:
            for t in turns:
                if t["id"] == self.active.turn.id and self.active.mode in HINT_MODES:
                    t["text"] = ""  # unchecked hint text is never shown
                    t["reasoning"] = ""
        return {
            "id": thread.id,
            "mode": thread.mode,
            "side": thread.side,
            "readOnly": read_only,
            "turns": turns,
            "chips": chips,
            "hintActions": thread.side == "question" and thread.mode == "hint" and not read_only,
            "showAnswerAction": thread.side == "question" and not read_only,
            "awaitingQuiz": thread.awaiting_quiz,
            "busy": busy,
            "busyTurn": self.active.turn.id if busy and self.active else None,
            "busyMode": self.active.mode if busy and self.active else None,
            "hasAnswer": thread.has_first_answer,
            "title": thread.title,
        }

    def shutdown(self) -> None:
        self._cancel_active(discard=True)
        self._stop_timer()
        try:
            self.cache.close()
        except Exception:
            pass


def _strip_md(text: str) -> str:
    return text.replace("**", "").replace("*", "").strip()


def _instruction_of(content: Any) -> str:
    if isinstance(content, list) and content and content[0].get("type") == "text":
        text = content[0]["text"]
        idx = text.rfind("</card>")
        return text[idx + len("</card>"):].strip() if idx >= 0 else text
    return str(content)


def _replace_instruction(content: Any, instruction: str) -> Any:
    if isinstance(content, list) and content and content[0].get("type") == "text":
        text = content[0]["text"]
        idx = text.rfind("</card>")
        new = [dict(p) for p in content]
        new[0]["text"] = (text[: idx + len("</card>")] if idx >= 0 else text) + "\n\n" + instruction
        return new
    return content


def _messages_with_first(thread: Thread, first_content: list[dict[str, Any]], upto: int, drop_old: bool) -> list[dict[str, Any]]:
    messages = thread.api_messages(drop_old_images=drop_old, upto=upto)
    if len(messages) >= 2 and messages[1]["role"] == "user":
        user_turns = sum(1 for m in messages if m["role"] == "user")
        if drop_old and user_turns > 6:
            from .llm.threads import _strip_images

            first_content = _strip_images(first_content)
        messages[1] = {"role": "user", "content": first_content}
    return messages

