# Anki Explainer — Product & Technical Specification

**Status:** Draft v1.1 (scope decisions locked, see §16)
**Date:** 2026-10-19
**Target platform:** Anki Desktop 25.02+ (built and tested against 26.09.x) on Windows, macOS, Linux
**AI backend:** OpenRouter, bring your own key (BYOK)
**Default model:** `~deepseek/deepseek-flash-latest` (always the newest DeepSeek Flash; today that is DeepSeek V4.1 Flash `1017`)

---

## 1. Summary

Anki Explainer is an Anki desktop add-on. With one click or keypress during review, it gives a **concise, clear explanation of the card on screen**. That includes basic, reversed, cloze, image-occlusion, and image-heavy cards. From there the student can go further without leaving Anki: a deeper explanation, a real-world example, a simpler version, an analogy, a mnemonic, a quick self-check question, or a free-form follow-up.

The add-on talks directly to OpenRouter with the user's own API key. There is no server, account, or subscription. The model is the latest DeepSeek Flash because it is fast, cheap, and accepts images. A model selector is in place, but v1 ships with only that one model; more models come later. Reasoning effort, length, and other settings live in a dedicated settings dialog.

Explanations are generated **strictly on demand**: only for the card on screen, only when the user presses Explain. Nothing is fetched in the background or ahead of time.

### 1.1 The problem

During review, a student hits a card they don't really understand. Today the loop looks like this:

1. Alt-tab to a browser, then retype or copy the card's content into Google.
2. Skim results that are too long, too general, or about a slightly different concept.
3. Often still confused, so open an AI chat, paste the card again, and ask for clarification.
4. Ask a follow-up or two ("simpler?", "example?").
5. Alt-tab back to Anki, having lost their place, their rhythm, and 1–5 minutes.

Each detour costs time, breaks focus and review momentum, and makes it more likely the student gives up and presses "Good" on a card they don't understand. That last outcome is the worst one for long-term retention.

### 1.2 The solution in one sentence

**Press `X` and an explanation of exactly this card streams into a side panel within about a second. Keep asking follow-ups in the same panel, then press `Esc` and keep grading.**

### 1.3 Design principles

Every decision in this spec traces back to one of these principles:

| # | Principle | What it means in practice |
|---|-----------|---------------------------|
| P1 | **Zero-friction access** | One key or click. No copying, no context-switching, no prompt writing. The card *is* the prompt. |
| P2 | **Concise first, depth on demand** | The first answer is short (≈60–120 words). Depth is always one click away, never forced. |
| P3 | **Protect active recall** | Never show the answer before the student has tried to recall it. On the question side the add-on offers a *hint*, not the answer. |
| P4 | **Never break review flow** | The panel never steals keyboard focus from grading keys. Explaining a card never changes its scheduling. |
| P5 | **Explain *this* card, not the topic** | Explanations are anchored to the specific fact being tested (the active cloze, the occluded region, the typed answer), not a generic encyclopedia entry. |
| P6 | **Make understanding stick** | Explanations end in a memory hook where that helps, and follow-ups offer examples, analogies, and self-checks. |
| P7 | **Transparent and in the user's control** | The user owns the key, sees the cost, and nothing is sent until they ask. Every request is for the current card only. |

---

## 2. Goals and non-goals

### 2.1 Goals (v1)

- G1. One-click or one-key **concise explanation** of the current card, streamed, with first token in ≤1.5 s at the median on the default model.
- G2. Works on **all common card types**: Basic, Basic (and reversed), Basic (type in answer), Cloze, Image Occlusion (native, Anki 23.10+), and any card containing images, tables, MathJax/LaTeX, or code.
- G3. **Follow-up conversation** per card, with one-click "learning moves" (deeper, example, simpler, analogy, mnemonic, quiz me, compare, and more) plus free-text questions.
- G4. **OpenRouter BYOK** with in-panel onboarding and key validation.
- G5. **Separate settings dialog** for reasoning effort/budget, output length, what card context is sent, shortcuts, appearance, and advanced prompt editing. It includes a model selector, populated with only the default model in v1.
- G6. **Caching and history**, so re-opening an already-explained card is instant and free.
- G7. **On-demand only**: one request per explicit user action, for the current card only, with clear loading feedback while it's generated.
- G8. A polished UI that looks native in light and dark mode.

### 2.2 Non-goals (v1)

- Mobile clients (AnkiDroid, AnkiMobile, AnkiWeb). They cannot run Python add-ons.
- **Writing anything into notes.** That includes "save explanation to card", generating new cards, and rewriting cards.
- **Background or ahead-of-time generation** (prefetching, batch-explaining a deck).
- **Models other than DeepSeek Flash Latest.** The selector exists, but adding models is future work (§6.2).
- **Provider routing or data-retention controls.** Requests use the user's own OpenRouter account settings as configured on openrouter.ai. The add-on sends no provider, routing, or data-policy parameters.
- Changing scheduling or grading automatically.
- Direct integrations with providers other than OpenRouter.
- Audio transcription of `[sound:]` content. Filenames are passed as context only.
- Any backend server, telemetry, or user accounts.

### 2.3 Future (v2+ candidates)

Additional models in the selector, saving explanations to a note field, explaining from the Browser/Previewer, per-deck prompt profiles, "weak concepts" analytics, voice read-out, and image region selection ("explain this part of the diagram").

---

## 3. Users and core scenarios

**Primary user:** a student doing daily Anki reviews (medicine, law, languages, STEM, certifications) who often meets cards they memorized but don't understand, or cards made by someone else (shared decks) that lack context.

| Scenario | Today | With Anki Explainer |
|---|---|---|
| S1. Answer revealed, still confused about *why* | Google, then AI chat, 2–5 min | `X`, read 80 words, 10–20 s |
| S2. Cloze card from a shared deck with no context | Search the whole sentence | `X`; the explanation targets the hidden term *in context* |
| S3. Image occlusion of an anatomy diagram | Find a labeled atlas online | `X`; the model sees the image with the target region outlined |
| S4. Understands the fact, wants it to *stick* | Nothing, or make a mnemonic manually | "Mnemonic" or "Real-world example" chip |
| S5. Stuck on the question side, frustrated | Flip and give up | `X` gives a **hint** that nudges recall without the answer |
| S6. Typed answer was wrong | Stare at the diff | `X` explains the specific gap between what was typed and the correct answer |
| S7. One unfamiliar word in a long card | Copy that word into Google | Select the word, right-click, then "Explain selection" |

---

## 4. User experience

### 4.1 Entry points

| Entry point | Behavior |
|---|---|
| **Keyboard: `X`** (default, rebindable) | On the answer side: open the panel and stream a concise explanation, or show the cached one. On the question side: hint flow (§4.4). If the panel is open and the current card is already explained, `X` toggles the panel closed. |
| **Bottom-bar button "Explain"** | Same as `X`. It sits in the reviewer's bottom bar between "Edit" and the answer buttons, styled like Anki's own buttons, and shows the shortcut in its tooltip. On the question side the label reads "Hint". |
| **Card context menu → "Explain this card"** | Same as `X`. |
| **Card context menu → "Explain selection"** | Appears only when text is selected. It explains the selected term or phrase *in the context of this card* (§4.7). |
| **`Shift+X`** | Opens the panel and focuses the follow-up input box. |
| **Tools menu → "Anki Explainer Settings…"**, and Add-ons → Config | Opens the settings dialog (§6). |

