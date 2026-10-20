"""Static model registry (spec §6.2).

Adding a model later means adding one entry here. The settings selector, the
image capability check (§5.6) and the reasoning controls (§6.3) all read from
this registry, so no UI changes are needed. No network call lists models.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str
    short_name: str
    vision: bool
    reasoning: bool
    default_max_tokens: int


DEFAULT_MODEL_ID = "~deepseek/deepseek-flash-latest"

REGISTRY: tuple[ModelInfo, ...] = (
    ModelInfo(
        id=DEFAULT_MODEL_ID,
        display_name="DeepSeek Flash (latest)",
        short_name="DeepSeek Flash",
        vision=True,
        reasoning=True,
        default_max_tokens=1200,
    ),
)


def get_model(model_id: str) -> ModelInfo:
    for m in REGISTRY:
        if m.id == model_id:
            return m
    return REGISTRY[0]


def all_models() -> tuple[ModelInfo, ...]:
    return REGISTRY
