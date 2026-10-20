import threading
import time

import pytest

from explainer.llm import openrouter as orc
from fake_openrouter import FakeOpenRouter, Reply, delta, sse, text_reply, usage_chunk


@pytest.fixture
def server():
    s = FakeOpenRouter().start()
    yield s
    s.stop()


def client(server, **kw):
    return orc.OpenRouterClient("sk-or-test-key-123456", base_url=server.base_url, sleep_backoff=(0.05, 0.05, 0.05), **kw)


def body(**kw):
    b = orc.build_body("~deepseek/deepseek-flash-latest", [{"role": "user", "content": "hi"}],
                       stream=True, temperature=0.3, max_tokens=1200, effort="minimal",
                       budget_tokens=None, exclude_reasoning=False)
    b.update(kw)
    return b


def collect():
    events = []
    return events, lambda kind, value: events.append((kind, value))


def test_stream_success_headers_and_body(server):
    server.queue(text_reply("The core idea is simple.", reasoning="hmm"))
    events, sink = collect()
    res = client(server).chat(body(), sink, orc.CancelToken())
    assert res.content == "The core idea is simple."
    assert res.reasoning == "hmm"
    assert res.usage["cost"] == 0.0004
    assert res.model == "deepseek/deepseek-v4.1-flash-1017"
    assert [k for k, _ in events][0] == "reasoning"
    req = server.chat_requests[0]
    assert req["headers"]["Authorization"] == "Bearer sk-or-test-key-123456"
    assert req["headers"]["X-Title"] == "Anki Explainer"
    assert req["headers"]["HTTP-Referer"].startswith("https://github.com/")
    sent = req["body"]
    assert sent["model"] == "~deepseek/deepseek-flash-latest"
    assert sent["reasoning"] == {"effort": "minimal"}
    assert sent["usage"] == {"include": True}
    for forbidden in ("provider", "models", "route", "transforms"):
        assert forbidden not in sent


@pytest.mark.parametrize("status,kind,action", [
    (401, "auth", "settings"),
    (402, "credits", "credits"),
    (403, "moderation", "retry"),
    (404, "routing", "provider_settings"),
    (503, "routing", "provider_settings"),
    (408, "timeout", "retry"),
    (500, "server", "retry"),
    (502, "server", "retry"),
])
def test_http_errors_mapped(server, status, kind, action):
    server.queue(Reply(status=status, body={"error": {"code": status, "message": "nope"}}))
    with pytest.raises(orc.ApiError) as ei:
        client(server).chat(body(), lambda *_: None, orc.CancelToken())
    assert ei.value.kind == kind and ei.value.action == action


def test_429_retried_with_backoff_then_success(server):
    server.queue(Reply(status=429, body={"error": {"code": 429, "message": "slow down"}}),
                 Reply(status=429, body={"error": {"code": 429, "message": "slow down"}}),
                 text_reply("ok"))
    events, sink = collect()
    res = client(server).chat(body(), sink, orc.CancelToken())
    assert res.content == "ok"
    retries = [v for k, v in events if k == "retry"]
    assert [r["attempt"] for r in retries] == [1, 2]
    assert len(server.chat_requests) == 3


def test_429_gives_up_after_three_retries(server):
    for _ in range(4):
        server.queue(Reply(status=429, body={"error": {"code": 429, "message": "x"}}))
    with pytest.raises(orc.ApiError) as ei:
        client(server).chat(body(), lambda *_: None, orc.CancelToken())
    assert ei.value.kind == "rate_limit"
    assert len(server.chat_requests) == 4


def test_mid_stream_error_partial(server):
    server.queue(Reply(chunks=sse(delta("Partial "), {"error": {"code": 502, "message": "boom"},
                                                       "choices": [{"delta": {"content": ""}, "finish_reason": "error"}]}, done=False)))
    events, sink = collect()
    with pytest.raises(orc.ApiError) as ei:
        client(server).chat(body(), sink, orc.CancelToken())
    assert ei.value.partial and ei.value.kind == "server"
    assert ("content", "Partial ") in events


def test_cancel_mid_stream(server):
    server.queue(text_reply(" ".join(["word"] * 200), words_per_chunk=1, chunk_delay=0.02))
    token = orc.CancelToken()
    events, sink = collect()

    def cancel_soon():
        time.sleep(0.2)
        token.cancel("stop")

    threading.Thread(target=cancel_soon).start()
    started = time.monotonic()
    with pytest.raises(orc.Cancelled):
        client(server).chat(body(), sink, token)
    assert time.monotonic() - started < 2.0
    assert 0 < len([e for e in events if e[0] == "content"]) < 200


def test_idle_timeout(server):
    server.queue(Reply(chunks=sse(delta("start "), comment=False, done=False) + ["data: [DONE]\n\n"], chunk_delay=0.6))
    with pytest.raises(orc.ApiError) as ei:
        client(server, first_byte_timeout=0.3, idle_timeout=0.3).chat(body(), lambda *_: None, orc.CancelToken())
    assert ei.value.kind == "timeout"


def test_offline(monkeypatch):
    c = orc.OpenRouterClient("sk-or-x", base_url="http://127.0.0.1:9/api/v1")
    with pytest.raises(orc.ApiError) as ei:
        c.chat(body(), lambda *_: None, orc.CancelToken())
    assert ei.value.kind in ("offline", "timeout")
    assert ei.value.message in ("You appear to be offline.", "The model took too long to respond.")


def test_non_streaming(server):
    server.queue(Reply(body={"id": "g", "model": "m", "choices": [{"message": {"content": "whole", "reasoning": "r"}}],
                             "usage": {"cost": 0.01}}))
    events, sink = collect()
    res = client(server).chat(body(stream=False), sink, orc.CancelToken())
    assert res.content == "whole" and res.usage["cost"] == 0.01
    assert ("content", "whole") in events


def test_key_info(server):
    server.queue(Reply(body={"data": {"label": "sk-or-v1-abc...", "limit": 5, "limit_remaining": 4.5, "usage": 0.5}}))
    info = client(server).key_info()
    assert info["limit_remaining"] == 4.5
    text = orc.describe_key_info(info)
    assert "$4.50 of $5.00" in text
    assert server.requests[0]["path"] == "/api/v1/key"


def test_build_body_budget_and_exclude():
    b = orc.build_body("m", [], stream=True, temperature=0.3, max_tokens=1200, effort="low",
                       budget_tokens=2000, exclude_reasoning=True)
    assert b["reasoning"] == {"max_tokens": 2000, "exclude": True}
    assert "effort" not in b["reasoning"]
    assert b["max_tokens"] > 2000
    off = orc.build_body("m", [], stream=True, temperature=0.3, max_tokens=300, effort="none",
                         budget_tokens=2000, exclude_reasoning=True)
    assert off["reasoning"] == {"effort": "none"}
    no_r = orc.build_body("m", [], stream=True, temperature=0.3, max_tokens=300, effort="low",
                          budget_tokens=None, exclude_reasoning=False, supports_reasoning=False)
    assert "reasoning" not in no_r
