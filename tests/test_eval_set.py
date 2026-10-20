"""The prompt eval set (§9.4) must stay valid and exercise every card kind.
These checks run offline; ``scripts/run_eval.py`` does the scored run."""

import collections
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "eval"))

import evalset  # noqa: E402
from explainer.context.extractor import build_context  # noqa: E402
from explainer.llm import leak_guard, prompts  # noqa: E402
from explainer.store.settings import Settings  # noqa: E402

CARDS = evalset.load()


def test_set_size_and_coverage():
    assert 38 <= len(CARDS) <= 45
    assert len({c.id for c in CARDS}) == len(CARDS)
    domains = collections.Counter(c.domain for c in CARDS)
    for d in ("medicine", "languages", "law", "programming"):
        assert domains[d] >= 4, d
    assert sum(domains[d] for d in ("math", "physics", "chemistry", "biology")) >= 6
    kinds = collections.Counter(c.type for c in CARDS)
    for k in ("basic", "cloze", "type_in", "image_occlusion", "reversed"):
        assert kinds[k] >= 1, k
    assert sum(c.deliberately_wrong for c in CARDS) >= 4


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c.id)
def test_card_builds_and_never_leaks_answer_on_question_side(card, qapp):
    settings = Settings({})
    a = build_context(card.raw("answer"), settings)
    q = build_context(card.raw("question"), settings)
    expected_kind = {"reversed": "basic"}.get(card.type, card.type)
    assert a.card_kind == expected_kind and q.card_kind == expected_kind
    assert q.answer_text is None
    if card.type in ("cloze", "type_in"):
        assert a.target
    prompt = prompts.render_card_block(q)
    source = (card.data.get("front", "") + card.data.get("text", "")).lower()
    for ans in card.answers:
        if leak_guard.normalize(ans) in leak_guard.normalize(source):
            continue  # the answer is part of the question itself
        assert not leak_guard.leaks(prompt, [ans]), (card.id, ans)


def test_dry_run_script():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, os.path.join(root, "scripts", "run_eval.py"), "--dry-run"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "40 cards OK" in out.stdout
