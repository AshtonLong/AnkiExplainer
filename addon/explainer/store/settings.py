"""Typed settings over the add-on config.

Defaults come from ``config.json`` (the single source of truth). The user's
config is deep-merged over them and every leaf is validated; anything invalid
silently falls back to its default so a hand-edited config can never break
review. The API key is *never* part of this config (see ``secrets.py``).
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Callable

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EFFORTS = ("none", "minimal", "low", "medium", "high", "max")
EFFORT_LABELS = {
    "none": "Off",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "max": "Max",
}
REASONING_TIERS = ("hint", "concise", "followup", "deep")

# Allowed values for enum-like leaves. Paths not listed are validated by type.
ENUMS: dict[str, tuple[Any, ...]] = {
    "reasoning.hint.effort": EFFORTS,
    "reasoning.concise.effort": EFFORTS,
    "reasoning.followup.effort": EFFORTS,
    "reasoning.deep.effort": EFFORTS,
    "reasoning.display": ("hidden", "collapsible", "off"),
    "output.concise_length": ("short", "standard"),
    "output.level": ("intro", "undergraduate", "graduate"),
    "behavior.question_side": ("hint", "explain_no_reveal", "disabled"),
    "behavior.panel_position": ("right", "left", "floating"),
}

# (min, max) for numeric leaves.
RANGES: dict[str, tuple[float, float]] = {
    "output.temperature": (0.0, 1.0),
    "output.max_tokens.hint": (50, 32000),
    "output.max_tokens.concise": (100, 32000),
    "output.max_tokens.followup": (100, 64000),
    "output.max_tokens.deep": (100, 64000),
    "behavior.struggle_nudge_lapses": (1, 100),
    "behavior.panel_width": (300, 4000),
    "behavior.font_size": (10, 28),
    "context.max_images": (0, 8),
    "context.cache_retention_days": (1, 3650),
}

# Leaves whose default is null but which accept a value of this type.
NULLABLE: dict[str, tuple[type, ...]] = {
    "reasoning.budget_tokens": (int,),
    "usage.monthly_soft_cap_usd": (int, float),
}
NULLABLE_RANGES: dict[str, tuple[float, float]] = {
    "reasoning.budget_tokens": (256, 64000),
    "usage.monthly_soft_cap_usd": (0.01, 10000.0),
}


def load_defaults() -> dict[str, Any]:
    with open(os.path.join(ADDON_DIR, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _type_ok(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    return True


def _validate_leaf(path: str, default: Any, value: Any) -> Any:
    if path in NULLABLE:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, NULLABLE[path]):
            return default
        lo, hi = NULLABLE_RANGES[path]
        return value if lo <= value <= hi else default
    if not _type_ok(default, value):
        return default
    if path in ENUMS and value not in ENUMS[path]:
        return default
    if path in RANGES:
        lo, hi = RANGES[path]
        if not lo <= value <= hi:
            return default
        if isinstance(default, float):
            value = float(value)
    if path == "shortcuts.chips":
        if not all(isinstance(v, str) for v in value):
            return default
        value = (list(value) + list(default)[len(value):])[: len(default)]
    if path == "chips":
        from ..llm.prompts import CHIPS  # local import: avoid cycle at import time

        cleaned = [c for c in value if isinstance(c, str) and c in CHIPS]
        value = list(dict.fromkeys(cleaned))
    return value


def merge(defaults: dict[str, Any], user: dict[str, Any] | None, prefix: str = "") -> dict[str, Any]:
    """Deep-merge ``user`` over ``defaults`` with per-leaf validation."""
    out: dict[str, Any] = {}
    user = user if isinstance(user, dict) else {}
    for key, default in defaults.items():
        path = f"{prefix}{key}"
        if key not in user:
            out[key] = copy.deepcopy(default)
            continue
        value = user[key]
        if isinstance(default, dict) and path != "prompts.overrides":
            out[key] = merge(default, value if isinstance(value, dict) else {}, path + ".")
        elif path == "prompts.overrides":
            out[key] = _clean_overrides(value)
        else:
            out[key] = _validate_leaf(path, copy.deepcopy(default), copy.deepcopy(value))
    return out


def _clean_overrides(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for k, v in value.items():
        if isinstance(v, str):
            cleaned[k] = {"text": v, "default_hash": ""}
        elif isinstance(v, dict) and isinstance(v.get("text"), str):
            cleaned[k] = {"text": v["text"], "default_hash": str(v.get("default_hash", ""))}
    return cleaned


class Settings:
    """Validated settings. Reads are cheap; ``save()`` persists via the writer."""

    def __init__(
        self,
        user_config: dict[str, Any] | None = None,
        writer: Callable[[dict[str, Any]], None] | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> None:
        self.defaults = defaults if defaults is not None else load_defaults()
        self._writer = writer
        self.data = merge(self.defaults, user_config)

    # -- generic access -------------------------------------------------
    def get(self, path: str, fallback: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return fallback
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def default(self, path: str) -> Any:
        node: Any = self.defaults
        for part in path.split("."):
            node = node[part]
        return copy.deepcopy(node)

    def replace(self, data: dict[str, Any]) -> None:
        self.data = merge(self.defaults, data)

    def save(self) -> None:
        self.data = merge(self.defaults, self.data)
        if self._writer:
            self._writer(copy.deepcopy(self.data))

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    # -- typed helpers ----------------------------------------------------
    @property
    def model(self) -> str:
        return self.data["model"]

    def effort(self, tier: str) -> str:
        return self.data["reasoning"][tier]["effort"]

    def max_tokens(self, tier: str) -> int:
        return int(self.data["output"]["max_tokens"][tier])

    @property
    def reasoning_display(self) -> str:
        return self.data["reasoning"]["display"]

    @property
    def budget_tokens(self) -> int | None:
        return self.data["reasoning"]["budget_tokens"]

    @property
    def temperature(self) -> float:
        return float(self.data["output"]["temperature"])

    @property
    def stream(self) -> bool:
        return bool(self.data["output"]["stream"])

    @property
    def question_side(self) -> str:
        return self.data["behavior"]["question_side"]

    @property
    def chips(self) -> list[str]:
        return list(self.data["chips"])

    def ctx(self, key: str) -> Any:
        return self.data["context"][key]

    def behavior(self, key: str) -> Any:
        return self.data["behavior"][key]

    def output(self, key: str) -> Any:
        return self.data["output"][key]

    @property
    def soft_cap(self) -> float | None:
        return self.data["usage"]["monthly_soft_cap_usd"]

    @property
    def prompt_overrides(self) -> dict[str, Any]:
        return self.data["prompts"]["overrides"]
