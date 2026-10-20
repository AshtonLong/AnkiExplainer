"""Prompt eval (spec §9.4).

Runs the default prompts over ``tests/eval/cards.json`` (40 cards across
medicine, languages, STEM, law and programming, including clozes, image
occlusion, type-in and deliberately wrong cards) and scores each prompt
version on six criteria:

  1. targets the tested fact      (LLM judge)
  2. accurate                     (LLM judge)
  3. within length                (automatic)
  4. explains why, not restating  (LLM judge)
  5. hint never leaks the answer  (automatic, same guard as the add-on)
  6. flags bad cards              (automatic: deliberately wrong cards must get
                                   a "⚠ Card note"; correct cards must not)

A prompt change ships only if no criterion regresses against the baseline.

    python scripts/run_eval.py --dry-run                 # validate + render prompts, no network
    OPENROUTER_API_KEY=sk-or-... python scripts/run_eval.py --judge
    python scripts/run_eval.py --judge --update-baseline # accept the current scores
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests", "eval"))
sys.path.insert(0, os.path.join(ROOT, "addon"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import evalset  # noqa: E402
from explainer.context.extractor import build_context, load_images  # noqa: E402
from explainer.llm import leak_guard, prompts  # noqa: E402
from explainer.llm.models import get_model  # noqa: E402
from explainer.llm.openrouter import CancelToken, OpenRouterClient, build_body  # noqa: E402
from explainer.store.settings import Settings  # noqa: E402

REPORTS = os.path.join(ROOT, "tests", "eval", "reports")
BASELINE = os.path.join(ROOT, "tests", "eval", "baseline.json")
CRITERIA = ["targets", "accurate", "within_length", "explains_why", "hint_no_leak", "flags_bad_cards"]
TOLERANCE = 0.03
FLAG_RE = re.compile(r"⚠\s*card note|likely wrong|card (?:is|looks|seems) (?:wrong|incorrect|outdated)", re.I)

JUDGE_PROMPT = """You are grading an AI tutor's explanation of an Anki flashcard.

Card (as the student saw it):
{card}

Tutor's explanation:
<<<
{explanation}
>>>

Score each criterion 1 (yes) or 0 (no):
- targets: explains the specific fact this card tests (the TARGET / answer), not a generic overview.
- accurate: contains no factual errors (if the card itself is wrong, a correct explanation that says so is accurate).
- explains_why: explains WHY the answer is the answer instead of just restating the card.

