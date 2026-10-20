"""Conversation state (spec §7.4).

One thread per card per (side, selection). A thread holds the system prompt,
the first user turn (card context + images), then alternating turns.
Assistant turns keep their ``reasoning_details`` and pass them back unmodified.
Images are sent only in the first user turn; once the thread grows past six
user turns they can be dropped to save tokens. The token estimate is capped at
60k; beyond that the oldest follow-up pairs are folded into a short local
summary (no extra request is made).
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

TOKEN_CAP = 60_000
IMAGE_TOKEN_ESTIMATE = 800
DROP_IMAGES_AFTER_TURNS = 6


def new_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: Any  # API content (str or list of parts)
    display: str = ""  # what the panel shows for user turns
    kind: str = "ask"  # initial | chip | ask | quiz_answer | hint | stronger_hint
    chip: str | None = None
    reasoning: str = ""
    reasoning_details: list[dict[str, Any]] | None = None
    status: str = "done"  # streaming | done | stopped | incomplete | error
    meta: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)


@dataclass
class Thread:
    card_id: int
    note_id: int
    side: str
    mode: str  # initial mode: concise | hint | explain_no_reveal | selection
    content_hash: str
    selection: str | None = None
    title: str = ""
    deck: str = ""
    model: str = ""
    profile: str = ""
    system: str = ""
    turns: list[Turn] = field(default_factory=list)
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    awaiting_quiz: bool = False
    card_kind: str = ""
    kind_label: str = ""
    target: str | None = None
    sent_summary: str = ""
    images_stripped: bool = False

    # -- building ---------------------------------------------------------------------
    def add_user(self, content: Any, display: str = "", kind: str = "ask", chip: str | None = None) -> Turn:
        t = Turn(role="user", content=content, display=display, kind=kind, chip=chip)
        self.turns.append(t)
        self.updated_at = time.time()
        return t

    def add_assistant(self, kind: str = "ask") -> Turn:
        t = Turn(role="assistant", content="", kind=kind, status="streaming")
        self.turns.append(t)
        self.updated_at = time.time()
        return t

    def drop_last_exchange(self) -> None:
        """Remove a trailing (user, assistant) pair, e.g. for Regenerate."""
        if self.turns and self.turns[-1].role == "assistant":
            self.turns.pop()
        if self.turns and self.turns[-1].role == "user":
            self.turns.pop()

    def last_assistant(self) -> Turn | None:
        for t in reversed(self.turns):
            if t.role == "assistant":
                return t
        return None

    @property
    def user_turns(self) -> int:
        return sum(1 for t in self.turns if t.role == "user")

    @property
    def has_first_answer(self) -> bool:
        return len(self.turns) >= 2 and self.turns[1].status in ("done", "stopped", "incomplete")

    # -- API messages ------------------------------------------------------------------------
    def api_messages(self, drop_old_images: bool = True, upto: int | None = None) -> list[dict[str, Any]]:
        """Messages for the next request. ``upto`` limits turns (exclusive)."""
        turns = [t for t in self.turns[:upto] if not (t.role == "assistant" and t.status == "streaming")]
        # Skip assistant turns that failed without any text, and their prompt.
        cleaned: list[Turn] = []
        for t in turns:
            if t.role == "assistant" and not t.content:
                if cleaned and cleaned[-1].role == "user" and len(cleaned) > 1:
                    cleaned.pop()
                continue
            cleaned.append(t)
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system}]
        user_count = sum(1 for t in cleaned if t.role == "user")
        drop_images = drop_old_images and user_count > DROP_IMAGES_AFTER_TURNS
        for i, t in enumerate(cleaned):
            content = t.content
            if i == 0 and isinstance(content, list):
                content = _strip_images(content) if drop_images else copy.deepcopy(content)
            msg: dict[str, Any] = {"role": t.role, "content": content}
            if t.role == "assistant" and t.reasoning_details:
                msg["reasoning_details"] = t.reasoning_details
            messages.append(msg)
        return _cap_tokens(messages)

    # -- persistence -----------------------------------------------------------------------------
    def to_dict(self, strip_images: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if strip_images:
            for t in d["turns"]:
                if isinstance(t["content"], list):
                    t["content"] = _strip_images(t["content"])
            d["images_stripped"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Thread":
        d = dict(d)
        turns = [Turn(**t) for t in d.pop("turns", [])]
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        th = cls(**{k: v for k, v in d.items() if k in known})
        th.turns = turns
        return th

    def view(self) -> list[dict[str, Any]]:
        """Turns as shown in the panel (the first user turn is implicit)."""
        out = []
        for i, t in enumerate(self.turns):
            # The first turn (card context) is implicit, except when the student
            # started the thread with their own words (selection or question).
            if i == 0 and t.role == "user" and self.mode not in ("selection", "question"):
                continue
            out.append(
                {
                    "id": t.id,
                    "role": t.role,
                    "text": t.content if t.role == "assistant" else (t.display or _text_of(t.content)),
                    "kind": t.kind,
                    "chip": t.chip,
                    "reasoning": t.reasoning,
                    "status": t.status,
                    "meta": t.meta,
                }
            )
        return out


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _strip_images(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    removed = 0
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            removed += 1
            continue
        out.append(copy.deepcopy(part))
    if removed:
        note = f"\n\n[{removed} image(s) of this card were shown earlier in the conversation.]"
        if out and out[0].get("type") == "text":
            out[0]["text"] = out[0]["text"] + note
        else:
            out.insert(0, {"type": "text", "text": note.strip()})
    return out


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c) // 4 + 4
        elif isinstance(c, list):
            for p in c:
                if p.get("type") == "image_url":
                    total += IMAGE_TOKEN_ESTIMATE
                else:
                    total += len(p.get("text", "")) // 4
    return total


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    for sep in (". ", "! ", "? "):
        idx = text.find(sep)
        if 0 < idx < limit:
            return text[: idx + 1]
    return text[:limit] + ("…" if len(text) > limit else "")


def _cap_tokens(messages: list[dict[str, Any]], cap: int = TOKEN_CAP) -> list[dict[str, Any]]:
    if estimate_tokens(messages) <= cap:
        return messages
    # messages: system, first user, first assistant, then follow-up pairs.
    head = messages[:3]
    rest = messages[3:]
    summary_bits: list[str] = []
    while estimate_tokens(head + rest) > cap and len(rest) > 3:
        q = rest.pop(0)
        a = rest.pop(0) if rest and rest[0]["role"] == "assistant" else None
        q_text = _text_of(q.get("content"))[:120]
        a_text = _first_sentence(_text_of(a.get("content"))) if a else ""
        summary_bits.append(f"- Q: {q_text} → A: {a_text}")
    if summary_bits and rest:
        first = copy.deepcopy(rest[0])
        prefix = "Earlier follow-ups (summarized):\n" + "\n".join(summary_bits) + "\n\n"
        if isinstance(first["content"], str):
            first["content"] = prefix + first["content"]
        rest[0] = first
    return head + rest
