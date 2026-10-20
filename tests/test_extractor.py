from explainer.context.extractor import build_context, load_images, referenced_fields, split_answer
from explainer.store.settings import Settings
from conftest import raw_from_fixture


def test_basic_answer_side(settings):
    ctx = build_context(raw_from_fixture("basic", "answer"), settings)
    assert ctx.card_kind == "basic"
    assert ctx.question_text == "What does **ATP** stand for?"
    assert ctx.answer_text == "Adenosine *triphosphate*\n[image 1]"
    assert ctx.audio_refs == ["atp.mp3"]
    assert ctx.deck_name == "Biochem::Enzymes"
    assert ctx.tags == []  # include_tags is off by default
    assert [s.src for s in ctx.image_sources] == ["heart.png"]
    assert ctx.title == "What does ATP stand for?"


def test_basic_question_side_never_includes_answer(settings):
    ctx = build_context(raw_from_fixture("basic", "question"), settings)
    assert ctx.answer_text is None
    assert ctx.image_sources == []  # the image is on the back only
    assert any("Adenosine" in a for a in ctx.hidden_answers)


def test_reversed_card(settings):
    ctx = build_context(raw_from_fixture("reversed_rev", "answer"), settings)
    assert ctx.question_text == "dog" and ctx.answer_text == "perro"


def test_cloze_sides(settings):
    q = build_context(raw_from_fixture("cloze_c1", "question"), settings)
    assert q.card_kind == "cloze"
    assert "⟦BLANK: type of inhibitor⟧" in q.question_text
    assert q.answer_text is None and q.target is None
    assert q.hidden_answers == ["competitive", "V max"]
    a = build_context(raw_from_fixture("cloze_c1", "answer"), settings)
    assert a.target == "**competitive** / V_{max}"
    assert "⟦TARGET: **competitive**⟧" in a.answer_text
    assert "⟦BLANK" in a.question_text


def test_type_in_includes_typed_and_expected(settings):
    ctx = build_context(raw_from_fixture("type_in", "answer"), settings)
    assert ctx.card_kind == "type_in"
    assert ctx.typed_answer == "metoprolol"
    assert ctx.target == "propranolol"
    assert ctx.answer_text == "propranolol"
    assert "[[type" not in ctx.question_text
    off = Settings({"context": {"include_typed_answer": False}})
    assert build_context(raw_from_fixture("type_in", "answer"), off).typed_answer is None


def test_type_in_question_side_hidden_answer(settings):
    ctx = build_context(raw_from_fixture("type_in", "question"), settings)
    assert ctx.typed_answer is None
    assert "propranolol" in ctx.hidden_answers


def test_math_and_code_flags(settings):
    ctx = build_context(raw_from_fixture("math_code", "answer"), settings)
    assert ctx.has_math and ctx.has_code
    assert "| a | b |" in ctx.question_text
    assert "- use factoring" in ctx.question_text


def test_image_occlusion_context(settings):
    q = build_context(raw_from_fixture("io_c1", "question"), settings)
    assert q.card_kind == "image_occlusion"
    assert q.question_text.startswith("Heart anatomy")
    assert "Ventricle" in q.question_text
    assert q.answer_text is None
    assert q.target.startswith("the outlined TARGET region in image 1")
    assert q.image_sources[0].role == "io_target"
    assert sum(s.active for s in q.image_sources[0].shapes) == 2
    a = build_context(raw_from_fixture("io_c1", "answer"), settings)
    assert a.answer_text == "Blue = right atrium"


def test_images_load_and_cap(settings, qapp):
    ctx = build_context(raw_from_fixture("io_c1", "answer"), settings)
    failed = load_images(ctx, allow_remote=False)
    assert failed == []
    assert len(ctx.images) == 1 and ctx.images[0].role == "io_target"
    no_img = Settings({"context": {"send_images": False}})
    ctx2 = build_context(raw_from_fixture("basic", "answer"), no_img)
    assert ctx2.image_sources == [] and ctx2.skipped_images == 1
    assert ctx2.images_not_sent_reason


def test_max_images_cap(qapp):
    s = Settings({"context": {"max_images": 1}})
    raw = raw_from_fixture("basic", "answer")
    raw.a_html += '<img src="heart.png?2"><img src="other.png">'
    ctx = build_context(raw, s)
    assert len(ctx.image_sources) == 1 and ctx.skipped_images == 2


def test_content_hash_stable_and_sensitive(settings):
    a = build_context(raw_from_fixture("basic", "answer"), settings).content_hash
    b = build_context(raw_from_fixture("basic", "answer"), settings).content_hash
    assert a == b
    raw = raw_from_fixture("basic", "answer")
    raw.a_html = raw.a_html.replace("Adenosine", "Adenosine (edited)")
    assert build_context(raw, settings).content_hash != a
    assert build_context(raw_from_fixture("basic", "answer"), settings, selection="ATP").content_hash != a
    assert build_context(raw_from_fixture("basic", "question"), settings).content_hash != a


def test_hidden_fields_optional():
    s = Settings({"context": {"include_hidden_fields": True}})
    raw = raw_from_fixture("basic", "answer")
    raw.fields["Notes"] = "secret mnemonic"
    ctx = build_context(raw, s)
    assert ctx.extra_fields == {"Notes": "secret mnemonic"}


def test_tags_optional():
    s = Settings({"context": {"include_tags": True, "include_deck": False}})
    ctx = build_context(raw_from_fixture("basic", "answer"), s)
    assert ctx.tags == ["biochem", "energy"] and ctx.deck_name == ""


def test_split_answer_and_template_refs():
    assert split_answer("Q<hr id=answer>A", "Q") == ("Q", "A")
    assert split_answer("Q A", "Q") == ("Q", " A")
    assert referenced_fields(
        {"qfmt": "{{Front}}{{#Hint}}{{hint:Hint}}{{/Hint}}", "afmt": "{{FrontSide}}{{type:Back}}"}
    ) == {"Front", "Hint", "FrontSide", "Back"}
