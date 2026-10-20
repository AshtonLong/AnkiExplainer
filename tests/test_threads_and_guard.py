from explainer.llm import leak_guard
from explainer.llm.threads import Thread, estimate_tokens


def make_thread(images=1):
    th = Thread(card_id=1, note_id=1, side="answer", mode="concise", content_hash="h", system="SYS")
    content = [{"type": "text", "text": "<card>…</card>\n\nExplain."}]
    content += [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}] * images
    th.add_user(content, kind="initial")
    a = th.add_assistant("initial")
    a.content, a.status = "First answer.", "done"
    a.reasoning_details = [{"type": "reasoning.text", "text": "r", "index": 0}]
    return th


def test_api_messages_structure_and_reasoning_passback():
    th = make_thread()
    th.add_user("Give an example.", display="Example", kind="chip", chip="example")
    th.add_assistant("chip")  # in flight: excluded
    msgs = th.api_messages()
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[0]["content"] == "SYS"
    assert msgs[1]["content"][1]["type"] == "image_url"
    assert msgs[2]["reasoning_details"] == [{"type": "reasoning.text", "text": "r", "index": 0}]


def test_images_dropped_after_six_turns():
    th = make_thread()
    for i in range(6):
        th.add_user(f"q{i}")
        a = th.add_assistant()
        a.content, a.status = f"a{i}", "done"
    msgs = th.api_messages(drop_old_images=True)
    assert all(p["type"] == "text" for p in msgs[1]["content"])
    assert "shown earlier" in msgs[1]["content"][0]["text"]
    kept = th.api_messages(drop_old_images=False)
    assert kept[1]["content"][1]["type"] == "image_url"


def test_token_cap_summarizes_oldest_followups():
    th = make_thread(images=0)
    for i in range(30):
        th.add_user(f"question {i} " + "x" * 8000)
        a = th.add_assistant()
        a.content, a.status = f"Answer {i}. " + "y" * 8000, "done"
    msgs = th.api_messages()
    assert estimate_tokens(msgs) <= 60_000
    assert msgs[1]["role"] == "user" and msgs[2]["content"] == "First answer."
    first_follow = msgs[3]["content"]
    assert first_follow.startswith("Earlier follow-ups (summarized):")
    assert "Answer 0." in first_follow
    assert msgs[-1]["content"].startswith("Answer 29.")


def test_failed_turns_skipped_and_regenerate():
    th = make_thread()
    th.add_user("why?")
    failed = th.add_assistant()
    failed.status, failed.content = "error", ""
    msgs = th.api_messages()
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    th.drop_last_exchange()
    assert len(th.turns) == 2


def test_persistence_strips_images_and_view():
    th = make_thread()
    d = th.to_dict()
    assert all(p["type"] == "text" for p in d["turns"][0]["content"])
    back = Thread.from_dict(d)
    assert back.id == th.id and back.turns[1].content == "First answer."
    view = th.view()
    assert [v["role"] for v in view] == ["assistant"]


def test_leak_guard_detects_answers():
    assert leak_guard.leaks("It's **Propranolol**, a beta blocker", ["propranolol"])
    assert leak_guard.leaks("think of compétitive binding", ["competitive"])
    assert not leak_guard.leaks("Think about which enzymes compete for a site", ["competitive"])
    assert not leak_guard.leaks("A word that starts with p", ["p"])  # single letters ignored
    assert leak_guard.leaks("The value is 42.", ["42"])
    assert leak_guard.leaks("Remember: dog!", ["perro / dog"])
    assert leak_guard.leaks("答えは犬です", ["犬"])
    assert not leak_guard.leaks("Consider the category", [])


def test_leak_guard_multiline_answer():
    answer = "Adenosine triphosphate\nThe main energy currency"
    assert leak_guard.leaks("adenosine triphosphate!", [answer])
    assert not leak_guard.leaks("It carries energy in cells", [answer])
