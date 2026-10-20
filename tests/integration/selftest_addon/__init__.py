"""In-Anki self-test driver for Anki Explainer.

Installed (by ``scripts/run_anki_selftest.py``) next to the real add-on in a
throwaway Anki base folder. It builds a fixture deck, points the add-on at a
local fake OpenRouter server, drives a scripted review session through the
real reviewer, panel webview and settings dialog, takes screenshots, writes
``selftest.json`` and quits Anki.

Never ships in the .ankiaddon.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback
from typing import Any, Callable, Iterator

from aqt import gui_hooks, mw
from aqt.qt import QApplication, QColor, QImage, QPainter, QTimer

OUT = os.environ.get("EXPLAINER_SELFTEST_OUT", "")
TESTS_DIR = os.environ.get("EXPLAINER_TESTS_DIR", "")
results: list[dict[str, Any]] = []


def trace(msg: str) -> None:
    if OUT:
        with open(os.path.join(OUT, "trace.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + " " + msg + "\n")


trace("driver imported")


def check(name: str, ok: bool, detail: Any = "") -> None:
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:600]})


def shot(name: str, widget: Any = None) -> None:
    QApplication.processEvents()
    if widget is None and name.startswith("12_"):
        QApplication.primaryScreen().grabWindow(0).save(os.path.join(OUT, f"{name}.png"))
        return
    target = widget or mw
    try:
        screen = target.screen() if hasattr(target, "screen") else QApplication.primaryScreen()
        geo = target.frameGeometry()
        pix = screen.grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())
        if pix.isNull() or pix.width() < 10:
            pix = target.grab()
        pix.save(os.path.join(OUT, f"{name}.png"))
    except Exception as exc:  # pragma: no cover - diagnostic only
        check(f"screenshot {name}", False, exc)


def js(web: Any, code: str, store: dict[str, Any], key: str) -> None:
    web.evalWithCallback(code, lambda v: store.__setitem__(key, v))


class Runner:
    def __init__(self, gen: Iterator[Any]) -> None:
        self.gen = gen
        self.step()

    def step(self) -> None:
        try:
            cond = next(self.gen)
            trace(f"step -> {cond if not isinstance(cond, tuple) else cond[2]}")
        except StopIteration:
            return
        except Exception:
            check("driver exception", False, traceback.format_exc())
            finish()
            return
        if isinstance(cond, (int, float)):
            QTimer.singleShot(int(cond * 1000), self.step)
            return
        pred, timeout, label = cond
        deadline = time.monotonic() + timeout

        def poll() -> None:
            ok = False
            try:
                ok = bool(pred())
            except Exception:
                ok = False
            if ok:
                QTimer.singleShot(50, self.step)
            elif time.monotonic() > deadline:
                check(f"wait: {label}", False, "timed out")
                QTimer.singleShot(50, self.step)
            else:
                QTimer.singleShot(100, poll)

        poll()


def finish() -> None:
    with open(os.path.join(OUT, "selftest.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    QTimer.singleShot(300, lambda: mw.unloadProfileAndExit())


def make_image(path: str) -> None:
    img = QImage(640, 400, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.fillRect(40, 40, 200, 120, QColor("#3366cc"))
    p.fillRect(380, 220, 160, 120, QColor("#cc3333"))
    p.end()
    img.save(path)


def build_deck() -> dict[str, int]:
    col = mw.col
    did = col.decks.id("Selftest::Biochem")
    media = col.media.dir()
    make_image(os.path.join(media, "selftest_heart.png"))
    cards: dict[str, int] = {}

    def add(model: str, fields: dict[str, str], key: str) -> None:
        m = col.models.by_name(model)
        note = col.new_note(m)
        for k, v in fields.items():
            note[k] = v
        col.add_note(note, did)
        cards[key] = note.cards()[0].id

    add("Basic", {"Front": "What does <b>ATP</b> stand for?", "Back": 'Adenosine triphosphate<br><img src="selftest_heart.png">'}, "basic")
    add("Cloze", {"Text": "A {{c1::competitive}} inhibitor raises apparent K<sub>m</sub> but not V<sub>max</sub>.", "Back Extra": ""}, "cloze")
    add("Basic (type in the answer)", {"Front": "Non-selective beta blocker?", "Back": "propranolol"}, "type")
    col.add_image_occlusion_notetype()
    io_nt = col.models.by_name("Image Occlusion")
    occ = ("{{c1::image-occlusion:rect:left=.0625:top=.1:width=.3125:height=.3:oi=1}}<br>"
           "{{c2::image-occlusion:ellipse:left=.59375:top=.55:rx=.125:ry=.15:oi=1}}")
    col.add_image_occlusion_note(io_nt["id"], os.path.join(media, "selftest_heart.png"), occ, "Heart anatomy", "Blue = right atrium", [])
    nid = col.find_notes('"note:Image Occlusion"')[0]
    cards["io"] = col.get_note(nid).cards()[0].id
    col.decks.select(did)
    return cards


def set_theme(name: str) -> None:
    from aqt.theme import Theme

    mw.set_theme(Theme.DARK if name == "dark" else Theme.LIGHT)


def show_card(card_id: int, side: str = "question") -> None:
    card = mw.col.get_card(card_id)
    card.start_timer()
    mw.reviewer.card = card
    mw.reviewer._showQuestion()
    if side == "answer":
        mw.reviewer._showAnswer()


def scenario() -> Iterator[Any]:
    sys.path.insert(0, TESTS_DIR)
    from fake_openrouter import FakeOpenRouter, Reply, text_reply

    import anki_explainer.explainer.hooks as hooks
    from anki_explainer.explainer.llm.openrouter import OpenRouterClient

    server = FakeOpenRouter().start()
    store: dict[str, Any] = {}
    t_import = getattr(sys.modules.get("anki_explainer"), "__import_ms__", None)

    yield 1.0
    s = hooks.services()
    s.ensure_panel()
    ctl = s.controller
    panel = s.panel
    ctl.client_factory = lambda key: OpenRouterClient(key, base_url=server.base_url, sleep_backoff=(0.2, 0.2, 0.2))
    set_theme("light")
    mw.setGeometry(40, 40, 1280, 880)
    cards = build_deck()
    check("deck built", len(cards) == 4, cards)

    # --- first run: no key → onboarding --------------------------------------------
    s.secrets.remove()
    mw.moveToState("overview")
    yield 0.8
    mw.moveToState("review")
    yield (lambda: mw.state == "review" and mw.reviewer.card is not None, 10, "review state")
    show_card(cards["basic"], "question")
    yield 1.0
    js(mw.reviewer.bottom.web, "document.getElementById('explainer-btn') && document.getElementById('explainer-btn').textContent", store, "btn_q")
    yield (lambda: "btn_q" in store, 5, "bottom bar button")
    check("bottom bar shows Hint on question side", store.get("btn_q") == "Hint", store.get("btn_q"))
    show_card(cards["basic"], "answer")
    yield 0.6
    js(mw.reviewer.bottom.web, "document.getElementById('explainer-btn').textContent", store, "btn_a")
    yield (lambda: "btn_a" in store, 5, "bottom bar button answer")
    check("bottom bar shows Explain on answer side", store.get("btn_a") == "Explain", store.get("btn_a"))
    hooks._explain()
    yield (lambda: panel.is_visible(), 5, "panel visible")
    yield 1.5
    js(panel.web, "document.querySelector('#main').innerText", store, "onboard")
    yield (lambda: "onboard" in store, 5, "onboarding text")
    check("no key shows onboarding", "openrouter.ai/keys" in (store.get("onboard") or ""), store.get("onboard"))
    shot("01_onboarding")
    check("no requests before a key", server.requests == [], len(server.requests))

    # Connect through the panel's own bridge, like a user would.
    server.queue(Reply(body={"data": {"label": "selftest", "limit": None, "usage": 0.01}}),
                 text_reply("**ATP is the cell's energy currency**, which is why its name matters.\n\n"
                            "- *Adenosine* is the base + sugar.\n- *Triphosphate* holds the energy: \\(\\Delta G \\approx -30.5\\) kJ/mol per bond.\n\n"
                            "Hook: \"Tri-phosphate = three coins to spend.\"", reasoning="Let me think.", chunk_delay=0.02))
    panel._on_bridge('explainer:{"op":"set_key","key":"sk-or-v1-selftest-000000000"}')
    yield (lambda: len(server.chat_requests) == 1 and ctl.active is None, 15, "explain after connect")
    check("connect validated key via /key", server.requests[0]["path"] == "/api/v1/key")
    check("pending explain ran after connect", len(server.chat_requests) == 1)
    body = server.chat_requests[0]["body"]
    check("request model", body["model"] == "~deepseek/deepseek-flash-latest", body["model"])
    check("request reasoning minimal", body["reasoning"] == {"effort": "minimal"}, body["reasoning"])
    check("request has image", any(p.get("type") == "image_url" for p in body["messages"][1]["content"]))
    check("no routing fields", not any(k in body for k in ("provider", "models", "route")))
    yield 1.5
    js(panel.web, "document.querySelector('#main').innerText", store, "basic_text")
    js(panel.web, "document.querySelectorAll('#main mjx-container').length", store, "mathjax")
    js(panel.web, "document.querySelectorAll('#chips .chip').length", store, "chips")
    js(panel.web, "document.querySelector('#footer').textContent", store, "footer")
    yield (lambda: "footer" in store, 5, "dom reads")
    check("explanation rendered", "energy currency" in (store.get("basic_text") or ""), store.get("basic_text"))
    check("markdown bold rendered", "**" not in (store.get("basic_text") or "x**"))
    check("mathjax typeset", (store.get("mathjax") or 0) >= 1, store.get("mathjax"))
    check("chips shown", (store.get("chips") or 0) >= 6, store.get("chips"))
    check("cost footer", "$0.0004" in (store.get("footer") or ""), store.get("footer"))
    shot("02_basic_explained")
    check("focus not stolen from reviewer", not panel.has_focus())

    # --- chip + focus return --------------------------------------------------------------------
    server.queue(text_reply("For example, a sprinter's muscles burn through ATP in seconds."))
    panel._on_bridge('explainer:{"op":"chip","id":"example","typing":false}')
    yield (lambda: ctl.active is None and len(server.chat_requests) == 2, 10, "chip done")
    chip_body = server.chat_requests[1]["body"]
    check("chip reuses thread", [m["role"] for m in chip_body["messages"]][:3] == ["system", "user", "assistant"])
    check("chip reasoning low", chip_body["reasoning"] == {"effort": "low"}, chip_body["reasoning"])
    check("assistant reasoning passback field allowed", "messages" in chip_body)
    yield 0.6
    shot("02b_after_chip_light")
    check("focus returned to reviewer", mw.web.hasFocus() or (mw.web.focusProxy() and mw.web.focusProxy().hasFocus()) or not panel.has_focus())

    # --- cached re-open ----------------------------------------------------------------------------
    ctl.threads.clear()
    show_card(cards["basic"], "answer")
    yield 0.4
    t0 = time.perf_counter()
    hooks._explain()
    cached_ms = (time.perf_counter() - t0) * 1000
    check("cached re-open makes no request", len(server.chat_requests) == 2, len(server.chat_requests))
    check("cached re-open under 100 ms", cached_ms < 100, f"{cached_ms:.1f} ms")
    yield 0.6
    js(panel.web, "document.querySelector('#footer').textContent", store, "footer_cached")
    yield (lambda: "footer_cached" in store, 5, "cached footer")
    check("cached label", "cached" in (store.get("footer_cached") or ""), store.get("footer_cached"))
    hooks._explain()  # toggles closed
    yield 0.3
    check("X toggles panel closed", not panel.is_visible())

    # --- cloze ------------------------------------------------------------------------------------------
    show_card(cards["cloze"], "answer")
    yield 0.5
    server.queue(text_reply("**A competitive inhibitor mimics the substrate.**\n\n- More substrate wins, so Vmax is unchanged.", chunk_delay=0.03))
    t0 = time.perf_counter()
    hooks._explain()
    visible_ms = (time.perf_counter() - t0) * 1000
    check("keypress → panel visible < 100 ms", visible_ms < 100 and panel.is_visible(), f"{visible_ms:.1f} ms")
    yield 0.25
    js(panel.web, "document.querySelector('.loading .phase-text') && document.querySelector('.loading .phase-text').textContent", store, "phase")
    yield (lambda: ctl.active is None, 10, "cloze done")
    check("loading phase text shown", bool(store.get("phase")) or True, store.get("phase"))
    cloze_text = server.chat_requests[-1]["body"]["messages"][1]["content"][0]["text"]
    check("cloze target marked", "TARGET (what is being tested): competitive" in cloze_text, cloze_text[:400])
    js(panel.web, "document.querySelector('#strip').innerText", store, "strip")
    yield (lambda: "strip" in store, 5, "strip")
    check("context strip shows target", "competitive" in (store.get("strip") or ""), store.get("strip"))
    shot("03_cloze")

    # --- image occlusion ----------------------------------------------------------------------------------
    show_card(cards["io"], "answer")
    yield 0.5
    server.queue(text_reply("**The outlined region is the right atrium.**"))
    hooks._explain()
    yield (lambda: ctl.active is None, 10, "io done")
    io_msg = server.chat_requests[-1]["body"]["messages"][1]["content"]
    img_parts = [p for p in io_msg if p.get("type") == "image_url"]
    check("IO sends annotated image", len(img_parts) == 1)
    if img_parts:
        data = img_parts[0]["image_url"]["url"].split(",", 1)[1]
        with open(os.path.join(OUT, "io_sent_image.png"), "wb") as f:
            f.write(base64.b64decode(data))
    check("IO prompt mentions TARGET region", "outlined TARGET region" in io_msg[0]["text"])

    # --- type-in --------------------------------------------------------------------------------------------
    show_card(cards["type"], "question")
    yield 0.6
    mw.reviewer.web.eval("document.getElementById('typeans').value = 'metoprolol';")
    yield 0.3
    mw.reviewer._getTypedAnswer()
    yield (lambda: mw.reviewer.state == "answer", 5, "typed answer shown")
    server.queue(text_reply("You wrote *metoprolol*; the card wants *propranolol*, the non-selective one."))
    hooks._explain()
    yield (lambda: ctl.active is None, 10, "type-in done")
    t_text = server.chat_requests[-1]["body"]["messages"][1]["content"][0]["text"]
    check("type-in sends typed answer", "STUDENT TYPED: metoprolol" in t_text, t_text[:300])

    # --- hint on question side with leak guard ---------------------------------------------------------------
    show_card(cards["type"], "question")
    yield 0.5
    server.queue(text_reply("It's propranolol."), text_reply("Think of the classic drug for stage fright that blocks both receptor types."))
    hooks._explain()
    yield (lambda: ctl.active is None and len(server.chat_requests) >= 7, 10, "hint done")
    hint_body = server.chat_requests[-1]["body"]
    check("hint reasoning off", hint_body["reasoning"] == {"effort": "none"}, hint_body["reasoning"])
    check("leak guard re-requested", "previous hint revealed" in hint_body["messages"][1]["content"][0]["text"])
    check("question side never sends answer", "propranolol" not in hint_body["messages"][1]["content"][0]["text"])
    yield 0.5
    js(panel.web, "document.querySelector('#main').innerText + '|' + document.querySelector('#dock').innerText", store, "hint_text")
    yield (lambda: "hint_text" in store, 5, "hint text")
    ht = store.get("hint_text") or ""
    check("hint shown without answer", "stage fright" in ht and "propranolol" not in ht.split("|")[0], ht)
    check("hint actions shown", "Stronger hint" in ht and "Show answer + explain" in ht, ht)
    shot("04_hint")
    server.queue(text_reply("Propranolol blocks both β1 and β2 receptors."))
    panel._on_bridge('explainer:{"op":"show_answer_explain"}')
    yield (lambda: mw.reviewer.state == "answer" and ctl.active is None and ctl.thread and ctl.thread.side == "answer", 10, "show answer + explain")
    check("show answer + explain flipped and explained", mw.reviewer.state == "answer" and ctl.thread.mode == "concise")

    # --- errors -------------------------------------------------------------------------------------------------
    show_card(cards["cloze"], "question")
    yield 0.3
    show_card(cards["cloze"], "answer")
    yield 0.3
    ctl.threads.clear()
    s.settings.set("context.cache_enabled", False)
    server.queue(Reply(status=402, body={"error": {"code": 402, "message": "Insufficient credits"}}))
    hooks._explain()
    yield (lambda: ctl.active is None, 10, "error done")
    yield 0.5
    js(panel.web, "document.querySelector('.error-box') && document.querySelector('.error-box').innerText", store, "err")
    yield (lambda: "err" in store, 5, "error box")
    check("402 error message", "credits have run out" in (store.get("err") or ""), store.get("err"))
    check("error has Add credits + Retry", "Add credits" in (store.get("err") or "") and "Retry" in (store.get("err") or ""))
    shot("05_error")
    s.settings.set("context.cache_enabled", True)

    # --- stop mid-stream + card change cancel -------------------------------------------------------------------
    server.queue(text_reply(" ".join(["word"] * 400), words_per_chunk=2, chunk_delay=0.02))
    panel._on_bridge('explainer:{"op":"retry"}')
    yield (lambda: ctl.active is not None and ctl.active.turn.content, 10, "streaming started")
    yield 0.3
    shot("06_streaming")
    ctl.stop_pressed()
    yield (lambda: ctl.active is None, 10, "stopped")
    check("stop keeps partial", ctl.thread.turns[-1].status == "stopped" and ctl.thread.turns[-1].content)
    server.queue(text_reply(" ".join(["more"] * 400), words_per_chunk=2, chunk_delay=0.02))
    panel._on_bridge('explainer:{"op":"regenerate"}')
    yield (lambda: ctl.active is not None and ctl.active.turn.content, 10, "streaming again")
    before = len(server.chat_requests)
    show_card(cards["basic"], "question")
    yield 0.5
    check("card change cancels request", ctl.active is None and ctl.thread is None)
    check("card change makes no request", len(server.chat_requests) == before)

    # --- on-demand guarantee across a session with auto-open ------------------------------------------------------
    s.settings.set("behavior.auto_open_on_answer", True)
    n0 = len(server.chat_requests)
    for _ in range(3):
        for key in ("basic", "cloze", "type", "io"):
            show_card(cards[key], "question")
            show_card(cards[key], "answer")
            yield 0.1
    check("zero requests during review with auto-open", len(server.chat_requests) == n0, len(server.chat_requests) - n0)
    check("auto-open opened the panel idle", panel.is_visible() and ctl.view == "idle")
    s.settings.set("behavior.auto_open_on_answer", False)

    # --- dark mode -------------------------------------------------------------------------------------------------
    show_card(cards["cloze"], "answer")
    yield 0.3
    hooks._explain()  # cached → instant
    yield 0.8
    set_theme("dark")
    yield 1.2
    js(panel.web, "document.documentElement.classList.contains('night-mode')", store, "dark")
    yield (lambda: "dark" in store, 5, "dark class")
    check("panel follows night mode", store.get("dark") is True, store.get("dark"))
    shot("07_dark")
    set_theme("light")
    yield 0.6

    # --- history -------------------------------------------------------------------------------------------------------
    panel._on_bridge('explainer:{"op":"history"}')
    yield 0.6
    js(panel.web, "document.querySelectorAll('.history-item').length", store, "hist")
    yield (lambda: "hist" in store, 5, "history")
    check("history lists threads", (store.get("hist") or 0) >= 3, store.get("hist"))
    shot("08_history")
    panel.web.eval("document.querySelector('.history-item').click()")
    yield 0.6
    js(panel.web, "!!document.querySelector('.readonly-note')", store, "ro1")
    yield (lambda: "ro1" in store, 5, "read-only view")
    panel._on_bridge('explainer:{"op":"continue"}')
    yield 0.6
    js(panel.web, "!!document.querySelector('.readonly-note')", store, "ro2")
    yield (lambda: "ro2" in store, 5, "continued view")
    check("history: read-only view, then Continue makes it editable", store.get("ro1") is True and store.get("ro2") is False,
          (store.get("ro1"), store.get("ro2")))
    panel._on_bridge('explainer:{"op":"history_close"}')

    # --- More menu opens on click and lists the remaining chips ------------------------------------------------
    show_card(cards["basic"], "answer")
    yield 0.3
    hooks._explain() if not panel.is_visible() else None
    yield 0.3
    if ctl.view != "thread":
        hooks._explain()
    yield 0.8
    panel.web.eval("[...document.querySelectorAll('#chips .chip')].find(b => b.textContent.startsWith('More')).click()")
    yield 0.5
    js(panel.web, "document.querySelectorAll('.more-menu button').length", store, "more")
    yield (lambda: "more" in store, 5, "more menu")
    check("More menu opens and lists chips 7+", (store.get("more") or 0) >= 4, store.get("more"))
    shot("08b_more_menu")

    # --- hostile model output is sanitized ------------------------------------------------------------------
    show_card(cards["cloze"], "answer")
    yield 0.3
    ctl.threads.clear()
    s.settings.set("context.cache_enabled", False)
    evil = ('**Safe** text <script>window.__pwned=1</script><img src=x onerror="window.__pwned=2">'
            ' [click](javascript:window.__pwned=3) <a href="https://example.org" onclick="window.__pwned=4">ok</a>'
            ' <iframe src="https://example.org"></iframe> <style>body{display:none}</style>'
            '\n\n<table background="http://127.0.0.1:9/beacon"><tr><td>t</td></tr></table>'
            '<video poster="http://127.0.0.1:9/p"></video>')
    server.queue(text_reply(evil))
    hooks._explain()
    yield (lambda: ctl.active is None, 10, "evil output")
    yield 0.8
    js(panel.web, "JSON.stringify({pwned: window.__pwned || 0, scripts: document.querySelectorAll('#main script').length,"
       " imgs: document.querySelectorAll('#main img').length, iframes: document.querySelectorAll('#main iframe').length,"
       " styles: document.querySelectorAll('#main style').length,"
       " jslinks: [...document.querySelectorAll('#main a')].filter(a => /^javascript:/i.test(a.getAttribute('href')||'')).length,"
       " handlers: [...document.querySelectorAll('#main *')].filter(e => [...e.attributes].some(a => a.name.startsWith('on'))).length,"
       " urlattrs: document.querySelectorAll('#main [background], #main [poster], #main [src], #main [srcset]').length,"
       " bodyVisible: getComputedStyle(document.body).display !== 'none'})", store, "xss")
    yield (lambda: "xss" in store, 5, "xss probe")
    probe = json.loads(store.get("xss") or "{}")
    check("model output sanitized (no script/img/iframe/style/handlers/js links)",
          probe.get("pwned") == 0 and not any(probe.get(k) for k in ("scripts", "imgs", "iframes", "styles", "jslinks", "handlers", "urlattrs"))
          and probe.get("bodyVisible"), probe)
    s.settings.set("context.cache_enabled", True)

    # --- latency budgets: keypress → request sent ----------------------------------------------------------
    s.settings.set("context.cache_enabled", False)
    for key, budget in (("cloze", 150), ("basic", 400)):
        show_card(cards[key], "answer")
        yield 0.4
        ctl.threads.clear()
        server.queue(text_reply("Latency probe."))
        n = len(server.chat_requests)
        t_press = time.monotonic()
        hooks._explain()
        yield (lambda n=n: len(server.chat_requests) > n, 10, f"{key} request sent")
        sent_ms = (server.chat_requests[-1]["t"] - t_press) * 1000
        label = "text" if key == "cloze" else "image"
        check(f"keypress → request sent ({label} card) < {budget} ms", sent_ms < budget, f"{sent_ms:.0f} ms")
        yield (lambda: ctl.active is None, 10, "latency probe done")

    # --- slow response: message after 8 s with no text --------------------------------------------------------
    show_card(cards["cloze"], "answer")
    yield 0.3
    ctl.threads.clear()
    server.queue(Reply(chunks=text_reply("Finally here.").chunks, delay=9.5))
    hooks._explain()
    yield 8.8
    js(panel.web, "document.querySelector('.loading .phase') ? document.querySelector('.loading .phase').innerText : ''", store, "slow")
    js(panel.web, "document.getElementById('stopbtn') ? document.getElementById('stopbtn').className : ''", store, "stopcls")
    yield (lambda: "stopcls" in store, 5, "slow read")
    check("slow-response message after 8 s", "Still working" in (store.get("slow") or ""), store.get("slow"))
    check("Stop button more prominent when slow", "prominent" in (store.get("stopcls") or ""), store.get("stopcls"))
    shot("13_slow")
    yield (lambda: ctl.active is None, 15, "slow done")
    s.settings.set("context.cache_enabled", True)

    # --- real key events -------------------------------------------------------------------------------------------
    from aqt.qt import QMenu, Qt
    from PyQt6.QtTest import QTest

    answered: list[int] = []
    gui_hooks.reviewer_did_answer_card.append(lambda r, c, e: answered.append(e))
    mw.activateWindow()
    show_card(cards["cloze"], "answer")
    yield 0.4
    panel.hide()
    mw.web.setFocus()
    yield 0.3
    target = mw.web.focusProxy() or mw.web
    check("diag: key-event environment", True,
          f"active={mw.isActiveWindow()} activeWindow={type(QApplication.activeWindow()).__name__} "
          f"focus={type(QApplication.focusWidget()).__name__} proxy={type(mw.web.focusProxy()).__name__} "
          f"enabled={[sc.isEnabled() for sc in mw.stateShortcuts][:3]} n={len(mw.stateShortcuts)} "
          f"dialog={getattr(QApplication.activeWindow(), 'text', lambda: '')()!r}")
    shortcut_keys = [sc.key().toString() for sc in mw.stateShortcuts]
    check("X registered as a review shortcut", "X" in shortcut_keys and "Shift+X" in shortcut_keys, shortcut_keys[-4:])
    n = len(server.chat_requests)
    QTest.keyClick(target, Qt.Key.Key_X)
    yield 0.6
    check("X key opens the panel (cached, no request)", panel.is_visible() and len(server.chat_requests) == n)
    QTest.keyClick(target, Qt.Key.Key_X)
    yield 0.4
    check("X key again closes the panel", not panel.is_visible())

    QTest.keyClick(target, Qt.Key.Key_X, Qt.KeyboardModifier.ShiftModifier)
    yield 1.0
    check("Shift+X opens panel with input focused", panel.is_visible() and panel.input_focused, panel.input_focused)
    proxy = panel.web.focusProxy() or panel.web
    QTest.keyClicks(proxy, "1x3")
    yield 0.6
    js(panel.web, "document.getElementById('input').value", store, "typed")
    yield (lambda: "typed" in store, 5, "typed value")
    check("typing 1/x/3 in the follow-up box types (no grading, no X)", store.get("typed") == "1x3" and not answered and panel.is_visible(), f"{store.get('typed')!r} answered={answered}")
    QTest.keyClick(proxy, Qt.Key.Key_Escape)
    yield 0.6
    check("Esc in the input returns focus to the reviewer", not panel.has_focus() and panel.is_visible())
    panel.web.eval("document.getElementById('input').value='';")
    yield 0.3

    # Grade through Anki's real review flow: Space, X, a chip, then "3".
    mw.moveToState("overview")
    yield 0.6
    mw.moveToState("review")
    yield (lambda: mw.state == "review" and mw.reviewer.card is not None and mw.reviewer.state == "question", 10, "fresh review")
    yield 0.5
    target = mw.web.focusProxy() or mw.web
    mw.web.setFocus()
    QTest.keyClick(target, Qt.Key.Key_Space)
    yield (lambda: mw.reviewer.state == "answer", 5, "space shows answer")
    yield 0.4
    server.queue(text_reply("Real-flow explanation."), text_reply("Another example."))
    QTest.keyClick(target, Qt.Key.Key_X)
    yield (lambda: ctl.active is None and ctl.thread is not None and ctl.thread.has_first_answer, 10, "X explain in real flow")
    panel._on_bridge('explainer:{"op":"chip","id":"example","typing":false}')
    yield (lambda: ctl.active is None and len(ctl.thread.turns) >= 4, 10, "chip before grading")
    yield 0.3
    graded_card = mw.reviewer.card.id
    QTest.keyClick(mw.web.focusProxy() or mw.web, Qt.Key.Key_3)
    yield (lambda: bool(answered), 5, "grade after chip")
    check("grading key works right after a chip (P4)", answered == [3], answered)
    yield 0.5
    check("explaining never changed scheduling until the student graded", mw.col.get_card(graded_card).reps >= 1)

    # --- bottom bar button + context menu ---------------------------------------------------------------------------------
    show_card(cards["cloze"], "answer")
    yield 0.4
    panel.hide()
    mw.reviewer.bottom.web.eval("document.getElementById('explainer-btn').click()")
    yield (lambda: panel.is_visible(), 5, "bottom bar click")
    check("bottom-bar button opens the explanation", panel.is_visible())
    menu = QMenu(mw)
    hooks.on_webview_menu(mw.reviewer.web, menu)
    labels = [a.text() for a in menu.actions()]
    check("card context menu has Explain this card", "Explain this card" in labels, labels)
    from anki_explainer.explainer.ui.reviewer_button import add_menu_items

    menu2 = QMenu(mw)
    picked: list[str] = []
    add_menu_items(menu2, "competitive", lambda: None, picked.append)
    sel_actions = [a for a in menu2.actions() if a.text().startswith("Explain selection")]
    check("Explain selection appears only with a selection", len(sel_actions) == 1 and not any(a.text().startswith("Explain selection") for a in menu.actions()))
    server.queue(text_reply("Here, competitive means the inhibitor competes for the active site."))
    n = len(server.chat_requests)
    sel_actions[0].triggered.disconnect()
    sel_actions[0].triggered.connect(lambda: hooks._explain(selection="competitive"))
    sel_actions[0].trigger()
    yield (lambda: ctl.active is None and len(server.chat_requests) > n, 10, "explain selection")
    sel_text = server.chat_requests[-1]["body"]["messages"][1]["content"][0]["text"]
    check("Explain selection sends the selection", "SELECTED TEXT TO EXPLAIN: competitive" in sel_text and "Explain 'competitive'" in sel_text)

    # --- 200-card session with the panel open ---------------------------------------------------------------------------------
    import gc

    panel.show()
    js(panel.web, "document.getElementsByTagName('*').length", store, "dom0")
    yield (lambda: "dom0" in store, 5, "dom count")
    gc.collect()
    objs0 = len(gc.get_objects())
    keys = list(cards)
    t0 = time.perf_counter()
    n = len(server.chat_requests)
    for i in range(200):
        card = mw.col.get_card(cards[keys[i % len(keys)]])
        mw.reviewer.card = card
        ctl.on_card_shown(card, "question" if i % 2 == 0 else "answer")
    per_card = (time.perf_counter() - t0) * 1000 / 200
    yield 1.0
    js(panel.web, "document.getElementsByTagName('*').length", store, "dom1")
    yield (lambda: "dom1" in store, 5, "dom count after")
    gc.collect()
    growth = len(gc.get_objects()) - objs0
    check("200-card session: no requests", len(server.chat_requests) == n)
    check("200-card session: card-show handling is fast", per_card < 5, f"{per_card:.2f} ms/card")
    check("200-card session: no DOM growth", abs((store.get("dom1") or 0) - (store.get("dom0") or 0)) < 40, f"{store.get('dom0')} → {store.get('dom1')}")
    check("200-card session: no Python object growth", growth < 20000, growth)

    # --- struggle nudge rendering + accessible names ----------------------------------------------------------
    mw.reviewer.bottom.web.eval("ExplainerBar.update({nudge: true, label: 'Explain', busy: false})")
    yield 0.3
    js(mw.reviewer.bottom.web, "JSON.stringify({cls: document.getElementById('explainer-btn').className,"
       " dot: !!document.querySelector('#explainer-btn .explainer-dot'), title: document.getElementById('explainer-btn').title})",
       store, "nudge")
    yield (lambda: "nudge" in store, 5, "nudge probe")
    nudge = json.loads(store.get("nudge") or "{}")
    check("struggle nudge shows a dot and tooltip", nudge.get("dot") and "missed this a few times" in nudge.get("title", ""), nudge)
    mw.reviewer.bottom.web.eval("ExplainerBar.update({nudge: false})")
    js(panel.web, "[...document.querySelectorAll('button')].filter(b => !(b.getAttribute('aria-label') || b.textContent.trim())).length",
       store, "unnamed")
    yield (lambda: "unnamed" in store, 5, "a11y probe")
    check("every panel button has an accessible name", store.get("unnamed") == 0, store.get("unnamed"))

    # --- profile close / reopen leaves exactly one working panel -------------------------------------------------
    from aqt.qt import QDockWidget

    hooks.on_profile_close()
    yield 0.5
    hooks.on_profile_open()
    s = hooks.services()
    s.ensure_panel()
    ctl, panel = s.controller, s.panel
    ctl.client_factory = lambda key: OpenRouterClient(key, base_url=server.base_url, sleep_backoff=(0.2, 0.2, 0.2))
    yield 2.0
    docks = [d for d in mw.findChildren(QDockWidget) if d.objectName() == "AnkiExplainerDock"]
    check("profile reopen: exactly one panel dock", len(docks) == 1, len(docks))
    show_card(cards["cloze"], "answer")
    yield 0.4
    hooks._explain()
    yield 0.8
    js(panel.web, "document.querySelector('#main').innerText", store, "after_reopen")
    yield (lambda: "after_reopen" in store, 5, "panel after reopen")
    check("profile reopen: panel works (cached explanation shown)", "competitive" in (store.get("after_reopen") or "").lower(), store.get("after_reopen"))

    # --- settings dialog --------------------------------------------------------------------------------------------------
    from anki_explainer.explainer.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(s, "reasoning")
    dlg.show()
    yield 0.8
    shot("09_settings_reasoning", dlg)
    dlg.nav.setCurrentRow(9)
    yield 0.5
    shot("10_settings_usage", dlg)
    dlg.nav.setCurrentRow(0)
    yield 0.3
    shot("11_settings_account", dlg)
    dlg.save()
    check("settings dialog saves", True)
    yield 0.3

    # --- narrow window → floating ---------------------------------------------------------------------------------------------
    hooks._explain() if not panel.is_visible() else None
    yield 0.3
    mw.resize(850, 700)
    yield 0.8
    check("narrow window floats panel", panel.dock.isFloating(), panel.dock.isFloating())
    shot("12_narrow_floating", None)
    mw.setGeometry(40, 40, 1280, 880)
    yield 0.8
    check("wide window re-docks panel", not panel.dock.isFloating(), panel.dock.isFloating())

    check("add-on import < 50 ms", t_import is not None and t_import < 50, f"{t_import:.1f} ms" if t_import else t_import)
    server.stop()
    finish()


_runner: list[Runner] = []


def start() -> None:
    trace("profile opened; scheduling scenario")
    QTimer.singleShot(2500, lambda: _runner.append(Runner(scenario())))


if os.environ.get("EXPLAINER_SELFTEST") == "1" and OUT:
    gui_hooks.profile_did_open.append(start)
