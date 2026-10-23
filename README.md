# Anki Explainer

Press **X** during review and a concise explanation of exactly the card on screen streams into a side panel, usually within about a second. You can then ask follow-ups or use one-click learning moves (Go deeper, Example, Simpler, Analogy, Mnemonic, Quiz me, Compare, and more), press **Esc**, and keep grading.

- Works on Basic, reversed, type-in, Cloze, native Image Occlusion, and cards with images, tables, MathJax, or code.
- Protects active recall. On the question side, **X** gives a *hint* that never states the answer (a leak guard enforces this), plus **Stronger hint** and **Show answer + explain**.
- Strictly on demand. Nothing is sent until you press Explain, and only the card on screen is sent. Explaining never changes scheduling.
- Bring your own OpenRouter key. There is no server, account, or subscription. The default model is `~deepseek/deepseek-flash-latest`, which always points to the newest DeepSeek Flash. An explanation typically costs a fraction of a cent.

Requires Anki desktop **25.02 or newer** on Windows, macOS, or Linux.

## Install

- **From a file:** download `anki_explainer.ankiaddon` from the [latest release](https://github.com/AshtonLong/AnkiExplainer/releases/latest), then Tools → Add-ons → *Install from file…* → choose it and restart Anki. To build it yourself, run `python scripts/build_addon.py`.
- **For development:** link or copy the `addon/` folder into your add-ons folder as `anki_explainer` (Tools → Add-ons → *View Files*), then restart Anki.

## Getting started

1. Start a review and press **X**, or click **Explain** in the bottom bar.
2. On first use the panel asks for your OpenRouter key. Get one at <https://openrouter.ai/keys>, paste it, and press **Connect**. The key is checked right away, and your explanation follows.
3. Press **X** again on any card. Re-opening a card you already explained is instant and free (served from the local cache).

| Key | Where | Action |
|---|---|---|
| `X` | Reviewer | Explain (answer side), hint (question side), or toggle the panel |
| `Shift+X` | Reviewer | Open the panel and focus the follow-up box |
| `Esc` | Panel | Return focus to the reviewer (press twice to close the panel) |
| `Enter` / `Shift+Enter` | Follow-up box | Send / new line |
| `Ctrl+.` | Panel | Stop generating |
| `Alt+1` … `Alt+6` | Panel open | Trigger the first six chips |
| `1`–`4`, `Space` | Always | Anki's grading and flipping. These are never intercepted. |

All keys except `1`–`4` and `Space` can be rebound under **Tools → Anki Explainer Settings… → Shortcuts**, which checks for conflicts as you type.

**Explain selection:** select a word or formula on the card, right-click, and choose *Explain selection*. Pressing **X** while text is selected does the same.

## Settings

Open **Tools → Anki Explainer Settings…**, the ⚙ in the panel, or the *Config* button in the add-ons list. The pages are:

- **Account:** your key (masked), Test connection (shows key label and remaining credit), and Remove key.
- **Model:** the model selector. v1 ships with DeepSeek Flash (latest) only.
- **Reasoning:** effort per mode (Hint, Concise, Follow-ups, Deep), an advanced token budget, and whether reasoning is shown.
- **Output:** concise length, student level, what you're studying, explanation language, memory hooks, temperature, streaming, and token limits.
- **Behavior:** question-side behavior, auto-open on answer reveal, struggle nudge, panel position, font size, reduce motion, show cost, and accent color.
- **Learning moves:** which chips appear and in what order.
- **Shortcuts:** see above.
- **Card context & cache:** what goes into the prompt (images, deck, tags, hidden fields, typed answers), plus cache retention and Clear cache.
- **Prompts (advanced):** edit any prompt. Your edits survive updates, and the page warns you if a default changes upstream.
- **Usage:** requests, tokens, and spend for the session and month; an optional monthly soft cap; and private local metrics.

## Privacy

- Your key is stored **only on this computer**. It goes in the OS keyring if the `keyring` package is available; otherwise it goes in `user_files/secret.json` with user-only permissions. It never syncs through AnkiWeb and is never written to the config, the log, or the cache. After saving, it is shown only as `sk-or-…last4`.
- The add-on makes only two kinds of network call: a chat request when you ask for one, and `GET /api/v1/key` when you press Connect or Test connection. Both go to `https://openrouter.ai`. There is no telemetry.
- Requests use your OpenRouter account's own provider and data-policy settings. The add-on sends no routing or retention parameters, so you configure those on openrouter.ai.
- The add-on is read-only toward your collection. It never edits notes, cards, or note types.
- Model output is sanitized (marked → DOMPurify) before rendering. Card text is treated as data in the prompt, and links open in your system browser.

## How it works

```
spec.md                    product & technical specification
addon/                     everything that ships in the .ankiaddon
  __init__.py              version check, hook registration (lazy: ~15 ms import)
  explainer/
    hooks.py               every gui_hooks registration
    controller.py          card → context → request → panel; caching, hints, chips, history
    context/               HTML→text, cloze targets, Image Occlusion parsing + annotation, images
    llm/                   OpenRouter client (SSE, retries, timeouts), prompts, threads, leak guard
    store/                 settings, API key storage, SQLite cache/history/usage
    ui/                    dock panel, bottom-bar button + shortcuts, settings dialog
  web/                     panel.html/css/js, bottom-bar button, vendored marked + DOMPurify
tests/                     unit tests, real-Anki fixtures, fake OpenRouter server,
                           in-Anki self-test driver, 40-card prompt eval set
scripts/                   build, fixtures, in-Anki self-test runner, prompt eval runner
```

The panel is a `QDockWidget` with its own `AnkiWebView`, so it never touches card templates. Streaming deltas are coalesced every 33 ms, and everything slow runs on background threads.

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install "aqt[qt6]" anki pytest   # Anki 25.02+ from PyPI

.venv/Scripts/python -m pytest tests                  # 170+ unit/behaviour tests (no Anki window)
.venv/Scripts/python scripts/run_anki_selftest.py     # drives a real Anki in a throwaway profile
.venv/Scripts/python scripts/make_fixtures.py         # regenerate card fixtures from real Anki
.venv/Scripts/python scripts/build_addon.py           # → dist/anki_explainer.ankiaddon
.venv/Scripts/python scripts/run_eval.py --dry-run    # prompt eval set (40 cards), offline check
OPENROUTER_API_KEY=… .venv/Scripts/python scripts/run_eval.py --judge   # scored prompt eval
```

The **in-Anki self-test** launches Anki with a fresh base folder, so your real profile is never touched. It points the add-on at a local fake OpenRouter server, then:

- drives a scripted review session with real key presses;
- checks every card type, hints and the leak guard, chips, errors, Stop, and cancel-on-card-change;
- checks the on-demand guarantee, light and dark themes, history, settings, and the narrow-window floating mode;
- checks the performance budgets and output sanitization;
- writes screenshots to `tests/_artifacts/`.

**Prompt changes:** run `scripts/run_eval.py --judge` before and after. It scores six criteria (targets the fact, accurate, within length, explains why, hint never leaks, flags bad cards) and fails if any criterion regresses against `tests/eval/baseline.json`. Bump `PROMPT_VERSION` in `llm/prompts.py` when a default prompt changes.

## License

Anki Explainer is released under the [MIT License](LICENSE). A copy also ships inside the add-on package.

Vendored libraries keep their own licenses: [marked](https://github.com/markedjs/marked) (MIT) and [DOMPurify](https://github.com/cure53/DOMPurify) (Apache-2.0 / MPL-2.0). Their license files are in `addon/web/vendor/`.