The shortcut is registered only in the reviewer's `review` state, so it never interferes with the editor, browser, or deck list. At startup the add-on checks the chosen key against Anki's built-in reviewer shortcuts and other add-ons' shortcuts. On a conflict it warns once and suggests an alternative.

### 4.2 The Explainer panel

**Placement:** a `QDockWidget` docked to the **right side** of the main window by default (left or floating are options). It contains its own `AnkiWebView`, so it:

- never interferes with card templates, card CSS, or card JavaScript (it isn't injected into the card),
- stays put across card transitions,
- can be resized, and its width is remembered (default 380 px, min 300, max 50% of the window),
- collapses to nothing when closed. The card regains the full width.

**Why a dock and not an overlay or popup:** an overlay covers the card the student is trying to understand, and a popup window gets lost behind Anki and needs to be managed by hand. A side dock keeps the card and explanation **visible side by side**, which is what lets the student connect the two. On narrow windows (<900 px) the panel automatically switches to a floating, always-on-top mode anchored to the right edge.

**Layout (answer side, after a concise explanation):**

```
┌──────────────────────────────────────────┐
│ ✦ Explainer          DeepSeek Flash  ⚙ ✕ │  ← header: model chip (click = quick switch), settings, close
├──────────────────────────────────────────┤
│ CARD · Cloze · Biochem::Enzymes          │  ← context strip: card type + deck (subject cue)
│ Target: "competitive inhibition"         │     shows WHAT is being explained (trust + accuracy)
├──────────────────────────────────────────┤
│                                          │
│ **A competitive inhibitor looks enough   │  ← core idea, bold, one sentence
│ like the substrate to occupy the active  │
│ site, so it competes with substrate.**   │
│                                          │
│ • Raising [S] outcompetes it, so Vmax is │  ← 2–3 "why/how" bullets
│   unchanged.                             │
│ • More substrate is needed for half-max  │
│   speed, so apparent Km rises.           │
│                                          │
│ Hook: "Competitive = Km climbs,          │  ← memory hook (when useful)
│ Vmax stays."                             │
│                                          │
├──────────────────────────────────────────┤
│ [Go deeper] [Example] [Simpler]          │  ← learning-move chips (§4.5)
│ [Analogy] [Mnemonic] [Quiz me] [More ▾]  │
├──────────────────────────────────────────┤
│ ┌──────────────────────────────────┐ ➤   │  ← follow-up input
│ │ Ask a follow-up…   (Shift+X)     │     │
│ └──────────────────────────────────┘     │
│ ⧉ Copy  ↻ Regenerate                     │  ← actions
│                     cached · ≈$0.0004    │  ← provenance + cost (toggleable)
└──────────────────────────────────────────┘
```

**Follow-ups** appear below as a conversation thread. The user's question is a compact right-aligned bubble and the model's answer is full-width prose. The thread auto-scrolls only while the user is at the bottom. If they scroll up to re-read, streaming does not yank them down; a "↓ New text" pill appears instead.

### 4.3 Panel states

| State | What the user sees | Notes |
|---|---|---|
| **No API key** (first run) | Onboarding card: one-sentence value prop, a "Get a key at openrouter.ai/keys" link, a paste field, and a **Connect** button. Validation happens inline. | No need to find a settings menu to get started. |
| **Idle** | "Press **X** to explain this card." plus a one-line tip that rotates (selection explain, Shift+X, chips). | Shown when the panel is open on a new, unexplained card. |
| **Loading** | See §4.3.1. | Appears within 100 ms of the key press. |
| **Streaming** | Text renders progressively and Markdown/MathJax render incrementally. A **Stop** button replaces the actions row. | |
| **Done** | Full explanation, chips, input, actions, footer. | |
| **Cached** | Same as Done, with a `cached` label in the footer. Rendered instantly. Shown only when the user presses Explain on a card they already explained. | ↻ Regenerate is always available. |
| **Error** | Inline, specific, actionable message with a **Retry** button (see §7.6). | Never a modal dialog. Never a raw stack trace. |
| **Card changed mid-request** | The in-flight request for the old card is cancelled (§7.5). The panel goes to Idle for the new card. | Nothing is generated for a card unless the user asks. |

#### 4.3.1 Loading feedback

Generation happens only when the user asks, so the wait between pressing `X` and the first words needs to feel responsive and honest. It typically takes 1–2 seconds and is longer with reasoning on.

- **Immediately (< 100 ms):** the panel opens, or stays open, and the Explain button in the bottom bar switches to a spinner state ("Explaining…") so the press is acknowledged even if the panel was already visible.
- **Phase label with a skeleton:** a 3-line shimmering skeleton where the text will appear, with a short status line that tracks real progress, not a fake timer:
  1. "Reading card…" while card content and images are prepared.
  2. "Asking DeepSeek…" once the request is sent.
  3. "Thinking… 3s" with a live seconds counter, only while reasoning tokens are arriving.
- **First token:** the skeleton fades out and text streams in, with a soft blinking caret at the end of the text while streaming.
- **Slow responses:** after 8 s with no text, the status adds "Still working, this model is busy" and the **Stop** button gets more prominent. Timeouts follow §7.6.
- **Reduced motion:** a static placeholder and status text with no shimmer or caret blink.

### 4.4 Question side: hint mode (protecting active recall)

Retrieval practice is what makes Anki work. Showing the explanation (and so the answer) before the student tries to recall it would quietly turn testing into re-reading, which is much less effective. So on the **question side**:

- The button reads **"Hint"** and `X` triggers a **hint**: a short nudge (≤40 words) that points toward the answer without stating it. Examples: the category it belongs to, a first-principles question to ask oneself, a related fact, or the first letter or syllable if the user asks for a stronger hint.
- The panel then offers: **[Stronger hint]** and **[Show answer + explain]**. The second option flips the card in Anki (as if Space were pressed) and then runs the normal concise explanation.
- Setting: *Question-side behavior* = `Hint (recommended)` | `Explain without revealing the answer` (explains the *question*, e.g. unfamiliar terms in the prompt, without giving the answer) | `Disabled`.

The hint prompt has a hard rule: never output the answer text or an obvious synonym. The add-on also enforces this in code: before rendering a hint, it checks that the answer string (normalized) doesn't appear. If it does, it re-requests once with a stricter instruction and falls back to a generic hint if it still fails.

### 4.5 Learning moves (follow-up chips)

Each chip is a carefully written preset prompt (§9.3). Chips are ordered by how often they help understanding. The first row is always visible and the rest sit under **More ▾**.

| Chip | Purpose (learning-science rationale) | Output shape |
|---|---|---|
| **Go deeper** | Elaboration: builds the causal "why" behind the fact. | 150–300 words, mechanism-first, with sub-headings if needed. |
| **Example** | Concrete examples make abstract ideas stick. | 1–2 vivid, realistic examples from practice, clinic, or daily life, tied back to the card. |
| **Simpler** | Lowers the level when the first explanation was too dense. | Plain language, no jargon, ≤80 words. |
| **Analogy** | Maps the concept onto something already known. | One analogy plus where it breaks down (prevents misconceptions). |
| **Mnemonic** | Retrieval cue for hard-to-memorize facts. | 1–2 mnemonic options, plus the logic of each. |
| **Quiz me** | Retrieval practice and a check that the explanation was understood. | 1 question. The student answers in the input box and the model grades briefly and corrects. |
| **Compare** *(More)* | Discrimination: separates easily confused concepts. | Small table: this concept vs 1–2 look-alikes. |
| **Common mistakes** *(More)* | Heads off typical misconceptions. | 2–3 traps and how to avoid them. |
| **Big picture** *(More)* | Connects the card to the broader topic. | Where this fits, why it matters, what it connects to. |
| **Step by step** *(More)* | For processes, calculations, derivations. | Numbered steps. |
| **Check this card** *(More)* | Shared decks contain errors. | Verdict (correct / ambiguous / likely wrong) with reasoning and an **Edit card** button. |

Chips are **context-aware**. "Step by step" is promoted on math, physics, or code cards (detected via MathJax or code blocks). "Mnemonic" is promoted on list-like answers. The chip set and order can be edited in settings.

After a chip or follow-up is sent, **keyboard focus returns to the reviewer** (unless the user is typing). Pressing 1–4 still grades immediately. This is P4 and it is non-negotiable.

### 4.6 Concise explanation format

The default explanation follows a fixed, scannable structure. Students are mid-review and need to understand in seconds:

1. **Core idea**: one bold sentence that directly resolves the card. It doesn't restate the card. It says *why* the answer is the answer.
2. **How/why**: 2–3 short bullets with the essential mechanism, reasoning, or context.
3. **Hook** *(optional)*: one line, a memory hook, contrast, or "remember it as…". Included only if genuinely useful.
4. **⚠ Card note** *(rare)*: only if the card appears wrong, outdated, or ambiguous. One line, followed by the "Check this card" chip.

Target length: **60–120 words** ("Short" setting: ≤60, "Standard": ≤120). It matches the card's own level of terminology, so it doesn't over-simplify a grad-level card or over-complicate a vocab card. Markdown, MathJax (`\( \)` / `\[ \]`), small tables, and code blocks are supported.

### 4.7 Explain selection

The student selects text on the card (a term, a formula, part of a sentence), right-clicks, and picks **"Explain selection"**, or presses `X` while text is selected. The request includes the full card as context but asks specifically: *"Explain '{selection}' as it is used on this card."* This works on both sides of the card, because explaining a word in the prompt doesn't give away the answer. If the selection *is* the answer and the card is on the question side, hint rules apply.

### 4.8 Type-in-answer cards

If the card uses `{{type:Field}}` and the student typed an answer, the add-on includes **what the student typed** alongside the correct answer. The concise explanation then explains the *specific gap* ("You wrote *metoprolol*; the card wants *propranolol*, which is the non-selective one. That's why it's used for…"). This is one of the highest-value explanations the add-on can give.

### 4.9 Struggle nudge (optional)

Setting: **Offer an explanation after repeated lapses** (off by default; threshold of 2 lapses, configurable). When a card with ≥N lapses is shown, the bottom-bar button gets a subtle accent dot and a tooltip, "You've missed this a few times. Want an explanation?". It's a visual cue only: no popup, no request until the student presses Explain (P4, P7).

### 4.10 History

A **Recent** tab in the panel header lists cards explained this session and the past 7 days: the first line of the card, deck, time, and model. Clicking one reopens its thread read-only, with **Continue** (re-opens the conversation) and **Open in Browser** (finds the card in Anki's Browser).

### 4.11 Keyboard map

| Key | Context | Action |
|---|---|---|
| `X` | Reviewer | Explain / hint / toggle panel |
| `Shift+X` | Reviewer | Open panel and focus the follow-up input |
| `Esc` | Panel focused | Return focus to reviewer (panel stays open) |
| `Esc` twice / `✕` | Panel | Close panel |
| `Enter` | Input | Send follow-up (`Shift+Enter` = newline) |
| `Ctrl+.` | Panel | Stop generation |
| `Alt+1…Alt+6` | Reviewer, panel open | Trigger chips 1–6 without leaving the keyboard |
| `1–4`, `Space` | Always | Anki's grading and flipping, never intercepted |

All bindings except `1–4`/`Space` are rebindable.

### 4.12 Visual design

- **Native look:** the panel uses Anki's CSS variables (`--canvas`, `--fg`, `--border`, `--button-bg`, and the rest) and follows `theme_manager.night_mode` live. It looks like part of Anki, not a web page bolted on.
- **Typography for reading speed:** system UI font, 15 px default (scales with a setting and with Anki's UI zoom), line height 1.55, max line length ≈ 65 characters, generous paragraph spacing. Bold is used for the core idea only, never for decoration.
- **Visual hierarchy:** the core idea is the most prominent element. Chips are quiet secondary buttons. Cost and provenance are smallest and muted.
- **Motion:** 150 ms fade-in for new content, shimmer skeleton while waiting. All of it is disabled under `prefers-reduced-motion` or the add-on's "Reduce motion" setting.
- **Accent color:** a single restrained accent (default: Anki blue, customizable) for the active chip, focus rings, and the struggle-nudge dot.
- **Accessibility:** every control is keyboard-reachable with visible focus rings, has an ARIA label, meets WCAG AA contrast in both themes, and the streaming region uses `aria-live="polite"`.

---

## 5. Card content extraction ("the card is the prompt")

The quality of an explanation depends on the model knowing **exactly what is being tested**. The extractor turns the current card into a structured context object.

### 5.1 Card context object

```python
@dataclass
class CardContext:
    card_id: int
    note_id: int
    side: Literal["question", "answer"]
    card_kind: Literal["basic", "cloze", "image_occlusion", "type_in", "unknown"]
    notetype_name: str
    deck_name: str                    # e.g. "USMLE::Step1::Pharm" — strong subject cue
    tags: list[str]                   # optional (context setting, §5.7)
    question_text: str                # cleaned, structure-preserving text
    answer_text: str | None           # None on the question side
    target: str | None                # the specific thing being tested (cloze text, IO label, …)
    typed_answer: str | None          # type-in cards only
    extra_fields: dict[str, str]      # fields not shown on the card (optional, context setting)
    images: list[ImagePart]           # base64 data, mime, role ("card_image" | "io_target")
    audio_refs: list[str]             # filenames only
    has_math: bool
    has_code: bool
    selection: str | None             # "Explain selection" text
    content_hash: str                 # sha256 of the above, for caching
```

### 5.2 Text extraction (all card types)

1. Get rendered HTML from `card.question()` / `card.answer()` (same rendering Anki shows, including template logic and conditionals).
2. On the answer side, split at `<hr id=answer>` when present. Text before it is the question and text after it is the answer. Otherwise use template-based fallback heuristics.
3. Remove `<script>`, `<style>`, and hidden elements. Turn `[sound:x.mp3]` into `audio_refs`. Drop `[anki:tts]` markup but keep its text.
4. Convert HTML to **structure-preserving plain text**: lists become `- `, tables become Markdown tables, `<br>`/`<div>` become newlines, `<b>`/`<i>` become `**`/`*`, and `<sub>`/`<sup>` are kept as `_`/`^`. MathJax/LaTeX source is kept verbatim. Code blocks are kept fenced.
5. Collapse whitespace and cap at 8,000 characters per side, truncating the middle with a marker (rarely needed).

### 5.3 Cloze cards

- Active cloze ordinal = `card.ord + 1`.
- **Question side:** the rendered `<span class="cloze">[...]</span>` (with hint if any) becomes `⟦BLANK{: hint}⟧`. Other clozes stay revealed as Anki shows them.
- **Answer side:** the active `<span class="cloze">answer</span>` becomes `⟦TARGET: answer⟧`, and `target = answer`.
- Multiple deletions sharing the same ordinal are all marked, and `target` joins them with " / ".
- The prompt explicitly tells the model that the target is what's being tested, and to explain it in the context of the sentence.

### 5.4 Image Occlusion (native, Anki 23.10+)

Detected by the notetype's `originalStockKind` being Image Occlusion, or by the presence of the `Image` and `Occlusion` fields.

1. Load the image from the `Image` field (`<img src>` resolved against `col.media.dir()`).
2. Parse the `Occlusion` field's shape list (`image-occlusion:rect|ellipse|polygon|text:…` with normalized coordinates and an ordinal). Implementation must check the exact syntax against the targeted Anki versions and cover it with fixture tests.
3. Render a **target-annotated copy** of the image with `QPainter`:
   - Active shape(s) (ordinal = `card.ord + 1`): thick high-contrast outline (magenta, 3–4 px at the rendered scale) and a small "TARGET" label.
   - Other shapes: thin gray outline (gives the layout context without clutter).
   - On the question side, fill the active shape with a solid color so the answer is hidden from the hint, matching what the student sees.
4. Send the annotated image and the `Header`, `Back Extra`, and `Comments` fields as text. Text-type occlusion shapes contribute their text.
5. The prompt says: *"The outlined TARGET region is what this card tests. Identify what is in that region and explain it."*

Legacy *Image Occlusion Enhanced* add-on notes (SVG masks) get best-effort support: send the original image plus the question-mask SVG rasterized over it.

### 5.5 Images on any card

- Collect `<img>` from the rendered question and answer (answer only on the answer side).
- Resolve against the media folder. Skip remote URLs unless "Allow remote images" is on.
- Downscale so the long side is ≤1568 px. Re-encode PNG as PNG if it has transparency or is small, otherwise JPEG at quality 85. Strip EXIF.
- Cap at **4 images per request** (configurable 0–8). The prompt mentions any skipped images.
- SVG is rasterized via `QSvgRenderer`. GIFs use the first frame.

### 5.6 Model capability handling

The default model (DeepSeek Flash Latest) accepts images, so v1 always sends them. Each model registry entry (§6.2) still carries a `vision` flag. If a text-only model is added later, image cards on that model send text only and show a small inline note: "This model can't see images. Explanation is based on text only."

### 5.7 Context controls

Settings (all on by default except where noted): **Send images**, **Include deck name**, **Include tags** (off), **Include fields not shown on the card** (off), **Include typed answers**. These control *what goes into the prompt* to improve or trim context. They are not data-retention controls (§2.2). The context strip at the top of the panel always shows what kind of content was sent.

---

## 6. Settings dialog

A native Qt dialog (`QDialog` with a left-hand page list), opened from **Tools → Anki Explainer Settings…**, the panel's ⚙ button, or Anki's Add-ons → Config button (`addonManager.setConfigAction`). Changes apply immediately on **Save**. Every page has "Reset page to defaults".

### 6.1 Account

- **OpenRouter API key**: masked field, show/hide toggle, **Test connection** button. A successful test shows key label, credit remaining, and rate limit from `GET /api/v1/key`. Error states give specific guidance.
- Link: "Get a key" → `https://openrouter.ai/keys`. Link: "Add credits" → `https://openrouter.ai/settings/credits`.
- **Remove key** button.

### 6.2 Model

- **Model selector**: a dropdown in settings, also reachable from the model chip in the panel header. In v1 it contains exactly one entry:

  | Display name | OpenRouter ID | Capabilities |
  |---|---|---|
  | **DeepSeek Flash (latest)** — default | `~deepseek/deepseek-flash-latest` | text + image input, reasoning |

  The ID is OpenRouter's alias that always routes to the newest DeepSeek Flash release. Verified 2026-10-19: it currently resolves to DeepSeek V4.1 Flash `1017`. Upgrades happen automatically with no add-on update needed.

- **Built to grow:** models come from a small static registry in code (`llm/models.py`). Each entry is `{id, display_name, vision: bool, reasoning: bool, default_max_tokens}`. Adding a model later means adding one entry. The selector, capability checks (§5.6), and reasoning controls (§6.3) read from the registry, so no UI changes are needed. There's no live catalog fetching, model search, pricing display, or fallback chains in v1.

### 6.3 Reasoning

Reasoning improves hard explanations (multi-step physiology, math derivations) but adds latency. Defaults are set **per mode**, so first clicks stay instant:

| Mode | Default reasoning | Rationale |
|---|---|---|
| Hint | Off (`effort: "none"`) | Must be instant, and hints are simple. |
| Concise explanation | **Minimal** | Fast first token, with a bit of self-check against wrong explanations. |
| Follow-ups and chips | **Low** | A bit more care for depth. |
| Go deeper / Check this card / Quiz grading | **Medium** | Accuracy matters most here. |

Controls:
- **Reasoning effort per mode**: `Off`, `Minimal`, `Low`, `Medium`, `High`, `Max`, mapped to OpenRouter `reasoning.effort` (`none|minimal|low|medium|high|max`).
- **Or a token budget** *(advanced)*: integer `reasoning.max_tokens`, mutually exclusive with effort per mode (OpenRouter rejects both together).
- **Show reasoning**: `Hidden` (default; shows a "Thinking… Ns" indicator only) | `Collapsible` (a collapsed "Thinking" section above the answer) | `Off` (sends `reasoning.exclude: true`. Reasoning tokens are still billed, and the dialog says so.)
- If the selected model doesn't support reasoning (per its registry entry), the controls are disabled with a tooltip explaining why. This can't happen in v1.

### 6.4 Output

- **Concise length**: `Short` (≤60 words) | `Standard` (≤120, default).
- **Detail level of the student**: `Intro` | `Undergraduate` (default) | `Graduate/Professional`. This shifts vocabulary and assumed background.
- **What I'm studying** (free text, optional; e.g. "USMLE Step 1", "JLPT N3 Japanese", "AWS SA Associate"). Injected into the system prompt, it greatly improves relevance.
- **Explanation language**: `Same as card` (default) | any language (e.g. explain Spanish vocab cards in English).
- **Include memory hook**: On (default) / Off.
- **Temperature**: default 0.3 (explanations should be stable and accurate). Range 0–1.
- **Max output tokens per mode** *(advanced)*: hint 300, concise 1,200, follow-up 2,500, deep 4,000. These include reasoning tokens on models that count them together.
- **Stream responses**: On (default).

### 6.5 Behavior

- Question-side behavior: `Hint` (default) | `Explain without revealing` | `Disabled`.
- **Auto-open panel on answer reveal**: Off (default) | On (opens the panel in its Idle state; it never requests anything until Explain is pressed).
- Struggle nudge (§4.9).
- **Panel position**: Right (default) | Left | Floating. **Panel font size.** **Reduce motion.**
- **Show cost in footer**: On (default).

### 6.6 Shortcuts

Rebind every non-Anki key in §4.11, with live conflict detection.

### 6.7 Card context and cache

- Toggles from §5.7.
- **Cache explanations locally**: On (default). **Clear cache** button with a size readout.
- Plain-language statement: "When you press Explain, this card's content is sent to OpenRouter using your account's settings. Nothing is sent before you press Explain, and nothing is sent for other cards."
- Provider routing and data-retention preferences are **not** configured in the add-on. They come from the user's OpenRouter account settings on openrouter.ai, and the dialog links there.

### 6.8 Prompts (advanced)

Editable system prompt and per-chip prompts, with `{{variables}}` documented (e.g. `{{studying}}`, `{{level}}`, `{{length_words}}`). Each has a **Reset to default** button. User edits survive add-on updates. A "Prompt version" note warns when defaults have changed upstream.

### 6.9 Usage

This session's and this month's request count, tokens, and spend (from response `usage.cost`). Optional **monthly soft cap** (e.g. $2.00): when reached, the panel asks for confirmation before each request.

---

## 7. AI integration (OpenRouter)

### 7.1 Endpoint and request

`POST https://openrouter.ai/api/v1/chat/completions`

Headers:
```
Authorization: Bearer <user key>
Content-Type: application/json
HTTP-Referer: https://github.com/<owner>/anki-explainer     # app attribution
X-Title: Anki Explainer
```

Body (concise explanation, answer side, image card):
```json
{
  "model": "~deepseek/deepseek-flash-latest",
  "stream": true,
  "temperature": 0.3,
  "max_tokens": 1200,
  "reasoning": { "effort": "minimal" },
  "usage": { "include": true },
  "messages": [
    { "role": "system", "content": "<system prompt §9.1>" },
    { "role": "user", "content": [
        { "type": "text", "text": "<card context block §9.2>\n\n<mode instruction: concise>" },
        { "type": "image_url", "image_url": { "url": "data:image/jpeg;base64,..." } }
    ]}
  ]
}
```
No `provider`, `models`, or routing fields are sent. OpenRouter applies the user's account-level preferences (provider ordering, data policy, and so on) exactly as configured on openrouter.ai.

### 7.2 Streaming

- Parse SSE line by line. Ignore comment lines (`: OPENROUTER PROCESSING`). Stop at `data: [DONE]`.
- Accumulate `choices[0].delta.content` into the answer. Accumulate `delta.reasoning` / `delta.reasoning_details` into the thinking buffer (shown per the §6.3 setting, and used for the "Thinking… Ns" indicator).
- A chunk containing a top-level `error` mid-stream ends the stream with that error and keeps any partial text, marked "(incomplete)".
- The final chunk's `usage` (tokens and `cost`) feeds the footer and Usage page. The resolved `model` (the alias target) is stored with the cache entry.
- Deltas are **coalesced on a ~33 ms timer** before being pushed to the webview, so there's one `eval` per frame rather than per token.

### 7.3 Model registry

Static, in code (§6.2). No network call is made to list models.

### 7.4 Multi-turn

- One **thread per card per (side, selection)**. The thread holds system, the first user turn with card context and images, then alternating turns.
- Follow-ups reuse the full thread. Images are sent only in the first user turn and are referenced afterwards. Optionally, images can be dropped from turns after 6 to save tokens (default on).
- Assistant turns are stored with their `reasoning_details` and passed back unmodified, as OpenRouter recommends for reasoning continuity.
- Thread token estimate is capped at 60k. If exceeded (very rare), the oldest follow-up pairs are summarized.

### 7.5 On-demand requests and cancellation

- **Every request comes from an explicit user action** (Explain/Hint, a chip, a follow-up, Regenerate, Retry) and covers **only the card on screen**. There is no prefetching, no batch generation, and no request on card show or answer reveal.
- **At most one active request at a time.** Starting a new one (for example, clicking a chip mid-stream) cancels the current one first.
- **Stop** closes the HTTP response. Partial text is kept and marked "(stopped)".
- **Moving to another card cancels the in-flight request** for the old card. Any partial text is discarded and not cached, so the cache only ever holds complete explanations the user asked for.

### 7.6 Errors (user-facing copy)

| Condition | Message | Action |
|---|---|---|
| No key | Onboarding card (§4.3) | Paste key |
| 401 | "OpenRouter didn't accept this API key." | **Open settings** |
| 402 | "Your OpenRouter credits have run out." | **Add credits** (link) |
| 403 (moderation) | "The model provider declined this request." | **Retry** |
| 404 / no available provider | "OpenRouter couldn't route this request. Check your OpenRouter provider settings." | **Open OpenRouter settings** (link) |
| 408 / timeout (connect 10 s, first-byte 30 s, idle 60 s) | "The model took too long to respond." | **Retry** |
| 429 | "Rate limited. Retrying in N s…" | Auto-retry with backoff (1 s, 2 s, 4 s; max 3), then **Retry** |
| 5xx / 502 provider | "The provider had a problem." | **Retry** |
| Network offline | "You appear to be offline." | **Retry**; cached explanations still work |

Errors never block grading. They're logged (without card content or key) to the add-on's log for bug reports.

---

## 8. Architecture

### 8.1 Project layout

```
anki_explainer/                    # repo root
├─ spec.md
├─ README.md
├─ addon/                          # what ships in the .ankiaddon
│  ├─ __init__.py                  # entry point: version check, register hooks
│  ├─ manifest.json                # name, package, min_point_version, human_version
│  ├─ config.json                  # defaults (non-secret)
│  ├─ config.md                    # shown in Anki's config editor; points to Settings dialog
│  ├─ explainer/
│  │  ├─ hooks.py                  # all gui_hooks registration in one place
│  │  ├─ controller.py             # orchestrates: card → context → request → panel
│  │  ├─ context/
│  │  │  ├─ extractor.py           # CardContext builder (§5)
│  │  │  ├─ html_to_text.py
│  │  │  ├─ cloze.py
│  │  │  ├─ image_occlusion.py     # shape parsing + QPainter annotation
│  │  │  └─ images.py              # media resolution, resize, encode
│  │  ├─ llm/
│  │  │  ├─ openrouter.py          # HTTP client, SSE parser, errors
│  │  │  ├─ models.py              # static model registry (v1: DeepSeek Flash Latest)
│  │  │  ├─ prompts.py             # templates + rendering
│  │  │  └─ threads.py             # conversation state
│  │  ├─ store/
│  │  │  ├─ cache.py               # SQLite cache + history
│  │  │  ├─ settings.py            # typed settings over addon config
│  │  │  └─ secrets.py             # API key storage
│  │  ├─ ui/
│  │  │  ├─ panel.py               # QDockWidget + AnkiWebView, bridge
│  │  │  ├─ reviewer_button.py     # bottom-bar button + shortcut + context menu
│  │  │  └─ settings_dialog.py
│  │  └─ util/log.py
│  ├─ web/
│  │  ├─ panel.html
│  │  ├─ panel.css
│  │  ├─ panel.js                  # rendering, chips, input, state machine
│  │  └─ vendor/
│  │     ├─ marked.min.js          # Markdown
│  │     └─ purify.min.js          # DOMPurify sanitization
│  └─ user_files/                  # preserved across updates (key, cache db)
├─ tests/
│  ├─ fixtures/                    # sample notes: basic, cloze, IO, type-in, images, MathJax
│  ├─ test_extractor.py
│  ├─ test_cloze.py
│  ├─ test_image_occlusion.py
│  ├─ test_sse_parser.py
│  └─ test_prompts.py
└─ scripts/
   └─ build_addon.py               # zips addon/ → anki_explainer.ankiaddon
```

### 8.2 Anki integration points

| Need | API |
|---|---|
| Create panel after main window ready | `gui_hooks.main_window_did_init` |
| Track current card / side | `gui_hooks.reviewer_did_show_question(card)`, `reviewer_did_show_answer(card)` |
| Leave reviewer | `gui_hooks.reviewer_will_end` (panel goes to idle; stays docked if open) |
| Shortcut | `gui_hooks.state_shortcuts_will_change(state, shortcuts)` for `state == "review"` |
| Bottom-bar button | `gui_hooks.webview_will_set_content` (context `ReviewerBottomBar`) to inject CSS/JS; click → `pycmd("explainer:explain")` |
| JS → Python messages | `gui_hooks.webview_did_receive_js_message` (prefix `explainer:`; return `(True, result)`) |
| Context menu | `gui_hooks.reviewer_will_show_context_menu(reviewer, menu)`; selection via `reviewer.web.selectedText()` |
| Flip card for "Show answer + explain" | `mw.reviewer._showAnswer()` (guarded; falls back to sending the Space action) |
| Typed answer | `mw.reviewer.typedAnswer` |
| Lapses (struggle nudge) | `card.lapses` |
| Serve web assets | `mw.addonManager.setWebExports(__name__, r"web/.*\.(js|css|html|svg|png)")` |
| Config | `mw.addonManager.getConfig/writeConfig`, `setConfigAction` |
| Background work (the HTTP stream for the user's request only) | `mw.taskman.run_in_background` / `QueryOp`; UI updates via `mw.taskman.run_on_main` |
| Theme | `aqt.theme.theme_manager.night_mode`, `gui_hooks.theme_did_change` |

All calls to private reviewer APIs (`_showAnswer`) are wrapped with feature checks and fallbacks, so an Anki update can't break review.

### 8.3 Data flow

```
X pressed (the only trigger; nothing runs on card show)
  └─ controller.explain(card, side, selection?)
       ├─ cache.lookup(content_hash, mode, model, prompt_version) ──hit──▶ panel.render(cached)
       └─ miss:
            panel.setState("loading", phase="reading")          # < 100 ms, §4.3.1
            extractor.build(card) ─▶ CardContext
            prompts.render(mode, ctx, settings) ─▶ messages
            panel.setState("loading", phase="asking")
            openrouter.stream(messages, params)  [background thread]
                 ├─ reasoning deltas ─▶ panel.setState("loading", phase="thinking", seconds)
                 └─ content deltas ─(coalesce 33ms)─▶ run_on_main ─▶ panel.append(delta)
            on done: cache.store(...), usage.record(...)
            on card change: cancel stream, discard partial
```

### 8.4 Panel bridge protocol

Python → JS (`panel.web.eval`): `Explainer.setState(stateJson)`, `Explainer.appendDelta(threadId, text, kind)`, `Explainer.finish(threadId, metaJson)`, `Explainer.error(threadId, errJson)`, `Explainer.setTheme(isDark)`.

JS → Python (`pycmd("explainer:" + JSON)`): `{"op":"chip","id":"example"}`, `{"op":"ask","text":"…"}`, `{"op":"stop"}`, `{"op":"copy"}`, `{"op":"regenerate"}`, `{"op":"open_settings"}`, `{"op":"set_key","key":"…"}`, `{"op":"focus_reviewer"}`, `{"op":"history_open","id":…}`.

All payloads are JSON-escaped. Model output is rendered via marked and then **sanitized with DOMPurify** (no scripts, no event handlers, no remote images), and then MathJax typesets the result.

### 8.5 Storage

- **Settings:** Anki add-on config (`meta.json` via `writeConfig`), **excluding the API key**.
- **API key:** `user_files/secret.json` with the file mode set to user-only where the OS supports it. Never written to config, logs, cache, or error reports. It's never sent anywhere except `openrouter.ai` over HTTPS. If the OS keyring is available (`keyring` importable), it's used instead (optional, with a graceful fallback). The README notes that the key lives on this computer and doesn't sync via AnkiWeb.
- **Cache and history:** SQLite `user_files/cache.sqlite3`:
  - `explanations(id, card_id, note_id, content_hash, side, mode, selection_hash, model_requested, model_resolved, prompt_version, created_at, text, reasoning, usage_json)`
  - `threads(id, card_id, created_at, updated_at, messages_json)`
  - Cache key = `(content_hash, side, mode, selection_hash, model_requested, prompt_version)`. When the card is edited, `content_hash` changes and the old cache entry naturally stops matching.
  - Default retention 90 days / 50 MB, then LRU eviction.
- **Usage:** `usage(month, requests, prompt_tokens, completion_tokens, cost_usd)`.

### 8.6 Dependencies

Uses Anki's bundled Python environment only: `requests` (bundled with Anki) for HTTP streaming, PyQt6 for UI and image work, and `sqlite3`/`json`/`hashlib` from the stdlib. Vendored JS: `marked` and `DOMPurify` (MIT/Apache-2.0, minified, license files included). No pip installs.

### 8.7 Performance budgets

| Metric | Budget |
|---|---|
| Add-on import time at Anki startup | < 50 ms (lazy-import heavy modules) |
| Keypress → panel visible | < 100 ms |
| Keypress → request sent (text card) | < 150 ms |
| Keypress → request sent (image card, incl. resize/encode) | < 400 ms |
| First token (default model, p50) | ≤ 1.5 s (network-dependent; tracked, not guaranteed) |
| Cached explanation render | < 100 ms |
| Reviewer frame drops during streaming | none (main thread never blocks on I/O) |

---

## 9. Prompt design

Prompts live in `llm/prompts.py` as versioned templates (`PROMPT_VERSION = "1"`). Changing a default prompt bumps the version, which invalidates matching cache entries.

### 9.1 System prompt (default)

```
You are a world-class tutor embedded inside Anki, helping a student who is in
the middle of a flashcard review session. Your job is to make the card make
sense FAST, so the student understands it and remembers it.

Student profile: studying {{studying|"not specified"}}; level: {{level}}.
Explain in {{language}}.

Rules:
- Explain the specific fact this card tests (the TARGET), in the context of
  the card. Do not write a generic encyclopedia entry about the topic.
- Lead with the single most clarifying sentence. Explain WHY, not just WHAT.
  Never just restate the card.
- Match the card's terminology level. Define a term only if the card likely
  assumes it and the student may not know it.
- Be accurate. If you are not sure, say so briefly. Never invent citations,
  numbers, or names.
- If the card itself looks wrong, outdated, or ambiguous, add one line
  starting with "⚠ Card note:" and explain briefly.
- Format in Markdown. Use \( \) for inline math and \[ \] for display math.
  No headings in short answers. No preamble ("Great question", "Sure!").
- Images: if an image region is outlined and labeled TARGET, that region is
  what the card tests.
```

### 9.2 Card context block

```
<card>
type: {{card_kind}} | note type: {{notetype}} | deck: {{deck}}
side shown to student: {{side}}
QUESTION:
{{question_text}}
ANSWER:
{{answer_text}}
TARGET (what is being tested): {{target}}
STUDENT TYPED: {{typed_answer}}
EXTRA FIELDS: {{extra_fields}}
IMAGES: {{n}} attached ({{roles}}); AUDIO: {{audio_refs}}
SELECTED TEXT TO EXPLAIN: {{selection}}
</card>
```
Lines with empty values are omitted entirely.

### 9.3 Mode instructions

- **Concise:** "Explain this card in at most {{length_words}} words. Format: one bold sentence with the core idea (why the answer is the answer); then 2–3 short bullets on the mechanism/reasoning; then, only if genuinely helpful, one line starting with 'Hook:' giving a memory hook. If STUDENT TYPED is present and wrong, focus on the gap between their answer and the correct one."
- **Hint (question side):** "Give a hint of at most 40 words that helps the student recall the answer themselves. NEVER state the answer, a synonym, or a translation of it. Point toward the reasoning, category, or a related cue." **Stronger hint:** same, plus "may reveal the first letter/syllable or narrow it to two options."
- **Explain without revealing:** "Explain any concepts or terms in the QUESTION the student may not know, so they can attempt it. Do not reveal or hint at the answer."
- **Go deeper:** "Give a deeper explanation (150–300 words): underlying mechanism, why it works this way, and how it connects to related concepts. Use short sections or bullets."
- **Example:** "Give 1–2 concrete, realistic examples (clinical, real-world, or everyday, as fits the subject) that show this fact in action. Tie each back to the card in one sentence."
- **Simpler:** "Re-explain in plain language for someone new to the subject, at most 80 words, no jargon."
- **Analogy:** "Give one strong analogy, then one sentence on where the analogy breaks down."
- **Mnemonic:** "Offer 1–2 memorable mnemonics for this card, each with a one-line explanation of how it maps to the fact."
- **Quiz me:** "Ask ONE short question that tests understanding of the concept (not the exact card wording). Do not give the answer. Wait for the student's reply." **(Grading turn):** "Grade the student's answer briefly: correct / partly / incorrect, then the key correction in ≤60 words."
- **Compare:** "Compare this concept with the 1–2 concepts students most often confuse it with, as a compact Markdown table, then one line on how to tell them apart."
- **Common mistakes:** "List 2–3 common misconceptions or exam traps related to this card and how to avoid each."
- **Big picture:** "In ≤150 words: where this fact fits in the larger topic, why it matters, and what it connects to."
- **Step by step:** "Walk through this step by step, numbered, showing reasoning for each step."
- **Check this card:** "Assess whether the card is correct, complete, and unambiguous. Start with one word verdict: Correct / Ambiguous / Likely wrong. Then explain briefly and suggest a corrected version if needed."

### 9.4 Prompt quality bar (eval)

Ship a small offline eval set: ~40 real cards across medicine, languages, STEM, law, and programming, including clozes, image occlusion, type-in, and deliberately wrong cards. Score each prompt version manually or with an LLM judge on:
1. targets the tested fact,
2. accurate,
3. within length,
4. explains why (doesn't restate),
5. hint never leaks the answer,
6. flags bad cards.

A prompt change ships only if it doesn't regress any criterion.

---

## 10. Learning-science rationale (why the design looks like this)

| Design choice | Grounding |
|---|---|
| Hint-first on the question side | **Retrieval practice / testing effect**: the effort of recall is what strengthens memory. Showing the answer early turns testing into passive restudy. |
| Concise first, depth on demand | **Cognitive load**: mid-review, working memory is busy. A short, structured answer resolves the confusion without flooding it. |
| "Why" over "what" | **Elaborative interrogation**: understanding *why* a fact is true makes it far easier to retain than the bare fact. |
| Example / Analogy chips | **Concreteness and analogical encoding**: link new abstract knowledge to familiar, concrete schemas. |
| Compare / Common mistakes | **Discrimination learning**: many errors come from confusing similar concepts. Contrasting them directly reduces interference. |
| Quiz me | **Generation and self-explanation**: answering in your own words exposes the illusion of understanding. |
| Memory hook / Mnemonic | **Encoding cues** for facts that are arbitrary and hard to derive. |
| On demand only, with honest loading feedback | **Agency and attention**: the student asks only when confused, and the visible progress states keep them oriented instead of wondering whether anything happened. |
| Never altering scheduling; focus returns to grading | **Honest self-grading** stays the student's call. The tool supports the review loop instead of replacing it. |

---

## 11. Security and privacy

- The API key is stored locally, never logged, and sent only to `https://openrouter.ai` over TLS. The settings dialog never shows the full key after save, only `sk-or-…last4`.
- No telemetry. The only network calls are (a) a chat request when the user explicitly asks for one, and (b) `GET /api/v1/key` when the user presses **Test connection**.
- The add-on never writes to notes, cards, or note types. It is read-only toward the collection.
- All model output is sanitized (DOMPurify) before rendering, which blocks prompt-injected HTML/JS from cards or model output. Links open in the system browser via `openLink`, never inside the panel.
- Card content may contain prompt-injection text (from shared decks). The system prompt treats `<card>` content as data. The add-on has no tools or actions the model can trigger, so injection impact is limited to the explanation text.

---

## 12. Compatibility and packaging

- `manifest.json`: `"min_point_version": 250200` (Anki 25.02). Tested on the latest 26.09.x and the previous two minor releases, on Windows 11, macOS 14+, and Ubuntu 24.04.
- Qt6 only. No Qt5 shims.
- Compatible with common add-ons: Review Heatmap, AnKing note types, Image Occlusion Enhanced (legacy), Speed Focus Mode, and custom reviewer themes. The panel is a separate webview, so card-styling add-ons don't affect it.
- Distributed as `anki_explainer.ankiaddon` (built by `scripts/build_addon.py`) and on AnkiWeb, with screenshots and a 30-second GIF.

---

## 13. Testing

- **Unit tests (pytest, outside Anki where possible):** HTML-to-text, cloze target marking (single, multiple same-ordinal, hints, nested formatting), IO shape parsing and annotation geometry, image encoding limits, SSE parser (comments, mid-stream errors, `[DONE]`, split lines), prompt rendering (empty-field omission), cache keying, hint-leak guard.
- **Integration (in Anki, scripted with a test profile):** card types × sides × light/dark × reasoning effort levels on the default model.
- **On-demand guarantee:** an automated check that a full review session with the panel open (and auto-open on) makes **zero** network requests until Explain is pressed, and exactly one per press.
- **Loading feedback:** each phase of §4.3.1 is shown in order, and the slow-response message appears after 8 s (simulated slow server).
- **Manual QA matrix:** each scenario S1–S7 from §3. Keyboard-only full session. 200-card review session with the panel open (check no slowdowns or memory growth). Offline mode. Invalid key. Zero credits. Rate limit (simulated).
- **Prompt evals:** §9.4 before every prompt change.

---

## 14. Milestones

| Milestone | Scope | Exit criteria |
|---|---|---|
| **M0 · Skeleton** | Add-on loads, dock panel, theme, shortcut, bottom-bar button, settings dialog shell, key storage + test. | Panel opens and closes with `X` on a review screen in light and dark. |
| **M1 · Concise explain** | Text extraction (basic/reversed), OpenRouter streaming client, concise prompt, full loading feedback (§4.3.1), Markdown + MathJax rendering, errors, Stop, cancel on card change. | S1 works end to end with first token ≤1.5 s p50 on the default model. |
| **M2 · All card types** | Cloze targeting, type-in, images, native IO annotation. | S2, S3, S6 pass on the fixture deck. |
| **M3 · Conversation** | Threads, chips, free-text follow-ups, focus management, Explain selection, Quiz me. | S4, S7 pass. Grading keys work after any chip. |
| **M4 · Recall protection** | Hint mode, stronger hint, show-answer-and-explain, leak guard. | S5 passes. Leak guard catches seeded leaks in tests. |
| **M5 · Settings depth** | Model selector + registry (one entry), reasoning per mode, output/behavior/context pages, prompt editor, usage page. | Every setting in §6 works and persists. |
| **M6 · Memory** | Cache, history tab, struggle nudge. | Cached re-open < 100 ms. |
| **M7 · Polish and release** | Accessibility pass, reduced motion, performance budgets, README, AnkiWeb listing, eval set green. | All §8.7 budgets met. QA matrix green. |

---

## 15. Success metrics (local, private, shown on the Usage page only)

- **Time to understanding:** median time from `X` to the next grade press (goal: < 30 s, vs minutes with Google/AI chat).
- **Depth rate:** % of explanations followed by a chip or follow-up. Very high could mean concise answers are too thin, so this tunes the prompts.
- **Hint resolution:** % of hints after which the student revealed the answer with "Good"/"Easy" (evidence that hints preserve recall).
- **Cost per explanation:** median USD (goal: < $0.001 on the default model).

None of these leave the user's machine.

---

## 16. Decisions log

| # | Decision | Date |
|---|---|---|
| D1 | Default shortcut is **`X`** (rebindable, with conflict warning). | 2026-10-19 |
| D2 | **Strictly on-demand generation.** One request per explicit action, for the current card only. No prefetching or background generation. The wait is covered by clear loading feedback (§4.3.1). In-flight requests are cancelled when the card changes. | 2026-10-19 |
| D3 | Model is **`~deepseek/deepseek-flash-latest`** (always the newest DeepSeek Flash). A model selector exists, backed by a static registry, but v1 ships with this single model. Other models are future work. | 2026-10-19 |
| D4 | **No writing to notes.** "Save to card" (and note tagging) are out of scope. | 2026-10-19 |
| D5 | **No provider-routing or data-retention controls** in the add-on. Requests rely on the user's OpenRouter account settings. | 2026-10-19 |

---

## Appendix A — Default `config.json`

```json
{
  "model": "~deepseek/deepseek-flash-latest",
  "reasoning": {
    "hint":     { "effort": "none" },
    "concise":  { "effort": "minimal" },
    "followup": { "effort": "low" },
    "deep":     { "effort": "medium" },
    "display": "hidden"
  },
  "output": {
    "concise_length": "standard",
    "level": "undergraduate",
    "studying": "",
    "language": "same_as_card",
    "memory_hook": true,
    "temperature": 0.3,
    "max_tokens": { "hint": 300, "concise": 1200, "followup": 2500, "deep": 4000 },
    "stream": true
  },
  "behavior": {
    "question_side": "hint",
    "auto_open_on_answer": false,
    "struggle_nudge": false,
    "struggle_nudge_lapses": 2,
    "panel_position": "right",
    "panel_width": 380,
    "font_size": 15,
    "reduce_motion": false,
    "show_cost": true
  },
  "shortcuts": {
    "explain": "X",
    "focus_input": "Shift+X",
    "stop": "Ctrl+.",
    "chips": ["Alt+1", "Alt+2", "Alt+3", "Alt+4", "Alt+5", "Alt+6"]
  },
  "context": {
    "send_images": true,
    "max_images": 4,
    "allow_remote_images": false,
    "include_deck": true,
    "include_tags": false,
    "include_hidden_fields": false,
    "include_typed_answer": true,
    "cache_enabled": true,
    "cache_retention_days": 90
  },
  "chips": ["deeper", "example", "simpler", "analogy", "mnemonic", "quiz", "compare", "mistakes", "big_picture", "steps", "check"],
  "usage": { "monthly_soft_cap_usd": null },
  "prompts": { "overrides": {} }
}
```

## Appendix B — Glossary

- **BYOK**: bring your own key. The user supplies their own OpenRouter API key and pays OpenRouter directly.
- **Alias model** (`~vendor/...-latest`): an OpenRouter ID that always routes to the newest model in a family.
- **Target**: the specific fact a card tests (active cloze, outlined IO region, typed-answer gap, or selection).
- **Learning move / chip**: a one-click follow-up preset (Example, Analogy, …).
- **On demand**: an explanation is generated only when the user explicitly asks, and only for the card on screen.
