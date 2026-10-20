"""Hint leak guard (spec §4.4).

Before a hint is rendered, check that the (normalized) answer does not appear
in it. The controller re-requests once with a stricter instruction and falls
back to a generic hint if the second attempt still leaks.
"""

from __future__ import annotations

import re
import unicodedata

GENERIC_HINT = (
    "Think about which category this belongs to and what you already know that is "
    "closely related. Picture where you last saw it, then try to recall the specific "
    "term before revealing the answer."
)

_NON_ALNUM = re.compile(r"[^0-9a-zͰ-ϿЀ-ӿ֐-׿؀-ۿ぀-ヿ㐀-鿿가-힯]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    return _NON_ALNUM.sub(" ", text).strip()


def _candidates(answer: str) -> list[str]:
    parts = [answer]
    for sep in ("\n", " / ", ";"):
        next_parts: list[str] = []
        for p in parts:
            next_parts.extend(p.split(sep))
        parts = next_parts
    out = [answer] + parts
    cands = []
    for c in out:
        n = normalize(c)
        # Single characters ("A", "3") would match ordinary words; skip them.
        if len(n.replace(" ", "")) < 2 and not _CJK.search(n):
            continue
        cands.append(n)
    return list(dict.fromkeys(cands))


def leaks(hint: str, answers: list[str]) -> bool:
    norm_hint = f" {normalize(hint)} "
    for answer in answers:
        for cand in _candidates(answer):
            if not cand:
                continue
            if _CJK.search(cand):
                if cand.replace(" ", "") in norm_hint.replace(" ", ""):
                    return True
            elif f" {cand} " in norm_hint:
                return True
    return False
