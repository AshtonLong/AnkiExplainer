"""Card → ``CardContext`` (spec §5).

Extraction runs in two stages so the main thread never blocks on I/O:

1. ``gather_raw()`` (main thread, a few ms): reads the card, note, notetype,
   rendered HTML and typed answer from Anki into a plain ``RawCard``.
2. ``build_context()`` (pure, fast): turns the HTML into structured text,
   marks cloze/IO targets, collects image references and computes
   ``content_hash`` from cheap identities (no image decoding), so the cache can
   be checked before any work is done.
3. ``load_images()`` (background thread): decodes, annotates, resizes and
   encodes the images that will actually be sent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from . import image_occlusion as io
from . import images as imgs
from .cloze import ClozeState, active_ordinal
from .html_to_text import ImageRef, html_to_text

Side = Literal["question", "answer"]
CardKind = Literal["basic", "cloze", "image_occlusion", "type_in", "unknown"]

KIND_LABELS = {
    "basic": "Basic",
    "cloze": "Cloze",
    "image_occlusion": "Image Occlusion",
    "type_in": "Type-in",
    "unknown": "Card",
}

_HR_ANSWER_RE = re.compile(r"<hr[^>]*\bid\s*=\s*[\"']?answer[\"']?[^>]*>", re.I)
_TYPE_RE = re.compile(r"\[\[type:(.+?)\]\]")
_PLAY_RE = re.compile(r"\[anki:play:[qa]:\d+\]")
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
_TTS_RE = re.compile(r"\[anki:tts[^\]]*\](.*?)\[/anki:tts\]", re.S)
_TEMPLATE_REF_RE = re.compile(r"\{\{\s*[#^/]?\s*([^}]+?)\s*\}\}")
_IMG_SRC_RE = re.compile(r"<img[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)


@dataclass
class RawCard:
    card_id: int
    note_id: int
    ord: int
    side: Side
    notetype: dict[str, Any]
    template: dict[str, str]
    deck_name: str
    tags: list[str]
    q_html: str
    a_html: str
    fields: dict[str, str]
    typed_answer: str | None
    type_correct: str | None
    audio_refs: list[str]
    media_dir: str
    lapses: int = 0


@dataclass
class ImageSource:
    src: str
    role: str
    alt: str = ""
    # Image Occlusion annotation input (only for role == "io_target").
    shapes: list[io.Shape] | None = None
    mask_src: str | None = None


@dataclass
class CardContext:
    card_id: int
    note_id: int
    side: Side
    card_kind: CardKind
    notetype_name: str
    deck_name: str
    tags: list[str]
    question_text: str
    answer_text: str | None
    target: str | None
    typed_answer: str | None
    extra_fields: dict[str, str]
    images: list[imgs.ImagePart]
    audio_refs: list[str]
    has_math: bool
    has_code: bool
    selection: str | None
    content_hash: str
    # --- not sent to the model ---
    image_sources: list[ImageSource] = field(default_factory=list)
    skipped_images: int = 0
    images_not_sent_reason: str | None = None
    hidden_answers: list[str] = field(default_factory=list)
    media_dir: str = ""
    title: str = ""
    io_note: str | None = None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.card_kind, "Card")

    def sent_summary(self) -> str:
        """Short description of what kind of content is sent (context strip)."""
        parts = ["text"]
        n = len(self.images) or len(self.image_sources)
        if n:
            parts.append(f"{n} image{'s' if n != 1 else ''}")
        if self.typed_answer:
            parts.append("your typed answer")
        if self.extra_fields:
            parts.append("hidden fields")
        if self.tags:
            parts.append("tags")
        return " + ".join(parts)

    def answer_hint_strings(self) -> list[str]:
        """Strings the hint must never contain (leak guard input)."""
        out = list(self.hidden_answers)
        return [s for s in dict.fromkeys(x.strip() for x in out) if s]


# --- stage 1: gather (Anki objects, main thread) --------------------------------


def gather_raw(card: Any, side: Side, reviewer: Any = None) -> RawCard:
    col = card.col
    note = card.note()
    nt = card.note_type()
    tmpls = nt.get("tmpls", [])
    if nt.get("type") == 1:  # cloze notetypes have a single template
        tmpl = tmpls[0] if tmpls else {}
    else:
        tmpl = tmpls[card.ord] if card.ord < len(tmpls) else (tmpls[0] if tmpls else {})
    q_html = card.question()
    a_html = card.answer()
    audio: list[str] = []
    try:
        tags = list(card.question_av_tags())
        if side == "answer":
            tags += list(card.answer_av_tags())
        for tag in tags:
            name = getattr(tag, "filename", None)
            if name and name not in audio:
                audio.append(name)
    except Exception:
        pass
    typed = None
    correct = None
    if reviewer is not None and side == "answer":
        typed = getattr(reviewer, "typedAnswer", None)
        correct = getattr(reviewer, "typeCorrect", None)
    try:
        deck_name = col.decks.name(card.current_deck_id())
    except Exception:
        deck_name = col.decks.name(card.did)
    return RawCard(
        card_id=card.id,
        note_id=note.id,
        ord=card.ord,
        side=side,
        notetype={
            "name": nt.get("name", ""),
            "type": nt.get("type", 0),
            "originalStockKind": nt.get("originalStockKind"),
            "flds": [{"name": f.get("name", "")} for f in nt.get("flds", [])],
        },
        template={"qfmt": tmpl.get("qfmt", ""), "afmt": tmpl.get("afmt", "")},
        deck_name=deck_name,
        tags=list(note.tags),
        q_html=q_html,
        a_html=a_html,
        fields=dict(note.items()),
        typed_answer=typed,
        type_correct=correct,
        audio_refs=audio,
        media_dir=col.media.dir(),
        lapses=getattr(card, "lapses", 0),
    )


# --- stage 2: build (pure) --------------------------------------------------------


def _strip_av(html: str) -> tuple[str, list[str]]:
    sounds = _SOUND_RE.findall(html)
    html = _SOUND_RE.sub("", html)
    html = _PLAY_RE.sub("", html)
    html = _TTS_RE.sub(lambda m: m.group(1), html)
    return html, sounds


def split_answer(a_html: str, q_html: str) -> tuple[str, str]:
    """Split rendered answer HTML into (question part, answer part)."""
    m = _HR_ANSWER_RE.search(a_html)
    if m:
        return a_html[: m.start()], a_html[m.end():]
    # Fallback: the answer often starts with the rendered question.
    q = q_html.strip()
    a = a_html.strip()
    if q and a.startswith(q):
        return q_html, a[len(q):]
    return q_html, a_html


def referenced_fields(template: dict[str, str]) -> set[str]:
    names: set[str] = set()
    for fmt in (template.get("qfmt", ""), template.get("afmt", "")):
        for ref in _TEMPLATE_REF_RE.findall(fmt):
            name = ref.split(":")[-1].strip()
            if name:
                names.add(name)
    return names


def _field_text(value: str, limit: int = 1000) -> str:
    value, _ = _strip_av(value)
    return html_to_text(value, limit=limit).text


def _type_field_name(spec: str) -> tuple[str, bool]:
    """``[[type:cloze:Text]]`` → ("Text", is_cloze)."""
    parts = spec.split(":")
    is_cloze = "cloze" in parts[:-1]
    return parts[-1], is_cloze


def _first_line(text: str, limit: int = 90) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("-#*> ").strip()
        s = s.replace("**", "").replace("⟦BLANK⟧", "[…]")
        s = re.sub(r"⟦BLANK: [^⟧]*⟧", "[…]", s)
        s = re.sub(r"[_^]\{([^}]*)\}", r"\1", s)  # V_{max} -> Vmax
        s = re.sub(r"(\w)[_^](\w)", r"\1\2", s)  # K_m -> Km
        if s and not s.startswith("[image"):
            return s if len(s) <= limit else s[: limit - 1] + "…"
    return "(image card)" if "[image" in text else "(empty card)"


def _hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def build_context(raw: RawCard, settings: Any, selection: str | None = None) -> CardContext:
    nt = raw.notetype
    side = raw.side
    is_io = io.is_io_notetype(nt)
    is_ioe = not is_io and io.is_legacy_ioe(nt)
    is_cloze = nt.get("type") == 1 and not is_io

    q_html, q_sounds = _strip_av(raw.q_html)
    a_html, a_sounds = _strip_av(raw.a_html)
    audio = list(dict.fromkeys(raw.audio_refs + q_sounds + (a_sounds if side == "answer" else [])))

    target: str | None = None
    typed = raw.typed_answer if settings.ctx("include_typed_answer") else None
    typed = typed.strip() if typed and typed.strip() else None
    hidden_answers: list[str] = []
    image_sources: list[ImageSource] = []
    io_note = None
    has_math = has_code = False

    type_match = _TYPE_RE.search(raw.q_html) or _TYPE_RE.search(raw.a_html)
    expected: str | None = None
    if type_match:
        fname, type_cloze = _type_field_name(type_match.group(1))
        expected = raw.type_correct
        if expected is None:
            value = raw.fields.get(fname, "")
            if not type_cloze:
                expected = _field_text(value)
        if expected:
            expected = _field_text(expected) if "<" in expected else expected.strip()

    if is_io or is_ioe:
        kind: CardKind = "image_occlusion"
        q_text, a_text, target, image_sources, io_note, has_math, has_code = _build_io(
            raw, side, is_ioe
        )
        hidden_answers = []
    else:
        q_part_html = q_html
        a_part_html = None
        if side == "answer":
            qpart, apart = split_answer(a_html, q_html)
            if is_cloze:
                # Cloze answers repeat the whole text with the target revealed;
                # send the question as the student saw it plus the revealed text.
                q_part_html, a_part_html = q_html, a_html
            else:
                q_part_html, a_part_html = qpart, apart
        q_part_html = _TYPE_RE.sub("", q_part_html)
        if a_part_html is not None:
            a_part_html = _TYPE_RE.sub(lambda _m: _escape_html(expected or ""), a_part_html)

        q_cloze = ClozeState("question") if is_cloze else None
        q_res = html_to_text(q_part_html, cloze=q_cloze)
        q_text = q_res.text
        image_refs: list[ImageRef] = list(q_res.images)
        has_math, has_code = q_res.has_math, q_res.has_code
        a_text = None
        if a_part_html is not None:
            a_cloze = ClozeState("answer") if is_cloze else None
            a_res = html_to_text(a_part_html, cloze=a_cloze, image_offset=len(image_refs))
            a_text = a_res.text
            # Avoid sending the same image twice when the answer repeats it.
            seen = {r.src for r in image_refs}
            image_refs += [r for r in a_res.images if r.src not in seen]
            has_math = has_math or a_res.has_math
            has_code = has_code or a_res.has_code
            if a_cloze is not None:
                target = a_cloze.target
        if q_cloze is not None:
            hidden_answers += q_cloze.hidden_answers
        if is_cloze:
            kind = "cloze"
        elif type_match:
            kind = "type_in"
            if side == "answer" and expected:
                target = expected
        elif q_text or a_text:
            kind = "basic"
        else:
            kind = "unknown"
        if side == "question" and not is_cloze:
            # The answer is never sent on the question side; keep it locally
            # for the hint leak guard only.
            _, apart = split_answer(a_html, q_html)
            apart = _TYPE_RE.sub(lambda _m: _escape_html(expected or ""), apart)
            ans = html_to_text(apart).text
            if ans:
                hidden_answers.append(ans)
            if expected:
                hidden_answers.append(expected)
        image_sources = [ImageSource(src=r.src, role="card_image", alt=r.alt) for r in image_refs]

    # Fields that the card doesn't show (optional context).
    extra: dict[str, str] = {}
    if settings.ctx("include_hidden_fields") and not (is_io or is_ioe):
        shown = referenced_fields(raw.template)
        for name, value in raw.fields.items():
            if name in shown or not value.strip():
                continue
            text = _field_text(value)
            if text:
                extra[name] = text

    # Images: respect the send toggle and the per-request cap.
    max_images = int(settings.ctx("max_images")) if settings.ctx("send_images") else 0
    allow_remote = bool(settings.ctx("allow_remote_images"))
    usable = [
        s for s in image_sources
        if not imgs.is_remote(s.src) or allow_remote
    ]
    skipped_remote = len(image_sources) - len(usable)
    sent_sources = usable[:max_images]
    skipped = len(image_sources) - len(sent_sources)
    not_sent_reason = None
    if image_sources and not settings.ctx("send_images"):
        not_sent_reason = "Sending images is turned off in settings."
    elif skipped_remote and not sent_sources:
        not_sent_reason = "Remote images are not sent (enable in settings)."

    sel = selection.strip() if selection and selection.strip() else None
    deck = raw.deck_name if settings.ctx("include_deck") else ""
    tags = list(raw.tags) if settings.ctx("include_tags") else []

    content_hash = _hash(
        {
            "kind": kind,
            "notetype": nt.get("name", ""),
            "deck": deck,
            "tags": tags,
            "side": side,
            "q": q_text,
            "a": a_text,
            "target": target,
            "typed": typed,
            "extra": extra,
            "images": [
                [imgs.image_identity(s.src, raw.media_dir), s.role, s.mask_src,
                 [(sh.kind, sh.ordinal, sorted(sh.props.items()), sh.active) for sh in (s.shapes or [])]]
                for s in sent_sources
            ],
            "skipped": skipped,
            "audio": audio,
            "selection": sel,
        }
    )

    return CardContext(
        card_id=raw.card_id,
        note_id=raw.note_id,
        side=side,
        card_kind=kind,
        notetype_name=nt.get("name", ""),
        deck_name=deck,
        tags=tags,
        question_text=q_text,
        answer_text=a_text if side == "answer" else None,
        target=target,
        typed_answer=typed if side == "answer" else None,
        extra_fields=extra,
        images=[],
        audio_refs=audio,
        has_math=has_math,
        has_code=has_code,
        selection=sel,
        content_hash=content_hash,
        image_sources=sent_sources,
        skipped_images=skipped,
        images_not_sent_reason=not_sent_reason,
        hidden_answers=hidden_answers,
        media_dir=raw.media_dir,
        title=_first_line(q_text),
        io_note=io_note,
    )


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_io(raw: RawCard, side: Side, legacy: bool):  # type: ignore[no-untyped-def]
    f = raw.fields
    html_side = raw.q_html if side == "question" else raw.a_html
    has_math = has_code = False
    sources: list[ImageSource] = []

    def text_of(name: str) -> str:
        nonlocal has_math, has_code
        res = html_to_text(_strip_av(f.get(name, ""))[0])
        has_math = has_math or res.has_math
        has_code = has_code or res.has_code
        for r in res.images:
            sources.append(ImageSource(src=r.src, role="card_image", alt=r.alt))
        return res.text

    def first_src(value: str) -> str | None:
        m = _IMG_SRC_RE.search(value or "")
        return m.group(1) if m else None

    if legacy:
        header = text_of("Header")
        footer = text_of("Footer") if "Footer" in f else ""
        q_text = "\n".join(x for x in (header, footer) if x)
        a_parts = [text_of(n) for n in ("Remarks", "Sources", "Extra 1", "Extra 2") if n in f] if side == "answer" else []
        a_text = "\n".join(x for x in a_parts if x) if side == "answer" else None
        mask_field = "Question Mask" if side == "question" else "Answer Mask"
        image_src = first_src(f.get("Image", ""))
        mask_src = first_src(f.get(mask_field, ""))
        if image_src:
            sources.insert(0, ImageSource(src=image_src, role="io_target", mask_src=mask_src))
        target = (
            "the region marked by the occlusion mask in image 1 (legacy Image Occlusion "
            "Enhanced: the target mask is red; other masks are yellow or outlined)"
        )
        return q_text, a_text, target, sources, "legacy", has_math, has_code

    shapes = io.parse_rendered_shapes(html_side)
    if not shapes:
        shapes = io.parse_occlusion_field(f.get("Occlusion", ""), active_ordinal(raw.ord))
    image_src = first_src(f.get("Image", "")) or io.find_io_image_src(html_side)
    header = text_of("Header")
    labels = io.describe_text_shapes(shapes)
    q_lines = [header] if header else []
    if labels:
        q_lines.append("Text labels on the image: " + "; ".join(labels))
    q_text = "\n".join(q_lines)
    a_text = None
    if side == "answer":
        a_parts = [text_of("Back Extra")]
        comments = text_of("Comments")
        if comments:
            a_parts.append("Comments: " + comments)
        a_text = "\n".join(x for x in a_parts if x)
    if image_src:
        sources.insert(0, ImageSource(src=image_src, role="io_target", shapes=shapes))
    active_text = [s.text for s in shapes if s.active and s.kind == "text" and s.text]
    target = "the outlined TARGET region in image 1"
    if active_text:
        target += " (labelled " + " / ".join(f'"{t}"' for t in active_text) + ")"
    n_active = sum(1 for s in shapes if s.active and s.kind != "text")
    if n_active > 1:
        target += f" ({n_active} outlined regions, tested together)"
    return q_text, a_text, target, sources, "native", has_math, has_code


# --- stage 3: images (background thread) ---------------------------------------------


def load_images(ctx: CardContext, allow_remote: bool) -> list[str]:
    """Decode/annotate/encode ``ctx.image_sources`` into ``ctx.images``.

    Returns names of images that could not be loaded.
    """
    failed: list[str] = []
    parts: list[imgs.ImagePart] = []
    for source in ctx.image_sources:
        data = imgs.load_bytes(source.src, ctx.media_dir, allow_remote)
        if data is None:
            failed.append(source.src)
            continue
        if source.role == "io_target":
            part = _prepare_io_image(ctx, source, data, allow_remote)
        else:
            part = imgs.prepare(data, source.src, role=source.role)
        if part is None:
            failed.append(source.src)
        else:
            parts.append(part)
    ctx.images = parts
    return failed


def _prepare_io_image(ctx: CardContext, source: ImageSource, data: bytes, allow_remote: bool):  # type: ignore[no-untyped-def]
    original = imgs.decode(data, source.src)
    if original is None or original.isNull():
        return None
    source_size = (original.width(), original.height())
    scaled = imgs.downscale(original)
    if source.mask_src:
        mask = imgs.load_bytes(source.mask_src, ctx.media_dir, allow_remote)
        if mask:
            scaled = io.composite_svg_mask(scaled, mask)
    elif source.shapes:
        scaled = io.annotate(scaled, source.shapes, ctx.side, source_size=source_size)
    return imgs.to_part(scaled, "io_target", source.src)
