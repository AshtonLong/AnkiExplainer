import json
import logging
import os
import time

import pytest

from explainer.store.cache import Cache, CacheKey, month_key, selection_hash
from explainer.store.secrets import SecretStore, mask
from explainer.store.settings import Settings
from explainer.util import log as logmod


def key(**kw):
    base = dict(content_hash="h", side="answer", mode="concise", selection_hash="",
                model_requested="~deepseek/deepseek-flash-latest", prompt_version="1:abc")
    base.update(kw)
    return CacheKey(**base)


@pytest.fixture
def cache(tmp_path):
    c = Cache(str(tmp_path / "cache.sqlite3"))
    yield c
    c.close()


def test_cache_roundtrip_and_keying(cache):
    assert cache.lookup(key()) is None
    cache.store(key(), 1, 2, "text", "why", {"cost": 0.001}, "deepseek/v4.1", [{"type": "reasoning.text"}])
    hit = cache.lookup(key())
    assert hit["text"] == "text" and hit["usage"]["cost"] == 0.001
    assert hit["model_resolved"] == "deepseek/v4.1"
    assert hit["reasoning_details"] == [{"type": "reasoning.text"}]
    # Every key component matters.
    for change in (dict(content_hash="h2"), dict(side="question"), dict(mode="hint"),
                   dict(selection_hash="s"), dict(model_requested="other"), dict(prompt_version="2:x")):
        assert cache.lookup(key(**change)) is None
    cache.store(key(), 1, 2, "updated")
    assert cache.lookup(key())["text"] == "updated"


def test_selection_hash_normalizes():
    assert selection_hash(None) == ""
    assert selection_hash("  Km  value ") == selection_hash("km value")


def test_threads_and_history(cache):
    now = time.time()
    t = {"id": "t1", "card_id": 5, "note_id": 6, "created_at": now, "updated_at": now, "title": "Q", "deck": "D",
         "model": "m", "side": "answer", "mode": "concise", "profile": "User 1", "content_hash": "h", "selection": None}
    cache.save_thread(t)
    assert cache.get_thread("t1")["title"] == "Q"
    assert cache.find_thread(5, "answer", "", "h", "User 1")["id"] == "t1"
    assert cache.find_thread(5, "answer", "", "h", "Other") is None
    hist = cache.history("User 1")
    assert [h["id"] for h in hist] == ["t1"]
    old = dict(t, id="t0", updated_at=now - 10 * 86400)
    cache.save_thread(old)
    assert [h["id"] for h in cache.history("User 1")] == ["t1"]


