import json

from explainer.llm.openrouter import DONE, SSEParser, StreamAccumulator


def test_comments_ignored_and_done():
    p = SSEParser()
    events = p.feed(': OPENROUTER PROCESSING\n\ndata: {"a":1}\n\ndata: [DONE]\n\n')
    assert events == ['{"a":1}', DONE]
    assert p.done


def test_split_lines_across_chunks():
    p = SSEParser()
    out = []
    for piece in ['da', 'ta: {"x"', ':1}\n', '\nda', 'ta: [DO', 'NE]\n\n']:
        out += p.feed(piece)
    assert out == ['{"x":1}', DONE]


def test_crlf_and_cr_line_endings_split_mid_crlf():
    p = SSEParser()
    out = p.feed('data: {"x":1}\r')
    out += p.feed('\n\r\ndata: {"y":2}\r\r')
    # A trailing "\r" may be the first half of "\r\n", so dispatch waits for more input.
    out += p.flush()
    assert out == ['{"x":1}', '{"y":2}']


def test_multibyte_utf8_split():
    p = SSEParser()
    raw = 'data: {"t":"⟦é⟧"}\n\n'.encode("utf-8")
    out = []
    for i in range(len(raw)):
        out += p.feed(raw[i : i + 1])
    assert json.loads(out[0])["t"] == "⟦é⟧"


def test_multiline_data_and_flush():
    p = SSEParser()
    assert p.feed("data: a\ndata: b\n") == []
    assert p.flush() == ["a\nb"]


def test_accumulator_content_reasoning_usage_model():
    acc = StreamAccumulator()
    acc.apply({"model": "m1", "choices": [{"delta": {"content": "", "reasoning": "think"}}]})
    acc.apply({"choices": [{"delta": {"content": "Hello "}}]})
    acc.apply({"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}],
               "usage": {"cost": 0.001, "prompt_tokens": 5, "completion_tokens": 2}})
    r = acc.result
    assert r.content == "Hello world" and r.reasoning == "think"
    assert r.model == "m1" and r.usage["cost"] == 0.001 and r.finish_reason == "stop"


def test_reasoning_details_merged_by_index():
    acc = StreamAccumulator()
    acc.apply({"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "ab", "index": 0, "format": "x"}]}}]})
    acc.apply({"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "cd", "index": 0, "signature": "sig"}]}}]})
    acc.apply({"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.encrypted", "data": "zz", "index": 1}]}}]})
    assert acc.result.reasoning_details == [
        {"type": "reasoning.text", "text": "abcd", "index": 0, "format": "x", "signature": "sig"},
        {"type": "reasoning.encrypted", "data": "zz", "index": 1},
    ]
    assert acc.result.reasoning == "abcd"


def test_mid_stream_error_keeps_partial():
    acc = StreamAccumulator()
    acc.apply({"choices": [{"delta": {"content": "partial"}}]})
    acc.apply({"error": {"code": 502, "message": "upstream died", "metadata": {"error_type": "server"}},
               "choices": [{"delta": {"content": ""}, "finish_reason": "error"}]})
    assert acc.result.content == "partial"
    assert acc.result.error.kind == "server" and acc.result.error.partial
