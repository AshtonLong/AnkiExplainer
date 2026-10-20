"""Controller behaviour outside Anki: on-demand guarantee, loading phases,
caching, cancellation on card change, hint leak guard, chips, quiz, errors."""

import threading
import time

import pytest

from explainer.controller import Controller
from explainer.llm.leak_guard import GENERIC_HINT
from explainer.llm.openrouter import OpenRouterClient
from explainer.store.cache import Cache
from explainer.store.secrets import SecretStore
from explainer.store.settings import Settings
from conftest import raw_from_fixture
from fake_openrouter import FakeOpenRouter, Reply, text_reply


class FakeCard:
    def __init__(self, cid, fixture, lapses=0):
        self.id = cid
        self.fixture = fixture
        self.lapses = lapses


class Timer:
    def __init__(self, cb):
        self.cb = cb
        self.active = True

    def stop(self):
        self.active = False


class FakeHost:
    def __init__(self):
        self.card = None
        self.side = None
        self.timers = []
        self.calls = []
        self.bar = None
        self.selection = ""
        self.flip_result = True
        self.controller = None

    def current_card(self):
        return self.card

    def reviewer_side(self):
        return self.side

    def card_summary(self, card):
        return {"kind": "Basic", "deck": "Deck"}

    def gather_raw(self, card, side):
        raw = raw_from_fixture(card.fixture, side)
        raw.card_id = card.id
        return raw

    def gather_raw_by_id(self, card_id, side):
        return None

    def selected_text(self):
        return self.selection

    def run_in_background(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def start_timer(self, ms, cb):
        t = Timer(cb)
        self.timers.append(t)
        return t

    def flip_card(self):
        self.calls.append("flip")
        if self.flip_result:
            self.side = "answer"
            self.controller.on_card_shown(self.card, "answer")
        return self.flip_result

    def focus_reviewer(self):
        self.calls.append("focus_reviewer")

    def open_settings(self, page=None):
        self.calls.append(("settings", page))

    def open_link(self, url):
        self.calls.append(("link", url))

    def copy_text(self, text):
        self.calls.append(("copy", text))

    def edit_card(self, cid):
        self.calls.append(("edit", cid))

    def open_browser(self, cid):
        self.calls.append(("browser", cid))

    def profile_name(self):
        return "Test"

    def tooltip(self, msg):
        self.calls.append(("tooltip", msg))

    def show_model_menu(self):
        self.calls.append("model_menu")

    def update_bar(self, state):
        self.bar = state

    def shortcut_label(self, name):
        return {"explain": "X", "focus_input": "Shift+X", "stop": "Ctrl+."}.get(name, "Alt+1")

    def pump(self, until, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for t in list(self.timers):
                if t.active:
                    t.cb()
            if until():
                return
            time.sleep(0.01)
        raise AssertionError("condition not reached")


class FakePanel:
    def __init__(self):
        self.visible = False
        self.states = []
        self.phases = []
        self.deltas = []
        self.focused = False
        self.errors = []

    def is_visible(self):
        return self.visible

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def set_state(self, state):
        self.states.append(state)

    def set_phase(self, thread_id, phase, info):
        self.phases.append(phase)

    def append_delta(self, thread_id, text, kind):
        self.deltas.append((kind, text))

    def finish(self, thread_id, meta):
        pass

    def error(self, thread_id, err):
        self.errors.append(err)

    def focus_input(self):
        self.focused = True

    def has_focus(self):
        return False

    @property
    def last(self):
        return self.states[-1]


@pytest.fixture
def env(tmp_path):
    server = FakeOpenRouter().start()
    host = FakeHost()
    panel = FakePanel()
    secrets = SecretStore(str(tmp_path), use_keyring=False)
    secrets.set("sk-or-v1-testkey-000000")
    settings = Settings({})
    cache = Cache(str(tmp_path / "cache.sqlite3"))
    ctl = Controller(host, settings, secrets, cache,
                     client_factory=lambda key: OpenRouterClient(key, base_url=server.base_url, sleep_backoff=(0.01,) * 3))
    ctl.attach_panel(panel)
    host.controller = ctl
    yield ctl, host, panel, server
    ctl.shutdown()
    server.stop()


def show(ctl, host, card, side):
    host.card, host.side = card, side
    ctl.on_card_shown(card, side)


def idle(ctl):
    return ctl.active is None


def last_assistant_text(ctl):
    return ctl.thread.last_assistant().content


def test_review_session_makes_zero_requests_until_explain(env):
    ctl, host, panel, server = env
    ctl.settings.set("behavior.auto_open_on_answer", True)
    panel.visible = True
    for i in range(25):
        card = FakeCard(1000 + i, "basic")
        show(ctl, host, card, "question")
        show(ctl, host, card, "answer")
    assert server.requests == [] and ctl.network_requests == 0
    server.queue(text_reply("**Because ATP stores energy.**"))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    assert len(server.chat_requests) == 1
    assert last_assistant_text(ctl) == "**Because ATP stores energy.**"


def test_phases_in_order_and_cache_hit(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("Explanation text here.", reasoning="thinking...", chunk_delay=0.01))
    ctl.explain_pressed()
    assert panel.visible
    host.pump(lambda: idle(ctl))
    order = [p for p in panel.phases if p in ("reading", "asking", "thinking")]
    assert order[:3] == ["reading", "asking", "thinking"]
    assert ("content", "Explanation ") in panel.deltas or any(k == "content" for k, _ in panel.deltas)
    # Re-showing the card and pressing X again is served from the cache: no request.
    show(ctl, host, card, "question")
    ctl.threads.clear()
    show(ctl, host, card, "answer")
    ctl.explain_pressed()
    assert len(server.chat_requests) == 1
    turn = ctl.thread.turns[1]
    assert turn.meta.get("cached") and turn.content == "Explanation text here."
    # X again toggles the panel closed.
    ctl.explain_pressed()
    assert not panel.visible


def test_card_change_cancels_and_discards(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply(" ".join(["w"] * 300), words_per_chunk=1, chunk_delay=0.01))
    ctl.explain_pressed()
    host.pump(lambda: any(k == "content" for k, _ in panel.deltas))
    show(ctl, host, FakeCard(2, "basic"), "question")
    assert ctl.active is None and ctl.thread is None
    assert ctl.view == "idle"
    assert ctl.cache.db.execute("SELECT COUNT(*) FROM explanations").fetchone()[0] == 0


def test_stop_keeps_partial_and_does_not_cache(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply(" ".join(["w"] * 300), words_per_chunk=1, chunk_delay=0.01))
    ctl.explain_pressed()
    host.pump(lambda: any(k == "content" for k, _ in panel.deltas))
    ctl.stop_pressed()
    host.pump(lambda: idle(ctl))
    turn = ctl.thread.turns[1]
    assert turn.status == "stopped" and turn.content
    assert ctl.cache.db.execute("SELECT COUNT(*) FROM explanations").fetchone()[0] == 0


def test_chip_quiz_and_grading_flow(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("First."), text_reply("Example!"), text_reply("What is ATP?"), text_reply("Correct."))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    ctl.handle_message({"op": "chip", "id": "example"})
    host.pump(lambda: idle(ctl))
    assert "focus_reviewer" in host.calls
    assert ctl.thread.turns[-1].content == "Example!"
    ctl.run_chip("quiz")
    host.pump(lambda: idle(ctl))
    assert ctl.thread.awaiting_quiz
    ctl.ask("energy currency")
    host.pump(lambda: idle(ctl))
    grading = server.chat_requests[-1]["body"]
    assert "Grade the student's answer" in grading["messages"][-1]["content"]
    assert grading["reasoning"] == {"effort": "medium"}
    assert ctl.thread.turns[-2].display == "energy currency"
    # The follow-up reused the thread: system, card, answer, ... in order.
    roles = [m["role"] for m in grading["messages"]]
    assert roles[:3] == ["system", "user", "assistant"]
    assert grading["messages"][1]["content"][0]["text"].startswith("<card>")


def test_followup_after_cache_hit_reloads_images(env, qapp):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("First."), text_reply("More."))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    first_body = server.chat_requests[0]["body"]
    assert first_body["messages"][1]["content"][1]["type"] == "image_url"
    ctl.threads.clear()
    show(ctl, host, card, "answer")
    ctl.explain_pressed()  # cache hit
    ctl.run_chip("deeper")
    host.pump(lambda: idle(ctl))
    body = server.chat_requests[1]["body"]
    assert body["messages"][1]["content"][1]["type"] == "image_url"
    assert body["reasoning"] == {"effort": "medium"} and body["max_tokens"] == 4000


def test_hint_mode_leak_guard_retries_then_generic(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "type_in")
    show(ctl, host, card, "question")
    server.queue(text_reply("It is propranolol."), text_reply("Still propranolol!"))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl) and len(server.chat_requests) == 2)
    assert ctl.thread.mode == "hint"
    assert ctl.thread.turns[-1].content == GENERIC_HINT
    second = server.chat_requests[1]["body"]
    assert "previous hint revealed the answer" in second["messages"][1]["content"][0]["text"]
    assert second["reasoning"] == {"effort": "none"} and second["max_tokens"] == 300
    # The question side never sends the answer.
    assert "propranolol" not in second["messages"][1]["content"][0]["text"]
    # Hint text is never streamed to the panel before the check.
    assert not any("propranolol" in t for k, t in panel.deltas)


