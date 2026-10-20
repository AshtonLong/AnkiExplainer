import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "addon"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def settings():
    from explainer.store.settings import Settings

    return Settings({})


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    yield app


def raw_from_fixture(name, side, **overrides):
    from explainer.context.extractor import RawCard

    d = load_fixture(name)
    return RawCard(
        card_id=d["card_id"],
        note_id=d["note_id"],
        ord=d["ord"],
        side=side,
        notetype=d["notetype"],
        template=d["template"],
        deck_name=d["deck_name"],
        tags=d["tags"],
        q_html=d["q_html"],
        a_html=d["a_html"],
        fields=d["fields"],
        typed_answer=overrides.get("typed_answer", d.get("typed_answer") if side == "answer" else None),
        type_correct=overrides.get("type_correct", d.get("type_correct") if side == "answer" else None),
        audio_refs=d.get("audio_refs", []),
        media_dir=overrides.get("media_dir", FIXTURES),
    )
