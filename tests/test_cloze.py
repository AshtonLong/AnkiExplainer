from explainer.context.cloze import ClozeState
from explainer.context.html_to_text import html_to_text
from conftest import load_fixture


def test_question_side_blank_with_hint_and_hidden_answers():
    d = load_fixture("cloze_c1")
    state = ClozeState("question")
    text = html_to_text(d["q_html"], cloze=state).text
    assert text == "A ⟦BLANK: type of inhibitor⟧ inhibitor raises apparent K_m but leaves ⟦BLANK⟧ unchanged."
    assert state.hidden_answers == ["competitive", "V max"]
    assert state.targets == []


def test_answer_side_targets_multiple_same_ordinal_nested_formatting():
    d = load_fixture("cloze_c1")
    state = ClozeState("answer")
    text = html_to_text(d["a_html"], cloze=state).text
    assert "⟦TARGET: **competitive**⟧" in text
    assert "⟦TARGET: V_{max}⟧" in text
    assert state.target == "**competitive** / V_{max}"
    assert "Lineweaver" in text


def test_other_cloze_stays_revealed():
    d = load_fixture("cloze_c2")
    state = ClozeState("question")
    text = html_to_text(d["q_html"], cloze=state).text
    assert "**competitive**" in text and "⟦BLANK⟧" in text
    assert state.hidden_answers == ["K m"]


def test_nested_cloze_answer_marks_outer_only():
    html = '<span class="cloze" data-ordinal="1">outer <span class="cloze" data-ordinal="1">inner</span></span>'
    state = ClozeState("answer")
    assert html_to_text(html, cloze=state).text == "⟦TARGET: outer inner⟧"
    assert state.target == "outer inner"


def test_duplicate_targets_deduplicated():
    html = '<span class="cloze">Km</span> and <span class="cloze">Km</span>'
    state = ClozeState("answer")
    html_to_text(html, cloze=state)
    assert state.target == "Km"