def test_hint_ok_and_show_answer_explain(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "question")
    server.queue(text_reply("Think about the cell's energy carrier."), text_reply("Answer explanation."))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    assert ctl.thread.turns[-1].content == "Think about the cell's energy carrier."
    state = ctl.view_state()
    assert state["thread"]["hintActions"]
    ctl.handle_message({"op": "show_answer_explain"})
    assert "flip" in host.calls
    host.pump(lambda: idle(ctl))
    assert ctl.thread.side == "answer" and ctl.thread.mode == "concise"
    assert ctl.thread.turns[-1].content == "Answer explanation."


def test_errors_mapped_with_actions(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(Reply(status=402, body={"error": {"code": 402, "message": "no credits"}}))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    err = ctl.thread.turns[-1].meta["error"]
    assert err["message"] == "Your OpenRouter credits have run out."
    assert err["actionLabel"] == "Add credits" and err["url"].startswith("https://openrouter.ai/")
    server.queue(text_reply("Recovered."))
    ctl.handle_message({"op": "retry"})
    host.pump(lambda: idle(ctl))
    assert ctl.thread.turns[-1].content == "Recovered."


def test_no_key_onboarding_then_connect_runs_pending(env, tmp_path):
    ctl, host, panel, server = env
    ctl.secrets.remove()
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    ctl.explain_pressed()
    assert panel.last["view"] == "onboarding"
    assert server.requests == []
    server.queue(Reply(body={"data": {"label": "k"}}), text_reply("Hello."))
    ctl.handle_message({"op": "set_key", "key": "sk-or-v1-newkey-123456"})
    host.pump(lambda: len(server.chat_requests) == 1 and idle(ctl))
    assert ctl.secrets.get() == "sk-or-v1-newkey-123456"
    assert server.requests[0]["path"] == "/api/v1/key"


def test_soft_cap_requires_confirmation(env):
    ctl, host, panel, server = env
    ctl.settings.set("usage.monthly_soft_cap_usd", 0.0001)
    ctl.cache.record_usage({"cost": 0.01})
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    ctl.explain_pressed()
    assert panel.last["view"] == "confirm" and server.chat_requests == []
    server.queue(text_reply("ok"))
    ctl.handle_message({"op": "confirm_send"})
    host.pump(lambda: idle(ctl))
    assert len(server.chat_requests) == 1


def test_selection_and_struggle_nudge(env):
    ctl, host, panel, server = env
    ctl.settings.set("behavior.struggle_nudge", True)
    card = FakeCard(1, "basic", lapses=3)
    show(ctl, host, card, "answer")
    assert host.bar["nudge"] and host.bar["label"] == "Explain"
    show(ctl, host, card, "question")
    assert host.bar["label"] == "Hint"
    show(ctl, host, card, "answer")
    server.queue(text_reply("ATP means..."))
    host.selection = "ATP"
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    body = server.chat_requests[0]["body"]
    text = body["messages"][1]["content"][0]["text"]
    assert "SELECTED TEXT TO EXPLAIN: ATP" in text and "Explain 'ATP' as it is used on this card" in text


def test_metrics_time_to_grade(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("x"))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    ctl.on_card_answered(1, 3)
    m = ctl.cache.metrics()
    assert m["explanations"] == 1 and m["median_seconds_to_grade"] is not None


def test_history_and_continue(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("x"))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    ctl.handle_message({"op": "history"})
    items = panel.last["history"]
    assert len(items) == 1 and items[0]["title"] == "What does ATP stand for?"
    ctl.handle_message({"op": "history_open", "id": items[0]["id"]})
    assert panel.last["view"] == "history_thread" and panel.last["thread"]["readOnly"]
    ctl.handle_message({"op": "continue"})
    assert panel.last["view"] == "thread" and not panel.last["thread"]["readOnly"]
    ctl.handle_message({"op": "open_browser"})
    assert ("browser", 1) in host.calls


def test_x_while_loading_does_not_start_a_second_request(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply(" ".join(["w"] * 100), words_per_chunk=1, chunk_delay=0.01))
    ctl.explain_pressed()
    host.pump(lambda: any(k == "content" for k, _ in panel.deltas))
    ctl.explain_pressed()  # pressed again while streaming
    host.pump(lambda: idle(ctl))
    assert len(server.chat_requests) == 1
    assert panel.visible and ctl.thread.turns[-1].status == "done"


def test_free_text_question_thread_and_regenerate(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("Because it stores energy."), text_reply("Fresh answer."))
    ctl.ask("why is it called a currency?")
    host.pump(lambda: idle(ctl))
    assert ctl.thread.mode == "question"
    view = ctl.view_state()["thread"]["turns"]
    assert view[0]["role"] == "user" and view[0]["text"] == "why is it called a currency?"
    ctl.regenerate()
    host.pump(lambda: idle(ctl))
    body = server.chat_requests[-1]["body"]
    assert "The student asks: why is it called a currency?" in body["messages"][1]["content"][0]["text"]
    assert ctl.thread.turns[-1].content == "Fresh answer."


def test_hint_leak_retry_does_not_reask_soft_cap(env):
    ctl, host, panel, server = env
    ctl.settings.set("usage.monthly_soft_cap_usd", 0.0001)
    ctl.cache.record_usage({"cost": 0.01})
    card = FakeCard(1, "type_in")
    show(ctl, host, card, "question")
    server.queue(text_reply("It is propranolol."), text_reply("Think of stage fright."))
    ctl.explain_pressed()
    assert panel.last["view"] == "confirm"
    ctl.handle_message({"op": "confirm_send"})
    host.pump(lambda: idle(ctl) and len(server.chat_requests) == 2)
    assert ctl.view == "thread" and ctl.thread.turns[-1].content == "Think of stage fright."


def test_escape_first_press_outside_panel_keeps_anki_default(env):
    ctl, host, panel, server = env
    panel.visible = True
    assert ctl.escape_pressed() is False  # Anki's own Esc handling still runs
    assert ctl.escape_pressed() is True and not panel.visible  # quick second press closes


def test_hint_reasoning_never_kept(env):
    ctl, host, panel, server = env
    ctl.settings.set("reasoning.display", "collapsible")
    card = FakeCard(1, "basic")
    show(ctl, host, card, "question")
    server.queue(text_reply("Think about the cell's battery.", reasoning="The answer is adenosine triphosphate."))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    assert ctl.thread.turns[-1].reasoning == ""
    assert not any(k == "reasoning" for k, _ in panel.deltas)
    assert "adenosine" not in str(ctl.view_state()).lower()


def test_superseded_hint_is_not_shown(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "question")
    server.queue(text_reply("Think about batteries."),
                 text_reply("Adenosine " + " ".join(["x"] * 200), words_per_chunk=1, chunk_delay=0.01),
                 text_reply("Starts with A."))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    ctl.handle_message({"op": "stronger_hint"})
    host.pump(lambda: ctl.active is not None and ctl.active.turn.content)
    ctl.handle_message({"op": "stronger_hint"})  # supersedes the first stronger hint
    host.pump(lambda: idle(ctl))
    stopped = [t for t in ctl.thread.turns if t.role == "assistant" and t.status != "done"]
    assert all(t.content == "" for t in stopped)


def test_question_thread_does_not_shadow_explain(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("Custom answer."), text_reply("Concise explanation."))
    ctl.ask("what is a phosphate?")
    host.pump(lambda: idle(ctl))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    assert ctl.thread.mode == "concise" and ctl.thread.turns[-1].content == "Concise explanation."


def test_retry_only_applies_to_failed_latest_turn(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("First."), text_reply("Example."))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    ctl.run_chip("example")
    host.pump(lambda: idle(ctl))
    ctl.handle_message({"op": "retry"})  # latest answer succeeded: nothing to retry
    assert len(server.chat_requests) == 2 and ctl.thread.turns[-1].content == "Example."


def test_cancelled_soft_cap_restores_conversation(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("First."), text_reply("Example."))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    ctl.run_chip("example")
    host.pump(lambda: idle(ctl))
    before = [(t.role, t.content) for t in ctl.thread.turns]
    ctl.settings.set("usage.monthly_soft_cap_usd", 0.0001)
    ctl.handle_message({"op": "regenerate"})
    assert ctl.view == "confirm"
    ctl.handle_message({"op": "confirm_cancel"})
    assert [(t.role, t.content) for t in ctl.thread.turns] == before and ctl.view == "thread"


def test_quiz_state_cleared_by_other_chip(env):
    ctl, host, panel, server = env
    card = FakeCard(1, "basic")
    show(ctl, host, card, "answer")
    server.queue(text_reply("First."), text_reply("Question?"), text_reply("Example."), text_reply("Because."))
    ctl.explain_pressed()
    host.pump(lambda: idle(ctl))
    ctl.run_chip("quiz")
    host.pump(lambda: idle(ctl))
    assert ctl.thread.awaiting_quiz
    ctl.run_chip("example")
    host.pump(lambda: idle(ctl))
    assert not ctl.thread.awaiting_quiz
    ctl.ask("why?")
    host.pump(lambda: idle(ctl))
    assert "Grade the student's answer" not in server.chat_requests[-1]["body"]["messages"][-1]["content"]