Reply with JSON only: {{"targets": 0|1, "accurate": 0|1, "explains_why": 0|1, "notes": "<one short sentence>"}}"""


def words(text: str) -> int:
    text = re.sub(r"\\\(.*?\\\)|\\\[.*?\\\]", " x ", text, flags=re.S)
    return len(re.findall(r"[\w’'-]+", text))


def build(card: evalset.EvalCard, side: str, settings: Settings) -> Any:
    ctx = build_context(card.raw(side), settings)
    load_images(ctx, allow_remote=False)
    return ctx


def messages_for(ctx: Any, mode: str, settings: Settings) -> list[dict[str, Any]]:
    ps = prompts.PromptSet(settings)
    instruction = ps.mode(mode)
    vision = get_model(settings.model).vision
    return [
        {"role": "system", "content": ps.system()},
        {"role": "user", "content": prompts.first_user_content(ctx, instruction, vision=vision)},
    ]


def ask(client: OpenRouterClient, settings: Settings, messages: list[dict[str, Any]], tier: str, model: str | None = None) -> tuple[str, float]:
    body = build_body(
        model or settings.model, messages, stream=False, temperature=settings.temperature,
        max_tokens=settings.max_tokens(tier), effort=settings.effort(tier),
        budget_tokens=None, exclude_reasoning=True,
    )
    result = client.chat(body, lambda *_: None, CancelToken())
    return result.content, float((result.usage or {}).get("cost") or 0.0)


def judge(client: OpenRouterClient, settings: Settings, ctx: Any, explanation: str, model: str | None) -> dict[str, Any]:
    card = prompts.render_card_block(ctx, vision=False)
    msg = [{"role": "user", "content": JUDGE_PROMPT.format(card=card, explanation=explanation)}]
    text, _ = ask(client, settings, msg, "deep", model)
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    return {k: int(bool(data.get(k))) for k in ("targets", "accurate", "explains_why")} | {"notes": data.get("notes", "")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="validate the set and render prompts; no network")
    ap.add_argument("--judge", action="store_true", help="score criteria 1, 2, 4 with an LLM judge")
    ap.add_argument("--judge-model", default=None, help="OpenRouter model id for the judge (default: the add-on model)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841 (needed for image work)
    settings = Settings({})
    cards = evalset.load()
    if args.limit:
        cards = cards[: args.limit]
    version = prompts.PromptSet(settings).fingerprint("concise")

    if args.dry_run:
        total_chars = 0
        for card in cards:
            for side, mode in (("answer", "concise"), ("question", "hint")):
                ctx = build(card, side, settings)
                msgs = messages_for(ctx, mode, settings)
                total_chars += len(json.dumps(msgs))
        print(f"{len(cards)} cards OK; prompt version {version}; ~{total_chars // 4:,} prompt tokens per full run")
        return 0

    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("Set OPENROUTER_API_KEY (or use --dry-run).")
        return 2
    client = OpenRouterClient(key)
    rows: list[dict[str, Any]] = []
    cost = 0.0
    length_limit = prompts.LENGTH_WORDS[settings.output("concise_length")]
    for card in cards:
        row: dict[str, Any] = {"id": card.id, "domain": card.domain, "type": card.type}
        a_ctx = build(card, "answer", settings)
        explanation, c1 = ask(client, settings, messages_for(a_ctx, "concise", settings), "concise")
        q_ctx = build(card, "question", settings)
        hint, c2 = ask(client, settings, messages_for(q_ctx, "hint", settings), "hint")
        cost += c1 + c2
        row["explanation"], row["hint"] = explanation, hint
        row["within_length"] = int(words(explanation) <= length_limit * 1.25 and words(hint) <= 40 * 1.25)
        question_src = (card.data.get("front", "") + card.data.get("text", "")).lower()
        guarded = [a for a in card.answers + q_ctx.answer_hint_strings() if a and a.lower() not in question_src]
        row["hint_no_leak"] = int(not leak_guard.leaks(hint, guarded))
        flagged = bool(FLAG_RE.search(explanation))
        row["flags_bad_cards"] = int(flagged == card.deliberately_wrong)
        if args.judge:
            row.update(judge(client, settings, a_ctx, explanation, args.judge_model))
        rows.append(row)
        print(f"{card.id:8} len={row['within_length']} leak-free={row['hint_no_leak']} flag={row['flags_bad_cards']}"
              + (f" judge={row['targets']}{row['accurate']}{row['explains_why']}" if args.judge else ""))

    scores = {c: sum(r[c] for r in rows) / len(rows) for c in CRITERIA if all(c in r for r in rows)}
    os.makedirs(REPORTS, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = {"prompt_version": version, "model": settings.model, "scores": scores, "cost_usd": cost, "rows": rows}
    with open(os.path.join(REPORTS, f"report-{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    with open(os.path.join(REPORTS, f"report-{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(f"# Prompt eval {stamp}\n\nPrompt version `{version}` · model `{settings.model}` · cost ${cost:.4f}\n\n")
        f.write("| criterion | score |\n|---|---|\n" + "".join(f"| {k} | {v:.0%} |\n" for k, v in scores.items()))
        for r in rows:
            f.write(f"\n## {r['id']} ({r['domain']}, {r['type']})\n\n**Explanation**\n\n{r['explanation']}\n\n**Hint**\n\n{r['hint']}\n")
    print("\nScores:", ", ".join(f"{k} {v:.0%}" for k, v in scores.items()), f"· cost ${cost:.4f}")

    regressions = []
    if os.path.exists(BASELINE):
        with open(BASELINE, encoding="utf-8") as f:
            base = json.load(f).get("scores", {})
        regressions = [k for k, v in scores.items() if k in base and v < base[k] - TOLERANCE]
        for k in regressions:
            print(f"REGRESSION: {k} {scores[k]:.0%} < baseline {base[k]:.0%}")
    if args.update_baseline:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"prompt_version": version, "model": settings.model, "scores": scores}, f, indent=1)
        print("Baseline updated.")
        return 0
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
