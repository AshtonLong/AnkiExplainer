"""Rendered card HTML → structure-preserving plain text (spec §5.2).

The conversion builds a small DOM tree with the stdlib parser and then renders
it: lists become ``- ``, tables become Markdown tables, ``<br>``/blocks become
newlines, ``<b>``/``<i>`` become ``**``/``*``, ``<sub>``/``<sup>`` become
``_``/``^``, code stays fenced and MathJax/LaTeX source is kept verbatim.
Scripts, styles, hidden elements and UI controls are dropped. Images are
collected (not inlined) and replaced with ``[image N]`` markers.

Cloze spans are delegated to :mod:`.cloze` through the ``ClozeState`` object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .cloze import ClozeState

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
SKIP = {
    "script", "style", "template", "noscript", "button", "input", "select",
    "textarea", "canvas", "svg", "audio", "video", "object", "iframe", "head",
    "title", "meta", "link",
}
BLOCK = {
    "address", "article", "aside", "blockquote", "center", "dd", "details",
    "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "summary", "table", "tr", "ul", "body",
    "html",
}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

MAX_SIDE_CHARS = 8000

_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_WS = re.compile(r"[ \t\r\n\f\v    ]+")
_BLOCK_MARK = "\x00"  # internal: "a block boundary goes here"
_BLOCK_RUN = re.compile(r"[ \t]*\x00(?:[ \t\r\f\v]*\x00)*[ \t]*")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    text: str = ""
    parent: "Node | None" = None

    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def iter(self):  # type: ignore[no-untyped-def]
        yield self
        for c in self.children:
            yield from c.iter()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs})
        # Implicitly close an open <p>/<li>/<td>/<tr> when a sibling starts.
        if tag in ("li", "p", "tr", "td", "th", "dt", "dd"):
            closers = {
                "li": {"li"},
                "p": {"p"},
                "tr": {"tr", "td", "th"},
                "td": {"td", "th"},
                "th": {"td", "th"},
                "dt": {"dt", "dd"},
                "dd": {"dt", "dd"},
            }[tag]
            if self.stack[-1].tag in closers:
                self.stack.pop()
                if tag == "tr" and self.stack[-1].tag in closers:
                    self.stack.pop()
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs})
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        node = Node("#text", text=data, parent=self.stack[-1])
        self.stack[-1].children.append(node)


def parse_html(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def is_hidden(node: Node) -> bool:
    if "hidden" in node.attrs:
        return True
    if node.tag == "input" and node.attrs.get("type", "").lower() == "hidden":
        return True
    style = node.attrs.get("style", "")
    return bool(style and _HIDDEN_STYLE.search(style))


@dataclass
class ImageRef:
    src: str
    alt: str = ""


@dataclass
class TextResult:
    text: str
    images: list[ImageRef]
    has_math: bool
    has_code: bool
    truncated: bool = False


_MATH_RE = re.compile(r"\\\(|\\\[|\$\$|\\begin\{")


class _Renderer:
    def __init__(self, cloze: ClozeState | None, image_offset: int) -> None:
        self.cloze = cloze
        self.images: list[ImageRef] = []
        self.image_offset = image_offset
        self.has_code = False

    # Returns text where block boundaries are marked by _BLOCK_MARK and
    # explicit line breaks by "\n". Whitespace is collapsed later.
    def render(self, node: Node, pre: bool = False) -> str:
        if node.tag == "#text":
            return node.text if pre else _WS.sub(" ", node.text)
        if node.tag in SKIP or is_hidden(node):
            return ""
        tag = node.tag

        if self.cloze is not None:
            handled = self.cloze.handle(node, lambda n: self.inline_text(n))
            if handled is not None:
                return handled

        if tag == "br":
            return "\n"
        if tag == "hr":
            return f"{_BLOCK_MARK}---{_BLOCK_MARK}"
        if tag == "img":
            src = node.attrs.get("src", "")
            if not src:
                return ""
            self.images.append(ImageRef(src=src, alt=node.attrs.get("alt", "")))
            n = self.image_offset + len(self.images)
            alt = node.attrs.get("alt", "").strip()
            return f"[image {n}: {alt}]" if alt else f"[image {n}]"
        if tag == "pre":
            self.has_code = True
            code = "".join(self.render(c, pre=True) for c in node.children)
            lang = ""
            for c in node.children:
                if c.tag == "code":
                    for cls in c.classes():
                        if cls.startswith("language-"):
                            lang = cls[len("language-"):]
            code = code.strip("\n")
            return f"{_BLOCK_MARK}```{lang}\n{code}\n```{_BLOCK_MARK}"
        if tag == "code" and not pre:
            self.has_code = True
            inner = self.inline_text(node)
            return f"`{inner}`" if inner else ""
        if tag in ("ul", "ol"):
            return self.render_list(node)
        if tag == "table":
            return self.render_table(node)

        inner = self.render_children(node, pre)

        if tag in ("b", "strong"):
            return _wrap(inner, "**")
        if tag in ("i", "em"):
            return _wrap(inner, "*")
        if tag in ("sub", "sup"):
            mark = "_" if tag == "sub" else "^"
            s = inner.strip()
            if not s:
                return ""
            return f"{mark}{s}" if len(s) == 1 else f"{mark}{{{s}}}"
        if tag in HEADINGS:
            s = _collapse_inline(inner)
            return f"{_BLOCK_MARK}**{s}**{_BLOCK_MARK}" if s else ""
        if tag in BLOCK:
            return f"{_BLOCK_MARK}{inner}{_BLOCK_MARK}"
        if tag in ("td", "th"):
            return f" {inner} "
        return inner

    def render_children(self, node: Node, pre: bool = False) -> str:
        return "".join(self.render(c, pre) for c in node.children)

    def inline_text(self, node: Node) -> str:
        """Render a subtree and flatten it to one line."""
        return _collapse_inline(self.render_children(node))

    def render_list(self, node: Node, depth: int = 0) -> str:
        ordered = node.tag == "ol"
        lines: list[str] = []
        n = 0
        for child in node.children:
            if child.tag == "#text" and not child.text.strip():
                continue
            if child.tag != "li":
                text = _collapse_inline(self.render(child))
                if text:
                    lines.append("  " * depth + text)
                continue
            if is_hidden(child):
                continue
            n += 1
            body_parts: list[str] = []
            nested: list[str] = []
            for gc in child.children:
                if gc.tag in ("ul", "ol"):
                    nested.append(self.render_list(gc, depth + 1))
                else:
                    body_parts.append(self.render(gc))
            body = _collapse_inline("".join(body_parts))
            bullet = f"{n}. " if ordered else "- "
            lines.append("  " * depth + bullet + body)
            lines.extend(x for x in nested if x)
        out = "\n".join(lines)
        return f"{_BLOCK_MARK}{out}{_BLOCK_MARK}" if depth == 0 else out

    def render_table(self, node: Node) -> str:
        rows: list[list[str]] = []
        header_row = -1

        def walk(n: Node) -> None:
            nonlocal header_row
            for c in n.children:
                if c.tag == "#text" or is_hidden(c):
                    continue
                if c.tag == "tr":
                    cells = [
                        _collapse_inline(self.render_children(cell)).replace("|", "\\|")
                        for cell in c.children
                        if cell.tag in ("td", "th") and not is_hidden(cell)
                    ]
                    if cells:
                        if header_row < 0 and all(
                            cell.tag == "th" for cell in c.children if cell.tag in ("td", "th")
                        ):
                            header_row = len(rows)
                        rows.append(cells)
                elif c.tag in ("thead", "tbody", "tfoot"):
                    walk(c)
                elif c.tag == "caption":
                    cap = _collapse_inline(self.render_children(c))
                    if cap:
                        rows.append([f"**{cap}**"])

        walk(node)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        head = rows[header_row] if header_row >= 0 else rows[0]
        body = [r for i, r in enumerate(rows) if i != (header_row if header_row >= 0 else 0)]
        lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in body]
        return _BLOCK_MARK + "\n".join(lines) + _BLOCK_MARK


def _wrap(inner: str, mark: str) -> str:
    stripped = inner.strip()
    if not stripped:
        return inner
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()):]
    return f"{lead}{mark}{stripped}{mark}{trail}"


def _collapse_inline(text: str) -> str:
    text = text.replace(_BLOCK_MARK, " ")
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return " ".join(x for x in lines if x)


def _finalize(text: str) -> str:
    # Turn block markers into line breaks, then tidy whitespace line by line,
    # keeping fenced code blocks verbatim.
    # Adjacent block boundaries (with only spaces between) are one line break.
    text = _BLOCK_RUN.sub("\n", text)
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line.strip())
            continue
        if in_fence:
            out.append(line.rstrip())
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = _WS.sub(" ", line).strip()
        # Preserve list nesting indentation only.
        if stripped.startswith(("- ", "1", "2", "3", "4", "5", "6", "7", "8", "9")) and indent:
            stripped = " " * min(indent, 8) + stripped
        out.append(stripped)
    # Collapse runs of blank lines.
    result: list[str] = []
    blank = 0
    for line in out:
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        result.append(line)
    return "\n".join(result).strip("\n")


def truncate_middle(text: str, limit: int = MAX_SIDE_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    removed = len(text) - limit
    head = text[: limit // 2]
    tail = text[-(limit - limit // 2):]
    return f"{head}\n…[{removed} characters truncated]…\n{tail}", True


def html_to_text(
    html: str,
    cloze: ClozeState | None = None,
    image_offset: int = 0,
    limit: int = MAX_SIDE_CHARS,
) -> TextResult:
    root = parse_html(html)
    renderer = _Renderer(cloze, image_offset)
    raw = renderer.render(root)
    text = _finalize(raw)
    has_math = bool(_MATH_RE.search(text)) or "<math" in html.lower()
    has_code = renderer.has_code or "```" in text
    text, truncated = truncate_middle(text, limit)
    return TextResult(
        text=text,
        images=renderer.images,
        has_math=has_math,
        has_code=has_code,
        truncated=truncated,
    )