def test_usage_and_metrics(cache):
    cache.record_usage({"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.001})
    cache.record_usage({"prompt_tokens": 50, "completion_tokens": 10, "cost": 0.002})
    u = cache.month_usage()
    assert u["requests"] == 2 and u["prompt_tokens"] == 150 and u["cost_usd"] == pytest.approx(0.003)
    e1 = cache.record_event(1, "concise", 0.001)
    e2 = cache.record_event(2, "concise", 0.003)
    h = cache.record_event(3, "hint", 0.0001)
    cache.mark_followed_up(e1)
    cache.record_grade(e1, 3, 12.0)
    cache.record_grade(e2, 1, 30.0)
    cache.record_grade(h, 4, 5.0)
    m = cache.metrics()
    assert m["explanations"] == 2 and m["depth_rate"] == 0.5
    assert m["median_seconds_to_grade"] == 21.0
    assert m["hint_resolution"] == 1.0 and m["median_cost"] == pytest.approx(0.002)
    assert month_key().count("-") == 1


def test_eviction_retention_and_size(cache):
    cache.store(key(content_hash="old"), 1, 1, "x")
    cache.db.execute("UPDATE explanations SET accessed_at = ?", (time.time() - 100 * 86400,))
    cache.db.commit()
    cache.store(key(content_hash="new"), 1, 1, "y" * 1000)
    assert cache.evict(retention_days=90) == 1
    assert cache.lookup(key(content_hash="old")) is None
    for i in range(10):
        cache.store(key(content_hash=f"big{i}"), 1, 1, "z" * 2000)
    cache.evict(retention_days=90, max_bytes=5000)
    remaining = cache.db.execute("SELECT COUNT(*) FROM explanations").fetchone()[0]
    assert remaining < 11
    assert cache.size_bytes() > 0
    cache.clear()
    assert cache.db.execute("SELECT COUNT(*) FROM explanations").fetchone()[0] == 0


def test_settings_defaults_and_validation():
    s = Settings({})
    assert s.model == "~deepseek/deepseek-flash-latest"
    assert s.effort("concise") == "minimal" and s.effort("hint") == "none"
    assert s.max_tokens("deep") == 4000
    bad = Settings({
        "reasoning": {"concise": {"effort": "ultra"}, "display": "loud", "budget_tokens": "x"},
        "output": {"temperature": 7, "max_tokens": {"hint": "lots"}},
        "behavior": {"font_size": 99, "question_side": "spoil"},
        "chips": ["deeper", "nope", "deeper", "quiz"],
        "shortcuts": {"chips": ["Alt+9"]},
        "unknown_key": 1,
    })
    assert bad.effort("concise") == "minimal" and bad.reasoning_display == "hidden"
    assert bad.budget_tokens is None and bad.temperature == 0.3
    assert bad.max_tokens("hint") == 300
    assert bad.behavior("font_size") == 15 and bad.question_side == "hint"
    assert bad.chips == ["deeper", "quiz"]
    assert bad.get("shortcuts.chips") == ["Alt+9", "Alt+2", "Alt+3", "Alt+4", "Alt+5", "Alt+6"]
    assert "unknown_key" not in bad.data
    ok = Settings({"output": {"temperature": 0}, "usage": {"monthly_soft_cap_usd": 2}})
    assert ok.temperature == 0.0 and ok.soft_cap == 2


def test_settings_save_writes_merged():
    written = {}
    s = Settings({}, writer=written.update)
    s.set("behavior.font_size", 18)
    s.save()
    assert written["behavior"]["font_size"] == 18
    assert "api_key" not in json.dumps(written)


def test_secret_store_file_fallback(tmp_path):
    store = SecretStore(str(tmp_path), use_keyring=False)
    assert store.get() is None
    store.set("  sk-or-v1-abcdef1234  ")
    assert SecretStore(str(tmp_path), use_keyring=False).get() == "sk-or-v1-abcdef1234"
    assert json.load(open(tmp_path / "secret.json"))["openrouter_api_key"] == "sk-or-v1-abcdef1234"
    store.remove()
    assert not os.path.exists(tmp_path / "secret.json")
    assert SecretStore(str(tmp_path), use_keyring=False).get() is None
    assert mask("sk-or-v1-abcdef1234") == "sk-or-…1234"


def test_log_redacts_keys(tmp_path):
    logmod._configured = False
    logger = logging.getLogger("anki_explainer")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logmod.configure(str(tmp_path))
    logmod.get_logger("t").error("failed with key sk-or-v1-0123456789abcdef and Bearer abcdefghijklmnop")
    for h in logger.handlers:
        h.flush()
    text = open(tmp_path / "explainer.log", encoding="utf-8").read()
    assert "0123456789" not in text and "abcdefghijklmnop" not in text
    assert "[REDACTED]" in text


def test_find_thread_matches_mode_and_model(cache):
    now = time.time()
    base = {"card_id": 5, "note_id": 6, "created_at": now, "title": "Q", "deck": "D", "side": "answer",
            "profile": "P", "content_hash": "h", "selection": None}
    cache.save_thread(dict(base, id="a", updated_at=now, model="m1", mode="concise"))
    cache.save_thread(dict(base, id="b", updated_at=now + 1, model="m2", mode="concise"))
    cache.save_thread(dict(base, id="c", updated_at=now + 2, model="m1", mode="question"))
    assert cache.find_thread(5, "answer", "", "h", "P", mode="concise", model="m1")["id"] == "a"


def test_log_redacts_exception_text(tmp_path):
    logmod._configured = False
    logger = logging.getLogger("anki_explainer")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logmod.configure(str(tmp_path))
    try:
        raise ValueError("Invalid header value b'Bearer sk-or-v1-secretsecret123\n'")
    except ValueError:
        logmod.get_logger("t").exception("request failed")
    for h in logger.handlers:
        h.flush()
    text = open(tmp_path / "explainer.log", encoding="utf-8").read()
    assert "secretsecret" not in text and "ValueError" in text
