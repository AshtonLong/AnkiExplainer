/* Anki Explainer panel: rendering, chips, input, state machine.
 *
 * Python → JS:  Explainer.setState(state), Explainer.setPhase(threadId, phase, info),
 *               Explainer.appendDelta(threadId, text, kind), Explainer.finish(threadId, meta),
 *               Explainer.error(threadId, err), Explainer.setTheme(isDark), Explainer.focusInput()
 * JS → Python:  pycmd("explainer:" + JSON.stringify({op, ...}))
 *
 * All model output goes through marked → DOMPurify before it touches the DOM;
 * MathJax then typesets the sanitized result.
 */
(function () {
  "use strict";

  // Model output must never make the panel load anything: no media tags and
  // no URL-bearing attributes except plain link hrefs (opened externally on click).
  const PURIFY = {
    FORBID_TAGS: ["img", "picture", "source", "track", "image", "style", "form", "input", "button",
      "textarea", "select", "iframe", "frame", "frameset", "object", "embed", "video", "audio",
      "svg", "math", "link", "meta", "base", "portal"],
    FORBID_ATTR: ["style", "srcset", "src", "action", "formaction", "background", "poster",
      "xlink:href", "ping", "lowsrc", "dynsrc", "longdesc", "cite", "codebase", "data", "manifest"],
    ALLOW_DATA_ATTR: false,
  };

  const PHASES = {
    reading: "Reading card…",
    asking: "Asking {model}…",
    thinking: "Thinking… {s}s",
    retrying: "Rate limited. Retrying in {n} s…",
  };

  let state = null;
  let mathPromise = null;
  let mathQueue = Promise.resolve();
  const stream = { threadId: null, turnId: null, text: "", reasoning: "", stableLen: 0, phase: null,
    phaseInfo: {}, phaseAt: 0, thinkingAt: 0, timer: null };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const el = (tag, attrs, children) => {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "html") node.innerHTML = v;
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const c of [].concat(children || [])) {
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  };

  // Anki defines pycmd asynchronously once its web channel is up, so early
  // messages are queued and flushed as soon as it exists.
  const outbox = [];
  let outboxTimer = null;

  function send(op, data) {
    const msg = "explainer:" + JSON.stringify(Object.assign({ op: op }, data || {}));
    if (typeof pycmd === "function" && !outbox.length) {
      pycmd(msg);
      return;
    }
    outbox.push(msg);
    if (outboxTimer) return;
    outboxTimer = setInterval(() => {
      if (typeof pycmd !== "function") return;
      clearInterval(outboxTimer);
      outboxTimer = null;
      outbox.splice(0).forEach((m) => pycmd(m));
    }, 30);
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fmt(template, vars) {
    return template.replace(/\{(\w+)\}/g, (_, k) => (vars[k] !== undefined ? vars[k] : ""));
  }

  /* ------------------------------------------------------------------ */
  /* Markdown + math                                                     */
  /* ------------------------------------------------------------------ */
  const MATH_RE = /\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$\$[\s\S]+?\$\$/g;
  const CODE_RE = /(```[\s\S]*?(?:```|$)|`[^`\n]+`)/g;

  function protectMath(src, store) {
    return src.split(CODE_RE).map((part, i) => {
      if (i % 2 === 1) return part; // code: leave untouched
      return part.replace(MATH_RE, (m) => {
        store.push(m);
        return "ZXMATH" + (store.length - 1) + "XZ";
      });
    }).join("");
  }

  function renderMarkdown(src) {
    const maths = [];
    const protectedSrc = protectMath(src || "", maths);
    let html;
    try {
      html = window.marked ? window.marked.parse(protectedSrc, { gfm: true, breaks: false, async: false }) : escapeHtml(protectedSrc);
    } catch (e) {
      html = "<p>" + escapeHtml(protectedSrc) + "</p>";
    }
    html = window.DOMPurify ? window.DOMPurify.sanitize(html, PURIFY) : escapeHtml(html);
    return html.replace(/ZXMATH(\d+)XZ/g, (_, i) => escapeHtml(maths[+i]));
  }

  function hasMath(text) {
    return /\\\(|\\\[|\$\$/.test(text || "");
  }

  function loadMathJax() {
    if (mathPromise) return mathPromise;
    mathPromise = new Promise((resolve) => {
      window.MathJax = {
        tex: {
          inlineMath: [["\\(", "\\)"]],
          displayMath: [["\\[", "\\]"], ["$$", "$$"]],
          processEscapes: false,
          // No \href/\class/\style: model output must not create links or styles.
          packages: { "[-]": ["html"] },
        },
        options: { enableMenu: false },
        startup: {
          typeset: false,
          ready: () => {
            window.MathJax.startup.defaultReady();
            resolve(window.MathJax);
          },
        },
        loader: { paths: { mathjax: "/_anki/js/vendor/mathjax" } },
      };
      const script = document.createElement("script");
      script.src = "/_anki/js/vendor/mathjax/tex-chtml-full.js";
      script.async = true;
      script.onerror = () => resolve(null);
      document.head.appendChild(script);
    });
    return mathPromise;
  }

  function typeset(node) {
    if (!node || !hasMath(node.textContent)) return;
    mathQueue = mathQueue.then(() => loadMathJax()).then((mj) => {
      if (!mj || !node.isConnected) return;
      return mj.typesetPromise([node]).catch(() => {});
    });
  }

  function setMarkdown(node, text) {
    node.innerHTML = renderMarkdown(text);
    typeset(node);
  }

  /* Where the "stable" (completed blocks) part of a streaming text ends. */
  function stableBoundary(text) {
    let inFence = false;
    let last = 0;
    let i = 0;
    const lines = text.split("\n");
    let pos = 0;
    for (i = 0; i < lines.length - 1; i++) {
      const line = lines[i];
      if (/^\s*```/.test(line)) inFence = !inFence;
      pos += line.length + 1;
      if (!inFence && line.trim() === "" && i > 0) {
        const before = text.slice(0, pos);
        // Don't split inside an unclosed display-math block.
        const opens = (before.match(/\\\[/g) || []).length;
        const closes = (before.match(/\\\]/g) || []).length;
        const dollars = (before.match(/\$\$/g) || []).length;
        if (opens === closes && dollars % 2 === 0) last = pos;
      }
    }
    return last;
  }

  /* ------------------------------------------------------------------ */
  /* State                                                               */
  /* ------------------------------------------------------------------ */
  function applySettings(s) {
    const root = document.documentElement;
    root.style.setProperty("--ex-font-size", (s.fontSize || 15) + "px");
    if (s.accent) root.style.setProperty("--ex-accent", s.accent);
    else root.style.removeProperty("--ex-accent");
    document.body.classList.toggle("reduce-motion", !!s.reduceMotion);
  }

  function setState(next) {
    const prevThread = state && state.thread ? state.thread.id : null;
    state = next;
    applySettings(next.settings || {});
    $("#model").textContent = (next.model && next.model.name) || "Model";
    $("#model").title = (next.model && next.model.id) || "";
    $("#recent").setAttribute("aria-pressed", next.view === "history" || next.view === "history_thread" ? "true" : "false");
    renderStrip(next);
    const thread = next.thread;
    if (!thread || !thread.busy) stopStreamTimer();
    if (thread && thread.busy) {
      if (stream.turnId !== thread.busyTurn) {
        const turn = thread.turns.find((t) => t.id === thread.busyTurn);
        Object.assign(stream, { threadId: thread.id, turnId: thread.busyTurn, text: (turn && turn.text) || "",
          reasoning: (turn && turn.reasoning) || "", stableLen: 0, phase: "reading", phaseInfo: {},
          phaseAt: Date.now(), thinkingAt: 0, startedAt: Date.now() });
      }
      startStreamTimer();
    } else {
      stream.turnId = null;
    }
    renderMain(next, prevThread !== (thread && thread.id));
    renderDock(next);
  }

  function renderStrip(s) {
    const strip = $("#strip");
    strip.textContent = "";
    if (s.view === "onboarding" || s.view === "history") return;
    const c = s.card;
    if (!c) return;
    const side = c.side === "question" ? "Question side" : "Answer side";
    const head = el("div", null, [
      el("span", { class: "kind", text: "Card" }), " · ", c.kind || "Card",
      c.deck ? " · " + c.deck : "", " · ", side,
    ]);
    strip.appendChild(head);
    if (c.target) strip.appendChild(el("div", { class: "target", text: "Target: " + c.target }));
    if (c.sent) strip.appendChild(el("div", { class: "note", text: "Sent: " + c.sent }));
    for (const n of c.notes || []) strip.appendChild(el("div", { class: "note warn", text: n }));
    if (s.notCurrent && s.view === "thread") {
      strip.appendChild(el("div", { class: "note", text: "This conversation is about a different card than the one on screen." }));
    }
  }

  /* ------------------------------------------------------------------ */
  /* Main area                                                           */
  /* ------------------------------------------------------------------ */
  function atBottom(main) {
    return main.scrollHeight - main.scrollTop - main.clientHeight < 40;
  }

  function scrollToBottom() {
    const main = $("#main");
    main.scrollTop = main.scrollHeight;
    $("#newtext").hidden = true;
  }

  function renderMain(s, threadChanged) {
    const main = $("#main");
    if (s.view === "thread" || s.view === "confirm" || s.view === "history_thread") {
      if (s.thread) {
        const wasBottom = atBottom(main);
        renderThread(main, s, threadChanged);
        if (threadChanged || wasBottom) scrollToBottom();
        return;
      }
    }
    main.dataset.thread = "";
    main.textContent = "";
    $("#newtext").hidden = true;
    if (s.view === "onboarding") main.appendChild(renderOnboarding(s));
    else if (s.view === "history") main.appendChild(renderHistory(s));
    else main.appendChild(renderIdle(s));
  }

  function renderIdle(s) {
    const keys = s.shortcuts || {};
    const box = el("div", { class: "empty" });
    if (!s.hasCard) {
      box.appendChild(el("div", { class: "big", text: "Start a review to explain cards." }));
      box.appendChild(el("div", { text: "Open a deck and press Study. Your recent explanations are under Recent." }));
      return box;
    }
    const q = s.side === "question";
    const label = q ? "for a hint" : "to explain this card";
    box.appendChild(el("div", { class: "big" }, ["Press ", el("span", { class: "kbd", text: keys.explain || "X" }), " " + label + "."]));
    if (q) box.appendChild(el("div", { text: "Hints nudge your recall without giving the answer away." }));
    box.appendChild(el("div", { style: "margin-top:12px" }, [
      el("button", { class: "btn primary", type: "button", text: q ? "Get a hint" : "Explain this card", onclick: () => send("explain") }),
    ]));
    if (s.notice) box.appendChild(el("div", { class: "notice-box", text: s.notice }));
    if (s.tip) box.appendChild(el("div", { class: "tip" }, [el("b", { text: "Tip: " }), s.tip]));
    return box;
  }

  function renderOnboarding(s) {
    const ob = s.onboarding || {};
    const input = el("input", { type: "password", id: "keyinput", placeholder: "sk-or-v1-…", "aria-label": "OpenRouter API key", autocomplete: "off", spellcheck: "false" });
    const connect = el("button", { class: "btn primary", type: "button", text: ob.busy ? "Checking…" : "Connect", disabled: ob.busy ? true : null });
    const doConnect = () => send("set_key", { key: input.value });
    connect.addEventListener("click", doConnect);
    const msg = el("div", { class: "msg" + (ob.error ? " err" : ""), role: "status", text: ob.error || "" });
    const box = el("div", { class: "card-box" }, [
      el("h2", { text: "Understand any card in one keypress" }),
      el("p", { text: "Anki Explainer explains the card on screen using your own OpenRouter API key. No account, no subscription; you pay OpenRouter directly (usually well under a cent per explanation)." }),
      el("ol", { class: "steps" }, [
        el("li", null, ["Get a key at ", el("a", { href: "https://openrouter.ai/keys", text: "openrouter.ai/keys" })]),
        el("li", { text: "Paste it below and press Connect." }),
      ]),
      el("form", { class: "field-row", onsubmit: (e) => { e.preventDefault(); doConnect(); } }, [
        el("input", { type: "text", name: "username", autocomplete: "username", value: "openrouter", hidden: true }),
        input, connect,
      ]),
      msg,
      el("div", { class: "fine", text: "Your key is stored only on this computer and is sent only to openrouter.ai. Nothing is sent until you press Explain." }),
    ]);
    setTimeout(() => { if (!ob.busy) input.focus(); }, 0);
    return box;
  }

  function timeAgo(ts) {
    if (!ts) return "";
    const d = (Date.now() / 1000) - ts;
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + " min ago";
    if (d < 86400) return Math.floor(d / 3600) + " h ago";
    const days = Math.floor(d / 86400);
    return days === 1 ? "yesterday" : days + " days ago";
  }

  function renderHistory(s) {
    const wrap = el("div");
    wrap.appendChild(el("div", { class: "section-title", text: "Recent · last 7 days" }));
    const items = s.history || [];
    if (!items.length) {
      wrap.appendChild(el("div", { class: "empty", text: "Nothing explained yet. Explanations you ask for appear here." }));
      return wrap;
    }
    const list = el("ul", { class: "history-list" });
    for (const h of items) {
      const meta = [h.deck, timeAgo(h.time), h.model, h.side === "question" ? "hint" : null].filter(Boolean).join(" · ");
      list.appendChild(el("li", null, [
        el("button", { class: "history-item", type: "button", onclick: () => send("history_open", { id: h.id }) }, [
          el("span", { class: "h-title", text: h.title }),
          el("span", { class: "h-meta", text: meta }),
        ]),
      ]));
    }
    wrap.appendChild(list);
    return wrap;
  }

  function turnSig(t, th) {
    const m = t.meta || {};
    return [t.status, (t.text || "").length, t.id === th.busyTurn ? "busy" : "", m.error ? m.error.message : "",
      m.edit_card ? 1 : 0, m.generic_hint ? 1 : 0, (t.reasoning || "").length,
      th.readOnly ? "ro" : "rw", th.busy ? "b" : "", isLatest(t, th) ? "last" : ""].join("|");
  }

  function renderThread(main, s, threadChanged) {
    const th = s.thread;
    if (threadChanged || main.dataset.thread !== th.id) {
      main.textContent = "";
      main.dataset.thread = th.id;
    }
    const note = main.querySelector(".readonly-note");
    if (th.readOnly && !note) {
      main.insertBefore(el("div", { class: "readonly-note", text: "Read-only view. Press Continue to keep asking about this card." }), main.firstChild);
    } else if (!th.readOnly && note) {
      note.remove();
    }
    const existing = new Map();
    main.querySelectorAll(":scope > .turn").forEach((n) => existing.set(n.dataset.id, n));
    const keep = new Set();
    let anchor = null;
    for (const t of th.turns) {
      keep.add(t.id);
      const sig = turnSig(t, th);
      let node = existing.get(t.id);
      if (node && t.id === th.busyTurn && node.dataset.sig.indexOf("busy") >= 0) {
        node.dataset.sig = sig; // keep the live streaming node as is
      } else if (!node || node.dataset.sig !== sig) {
        const fresh = renderTurn(t, th, s);
        fresh.dataset.sig = sig;
        if (node) {
          node.replaceWith(fresh);
          node = fresh;
        } else {
          node = fresh;
          if (anchor && anchor.nextSibling) main.insertBefore(node, anchor.nextSibling);
          else main.appendChild(node);
        }
      }
      anchor = node;
    }
    existing.forEach((n, id) => { if (!keep.has(id)) n.remove(); });
    const confirm = main.querySelector(".confirm-box");
    if (confirm) confirm.remove();
    if (s.view === "confirm" && s.confirm) {
      main.appendChild(el("div", { class: "confirm-box", role: "alertdialog" }, [
        el("div", { text: s.confirm.message }),
      ]));
    }
  }

  function renderTurn(t, th, s) {
    if (t.role === "user") {
      return el("div", { class: "turn user", "data-id": t.id }, [
        el("div", { class: "bubble" + (t.kind === "chip" || t.kind === "stronger_hint" ? " chip-bubble" : ""), text: t.text }),
      ]);
    }
    const node = el("div", { class: "turn assistant", "data-id": t.id });
    const busy = t.id === th.busyTurn;
    const showReasoning = (s.settings || {}).reasoningDisplay === "collapsible";
    const thinking = el("details", { class: "thinking" }, [el("summary", { text: "Thinking" }), el("div", { class: "reasoning-text" })]);
    thinking.hidden = !(showReasoning && t.reasoning);
    if (showReasoning && t.reasoning) thinking.querySelector(".reasoning-text").textContent = t.reasoning;
    node.appendChild(thinking);
    const body = el("div", { class: "md" });
    node.appendChild(body);
    if (busy) {
      const loading = renderLoading();
      node.appendChild(loading);
      if (t.text) {
        loading.hidden = true;
        stream.text = t.text;
        stream.stableLen = 0;
        renderStreamInto(body);
      }
      return node;
    }
    if (t.text) setMarkdown(body, t.text);
    const m = t.meta || {};
    if (t.status === "stopped") node.appendChild(el("div", { class: "status-tag", text: "(stopped)" }));
    if (t.status === "incomplete") node.appendChild(el("div", { class: "status-tag", text: "(incomplete)" }));
    if (m.generic_hint) node.appendChild(el("div", { class: "generic-note", text: "The model's hint gave the answer away, so a general hint is shown instead." }));
    if (m.images_failed) node.appendChild(el("div", { class: "generic-note", text: m.images_failed + " image(s) couldn't be loaded and weren't sent." }));
    if (m.edit_card && !th.readOnly) {
      node.appendChild(el("div", { style: "margin-top:8px" }, [
        el("button", { class: "btn", type: "button", text: "Edit card", onclick: () => send("edit_card") }),
      ]));
    }
    if ((t.status === "error" || t.status === "incomplete") && m.error) node.appendChild(renderError(m.error, th, isLatest(t, th)));
    return node;
  }

  function isLatest(t, th) {
    const last = [].concat(th.turns).reverse().find((x) => x.role === "assistant");
    return !!last && last.id === t.id;
  }

  function renderError(err, th, latest) {
    const row = el("div", { class: "row" });
    const action = err.action || "retry";
    if (action === "settings") {
      row.appendChild(el("button", { class: "btn primary", type: "button", text: err.actionLabel || "Open settings", onclick: () => send("open_settings", { page: "account" }) }));
    } else if (err.url) {
      row.appendChild(el("button", { class: "btn primary", type: "button", text: err.actionLabel || "Open", onclick: () => send("open_link", { url: err.url }) }));
    }
    if (!th.readOnly && latest && !th.busy) row.appendChild(el("button", { class: "btn", type: "button", text: "Retry", onclick: () => send("retry") }));
    return el("div", { class: "error-box", role: "alert" }, [el("div", { text: err.message }), row]);
  }

  function renderLoading() {
    const box = el("div", { class: "loading" }, [
      el("div", { class: "skeleton", "aria-hidden": "true" }, [el("span"), el("span"), el("span")]),
      el("div", { class: "phase" }, [el("span", { class: "dot", "aria-hidden": "true" }), el("span", { class: "phase-text" })]),
    ]);
    updatePhaseNode(box);
    return box;
  }

  function phaseText() {
    const model = (state && state.model && state.model.name) || "the model";
    const now = Date.now();
    let text;
    if (stream.phase === "thinking") text = fmt(PHASES.thinking, { s: Math.max(1, Math.round((now - stream.thinkingAt) / 1000)) });
    else if (stream.phase === "retrying") {
      const left = Math.max(0, Math.ceil((stream.phaseInfo.seconds || 0) - (now - stream.phaseAt) / 1000));
      text = fmt(PHASES.retrying, { n: left });
    } else text = fmt(PHASES[stream.phase] || PHASES.asking, { model: model });
    const th = state && state.thread;
    if (th && th.busyMode && th.busyMode.indexOf("hint") >= 0 && stream.phase !== "reading" && stream.phase !== "retrying") {
      text = stream.phase === "thinking" ? text : "Writing a hint…";
    }
    return text;
  }

  function isSlow() {
    return !stream.text && stream.phase !== "retrying" && Date.now() - stream.startedAt > 8000;
  }

  function updatePhaseNode(root) {
    const node = (root || document).querySelector(".loading .phase-text");
    if (!node) return;
    node.textContent = phaseText();
    let slow = node.parentNode.querySelector(".slow");
    if (isSlow()) {
      if (!slow) node.parentNode.appendChild(el("span", { class: "slow", text: " · Still working, this model is busy" }));
      const stop = $("#stopbtn");
      if (stop) stop.classList.add("prominent");
    } else if (slow) slow.remove();
  }

  function startStreamTimer() {
    if (stream.timer) return;
    stream.timer = setInterval(() => updatePhaseNode(document), 250);
  }

  function stopStreamTimer() {
    if (stream.timer) clearInterval(stream.timer);
    stream.timer = null;
  }

  function streamNode() {
    if (!stream.turnId) return null;
    return $('#main .turn[data-id="' + stream.turnId + '"]');
  }

  function renderStreamInto(body) {
    let stable = body.querySelector(":scope > .stable");
    let tail = body.querySelector(":scope > .tail");
    if (!stable) {
      body.textContent = "";
      stable = el("div", { class: "stable" });
      tail = el("div", { class: "tail" });
      body.appendChild(stable);
      body.appendChild(tail);
      stream.stableLen = 0;
    }
    const boundary = stableBoundary(stream.text);
    if (boundary > stream.stableLen) {
      const chunk = el("div", { class: "chunk" });
      setMarkdown(chunk, stream.text.slice(stream.stableLen, boundary));
      stable.appendChild(chunk);
      stream.stableLen = boundary;
    }
    tail.innerHTML = renderMarkdown(stream.text.slice(stream.stableLen));
    const caret = el("span", { class: "caret", "aria-hidden": "true" });
    const lastBlock = tail.lastElementChild;
    (lastBlock && /^(P|LI|TD|H\d)$/.test(lastBlock.tagName) ? lastBlock : (lastBlock && lastBlock.querySelector("li:last-child,p:last-child")) || tail).appendChild(caret);
  }

  /* ------------------------------------------------------------------ */
  /* Dock: chips, hint actions, input, actions, footer                   */
  /* ------------------------------------------------------------------ */
  let dockBuilt = false;
  let moreOpen = false;
  let focusMenu = false; // move focus into the More menu only right after the user opens it
  let moreFor = null; // thread the More menu was opened for

  function buildDock() {
    const dock = $("#dock");
    dock.textContent = "";
    dock.appendChild(el("div", { id: "hintActions", class: "hint-actions" }));
    dock.appendChild(el("div", { id: "chips", class: "chips", role: "toolbar", "aria-label": "Learning moves" }));
    dock.appendChild(el("div", { id: "confirmRow", class: "hint-actions" }));
    const input = el("textarea", { id: "input", rows: "1", "aria-label": "Ask a follow-up question", placeholder: "Ask a follow-up…" });
    const sendBtn = el("button", { id: "send", type: "button", "aria-label": "Send follow-up", title: "Send (Enter)", text: "➤" });
    dock.appendChild(el("div", { class: "input-row", id: "inputRow" }, [input, sendBtn]));
    dock.appendChild(el("div", { class: "actions", id: "actions" }, [el("span", { id: "actionBtns", style: "display:contents" }), el("span", { class: "footer", id: "footer" })]));
    input.addEventListener("keydown", onInputKey);
    input.addEventListener("input", autoGrow);
    input.addEventListener("focus", () => send("input_focus", { focused: true }));
    input.addEventListener("blur", () => send("input_focus", { focused: false }));
    sendBtn.addEventListener("click", submitInput);
    dockBuilt = true;
  }

  function autoGrow() {
    const input = $("#input");
    input.style.height = "auto";
    input.style.height = Math.min(140, input.scrollHeight + 2) + "px";
  }

  function submitInput() {
    const input = $("#input");
    const text = input.value.trim();
    if (!text) return;
    if (state && state.thread && state.thread.busy) return;
    input.value = "";
    autoGrow();
    input.blur();
    send("ask", { text: text });
  }

  function onInputKey(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submitInput();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation(); // Anki's own Esc handler would move focus to the dock
      e.target.blur();
      send("focus_reviewer");
    } else if ((e.ctrlKey || e.metaKey) && e.key === ".") {
      e.preventDefault();
      send("stop");
    } else if (e.altKey && /^[1-6]$/.test(e.key)) {
      e.preventDefault();
      triggerChipIndex(+e.key - 1, true);
    }
  }

  function triggerChipIndex(i, typing) {
    const th = state && state.thread;
    if (!th) return;
    if (th.side === "question") {
      if (i === 0 && th.hintActions) send("stronger_hint");
      if (i === 1 || (i === 0 && !th.hintActions)) send("show_answer_explain");
      return;
    }
    const chip = (th.chips || [])[i];
    if (chip) send("chip", { id: chip.id, typing: !!typing });
  }

  function chipButton(c, th, extraClass) {
    const title = c.desc + (c.key ? " (" + c.key + ")" : "");
    return el("button", {
      class: "chip" + (extraClass || "") + (th.busy && stream.activeChip === c.id ? " active" : ""),
      type: "button", title: title, "aria-label": c.label + ". " + title,
      disabled: th.busy ? true : null,
      onclick: () => {
        stream.activeChip = c.id;
        moreOpen = false;
        send("chip", { id: c.id, typing: !!$("#input").value.trim() });
      },
    }, [c.label]);
  }

  function renderDock(s) {
    if (!dockBuilt) buildDock();
    const dock = $("#dock");
    const th = s.thread;
    const v = s.view;
    const show = v === "thread" || v === "confirm" || v === "history_thread" || (v === "idle" && s.hasCard);
    dock.hidden = !show;
    if (!show) return;

    const hint = $("#hintActions");
    hint.textContent = "";
    if (th && v === "thread" && th.side === "question" && th.hasAnswer && !th.readOnly) {
      if (th.hintActions) {
        hint.appendChild(el("button", { class: "btn", type: "button", text: "Stronger hint", title: "A bigger nudge (" + ((s.shortcuts.chips || [])[0] || "") + ")", disabled: th.busy ? true : null, onclick: () => send("stronger_hint") }));
      }
      hint.appendChild(el("button", { class: "btn primary", type: "button", text: "Show answer + explain", title: "Flip the card and explain it", disabled: th.busy ? true : null, onclick: () => send("show_answer_explain") }));
    }

    const chips = $("#chips");
    chips.textContent = "";
    if (moreOpen && (!th || th.id !== moreFor || v !== "thread")) moreOpen = false;
    if (th && v === "thread" && th.side === "answer" && th.hasAnswer && th.chips.length) {
      const visible = th.chips.slice(0, 6);
      const more = th.chips.slice(6);
      visible.forEach((c) => chips.appendChild(chipButton(c, th)));
      if (more.length) {
        const moreBtn = el("button", { class: "chip", type: "button", "aria-haspopup": "menu", "aria-expanded": moreOpen ? "true" : "false", text: "More ▾", disabled: th.busy ? true : null, onclick: (e) => { e.stopPropagation(); moreOpen = !moreOpen; focusMenu = moreOpen; moreFor = th.id; renderDock(state); } });
        chips.appendChild(moreBtn);
        if (moreOpen && !th.busy) {
          const menu = el("div", { class: "more-menu", role: "menu" });
          more.forEach((c) => menu.appendChild(el("button", { type: "button", role: "menuitem", onclick: () => { moreOpen = false; stream.activeChip = c.id; send("chip", { id: c.id, typing: !!$("#input").value.trim() }); } }, [c.label, el("span", { class: "desc", text: c.desc })])));
          chips.appendChild(menu);
          if (focusMenu) {
            focusMenu = false;
            setTimeout(() => { const b = menu.querySelector("button"); if (b) b.focus(); }, 0);
          }
        }
      }
    }

    const confirmRow = $("#confirmRow");
    confirmRow.textContent = "";
    if (v === "confirm") {
      confirmRow.appendChild(el("button", { class: "btn primary", type: "button", text: "Send anyway", onclick: () => send("confirm_send") }));
      confirmRow.appendChild(el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => send("confirm_cancel") }));
    }

    const input = $("#input");
    const inputRow = $("#inputRow");
    const canAsk = (v === "thread" && th && !th.readOnly) || v === "idle";
    inputRow.hidden = !canAsk;
    const focusKey = (s.shortcuts && s.shortcuts.focus) || "Shift+X";
    if (th && th.awaitingQuiz) input.placeholder = "Type your answer…";
    else if (th && th.side === "question") input.placeholder = "Ask about the question…   (" + focusKey + ")";
    else input.placeholder = "Ask a follow-up…   (" + focusKey + ")";
    input.disabled = false;
    $("#send").disabled = !!(th && th.busy);

    const btns = $("#actionBtns");
    btns.textContent = "";
    if (v === "history_thread" && th) {
      btns.appendChild(el("button", { class: "btn primary", type: "button", text: "Continue", onclick: () => send("continue") }));
      btns.appendChild(el("button", { class: "btn", type: "button", text: "Open in Browser", onclick: () => send("open_browser") }));
      btns.appendChild(el("button", { class: "icon-btn", type: "button", text: "⧉ Copy", "aria-label": "Copy explanation", onclick: () => send("copy") }));
      btns.appendChild(el("button", { class: "icon-btn", type: "button", text: "← Back", "aria-label": "Back to Recent", onclick: () => send("history_back") }));
    } else if (th && v === "thread") {
      if (th.busy) {
        btns.appendChild(el("button", { id: "stopbtn", class: "btn" + (isSlow() ? " prominent" : ""), type: "button", text: "■ Stop", title: "Stop generating (" + ((s.shortcuts && s.shortcuts.stop) || "Ctrl+.") + ")", onclick: () => send("stop") }));
      } else if (th.hasAnswer) {
        btns.appendChild(el("button", { class: "icon-btn", type: "button", text: "⧉ Copy", "aria-label": "Copy explanation", title: "Copy the latest answer", onclick: () => send("copy") }));
        btns.appendChild(el("button", { class: "icon-btn", type: "button", text: "↻ Regenerate", "aria-label": "Regenerate", title: "Ask again for a fresh answer", onclick: () => send("regenerate") }));
      }
    }
    $("#footer").textContent = footerText(s);
  }

  function footerText(s) {
    const th = s.thread;
    if (!th || th.busy) return "";
    const last = [].concat(th.turns).reverse().find((t) => t.role === "assistant");
    if (!last) return "";
    const m = last.meta || {};
    const bits = [];
    if (m.cached) bits.push("cached");
    if (s.settings && s.settings.showCost && typeof m.cost === "number" && !m.cached) {
      bits.push("≈$" + (m.cost < 0.0001 ? m.cost.toFixed(6) : m.cost.toFixed(4)));
    }
    return bits.join(" · ");
  }

  /* ------------------------------------------------------------------ */
  /* Python → JS API                                                     */
  /* ------------------------------------------------------------------ */
  function setPhase(threadId, phase, info) {
    if (stream.threadId && threadId !== stream.threadId) return;
    stream.phase = phase;
    stream.phaseInfo = info || {};
    stream.phaseAt = Date.now();
    if (phase === "thinking" && !stream.thinkingAt) stream.thinkingAt = Date.now();
    updatePhaseNode(document);
    const sr = $("#sr-status");
    if (sr) sr.textContent = phaseText();
  }

  function appendDelta(threadId, text, kind) {
    if (threadId !== stream.threadId) return;
    const node = streamNode();
    if (!node) return;
    const main = $("#main");
    const wasBottom = atBottom(main);
    if (kind === "reasoning") {
      stream.reasoning += text;
      const details = node.querySelector("details.thinking");
      if (details) {
        details.hidden = false;
        details.querySelector(".reasoning-text").textContent = stream.reasoning;
      }
    } else {
      stream.text += text;
      const loading = node.querySelector(".loading");
      if (loading && !loading.hidden) loading.hidden = true;
      renderStreamInto(node.querySelector(".md"));
      main.setAttribute("aria-busy", "true");
    }
    if (wasBottom) main.scrollTop = main.scrollHeight;
    else $("#newtext").hidden = false;
  }

  function finish(threadId, meta) {
    $("#main").removeAttribute("aria-busy");
    if (threadId !== stream.threadId) return;
    // The final render happens in the setState that follows.
    stopStreamTimer();
    stream.turnId = null;
    stream.activeChip = null;
    const sr = $("#sr-status");
    if (sr) sr.textContent = "Explanation ready.";
  }

  function error(threadId, err) {
    $("#main").removeAttribute("aria-busy");
    stopStreamTimer();
    stream.turnId = null;
    const sr = $("#sr-status");
    if (sr && err) sr.textContent = err.message || "Error";
  }

  function setTheme(isDark) {
    document.documentElement.classList.toggle("night-mode", !!isDark);
    document.body.classList.toggle("nightMode", !!isDark);
  }

  function focusInput() {
    const input = $("#input");
    if (!input || $("#inputRow").hidden) return;
    input.focus();
    const len = input.value.length;
    input.setSelectionRange(len, len);
  }

  /* ------------------------------------------------------------------ */
  /* Global events                                                       */
  /* ------------------------------------------------------------------ */
  function onClick(e) {
    const a = e.target.closest && e.target.closest("a[href]");
    if (a) {
      e.preventDefault();
      const href = a.getAttribute("href") || "";
      if (/^https?:\/\//i.test(href)) send("open_link", { url: href });
      return;
    }
    if (moreOpen && e.target.isConnected && !e.target.closest(".chips")) {
      moreOpen = false;
      if (state) renderDock(state);
    }
  }

  function onKey(e) {
    const inInput = e.target && (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT");
    if (inInput) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (moreOpen) { moreOpen = false; renderDock(state); return; }
      send("focus_reviewer");
    } else if ((e.ctrlKey || e.metaKey) && e.key === ".") {
      e.preventDefault();
      send("stop");
    } else if (e.altKey && /^[1-6]$/.test(e.key)) {
      e.preventDefault();
      triggerChipIndex(+e.key - 1, false);
    }
  }

  function init() {
    $("#model").addEventListener("click", () => send("model_menu"));
    $("#settings").addEventListener("click", () => send("open_settings"));
    $("#close").addEventListener("click", () => send("close"));
    $("#recent").addEventListener("click", () => {
      if (state && (state.view === "history" || state.view === "history_thread")) send("history_close");
      else send("history");
    });
    $("#newtext").addEventListener("click", scrollToBottom);
    $("#main").addEventListener("scroll", () => { if (atBottom($("#main"))) $("#newtext").hidden = true; });
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKey);
    buildDock();
    send("ready");
  }

  window.Explainer = {
    setState: setState,
    setPhase: setPhase,
    appendDelta: appendDelta,
    finish: finish,
    error: error,
    setTheme: setTheme,
    focusInput: focusInput,
    _renderMarkdown: renderMarkdown,
    _stableBoundary: stableBoundary,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
