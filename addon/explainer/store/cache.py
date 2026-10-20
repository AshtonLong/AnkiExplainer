"""SQLite cache, history and usage (spec §8.5, §15).

``user_files/cache.sqlite3``:

* ``explanations`` — complete explanations the user asked for, keyed by
  ``(content_hash, side, mode, selection_hash, model_requested,
  prompt_version)``. Editing a card changes ``content_hash`` so stale entries
  simply stop matching. Partial/stopped text is never stored.
* ``threads`` — conversations for the Recent tab (images stripped).
* ``usage`` — per-month request/token/cost totals.
* ``events`` — local-only metrics for the Usage page (§15).

Retention: 90 days by default and 50 MB, then least-recently-used eviction.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

MAX_BYTES = 50 * 1024 * 1024
SCHEMA_VERSION = 1


def selection_hash(selection: str | None) -> str:
    if not selection:
        return ""
    return hashlib.sha256(" ".join(selection.split()).lower().encode("utf-8")).hexdigest()[:16]


def month_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.localtime(ts if ts is not None else time.time()))


@dataclass(frozen=True)
class CacheKey:
    content_hash: str
    side: str
    mode: str
    selection_hash: str
    model_requested: str
    prompt_version: str


class Cache:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None

    # -- connection -------------------------------------------------------------------
    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _migrate(self) -> None:
        db = self._db
        assert db is not None
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS explanations (
                id INTEGER PRIMARY KEY,
                card_id INTEGER NOT NULL,
                note_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                side TEXT NOT NULL,
                mode TEXT NOT NULL,
                selection_hash TEXT NOT NULL DEFAULT '',
                model_requested TEXT NOT NULL,
                model_resolved TEXT,
                prompt_version TEXT NOT NULL,
                created_at REAL NOT NULL,
                text TEXT NOT NULL,
                reasoning TEXT,
                usage_json TEXT,
                reasoning_details_json TEXT,
                accessed_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ix_expl_key ON explanations
                (content_hash, side, mode, selection_hash, model_requested, prompt_version);
            CREATE INDEX IF NOT EXISTS ix_expl_accessed ON explanations (accessed_at);

            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                card_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                messages_json TEXT NOT NULL,
                note_id INTEGER,
                title TEXT,
                deck TEXT,
                model TEXT,
                side TEXT,
                mode TEXT,
                profile TEXT,
                content_hash TEXT,
                selection_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_threads_updated ON threads (updated_at);
            CREATE INDEX IF NOT EXISTS ix_threads_card ON threads (card_id);

            CREATE TABLE IF NOT EXISTS usage (
                month TEXT PRIMARY KEY,
                requests INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                ts REAL NOT NULL,
                month TEXT NOT NULL,
                card_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                cost REAL,
                followed_up INTEGER NOT NULL DEFAULT 0,
                grade INTEGER,
                seconds_to_grade REAL
            );
            CREATE INDEX IF NOT EXISTS ix_events_month ON events (month);
            """
        )
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()

    # -- explanations ---------------------------------------------------------------------
    def lookup(self, key: CacheKey) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                """SELECT * FROM explanations WHERE content_hash=? AND side=? AND mode=?
                   AND selection_hash=? AND model_requested=? AND prompt_version=?""",
                (key.content_hash, key.side, key.mode, key.selection_hash,
                 key.model_requested, key.prompt_version),
            ).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE explanations SET accessed_at=? WHERE id=?", (time.time(), row["id"]))
            self.db.commit()
            d = dict(row)
            d["usage"] = json.loads(d.pop("usage_json") or "null")
            d["reasoning_details"] = json.loads(d.pop("reasoning_details_json") or "null")
            return d

    def store(
        self,
        key: CacheKey,
        card_id: int,
        note_id: int,
        text: str,
        reasoning: str = "",
        usage: dict[str, Any] | None = None,
        model_resolved: str | None = None,
        reasoning_details: list[dict[str, Any]] | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self.db.execute(
                """INSERT INTO explanations (card_id, note_id, content_hash, side, mode,
                       selection_hash, model_requested, model_resolved, prompt_version,
                       created_at, text, reasoning, usage_json, reasoning_details_json, accessed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (content_hash, side, mode, selection_hash, model_requested, prompt_version)
                   DO UPDATE SET text=excluded.text, reasoning=excluded.reasoning,
                       usage_json=excluded.usage_json, model_resolved=excluded.model_resolved,
                       reasoning_details_json=excluded.reasoning_details_json,
                       created_at=excluded.created_at, accessed_at=excluded.accessed_at""",
                (card_id, note_id, key.content_hash, key.side, key.mode, key.selection_hash,
                 key.model_requested, model_resolved, key.prompt_version, now, text, reasoning,
                 json.dumps(usage) if usage else None,
                 json.dumps(reasoning_details) if reasoning_details else None, now),
            )
            self.db.commit()

    # -- threads -------------------------------------------------------------------------
    def save_thread(self, d: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(
                """INSERT INTO threads (id, card_id, created_at, updated_at, messages_json, note_id,
                       title, deck, model, side, mode, profile, content_hash, selection_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (id) DO UPDATE SET updated_at=excluded.updated_at,
                       messages_json=excluded.messages_json, model=excluded.model""",
                (d["id"], d["card_id"], d["created_at"], d["updated_at"], json.dumps(d),
                 d.get("note_id"), d.get("title", ""), d.get("deck", ""), d.get("model", ""),
                 d.get("side", ""), d.get("mode", ""), d.get("profile", ""),
                 d.get("content_hash", ""), selection_hash(d.get("selection"))),
            )
            self.db.commit()

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT messages_json FROM threads WHERE id=?", (thread_id,)).fetchone()
        return json.loads(row["messages_json"]) if row else None

    def find_thread(
        self, card_id: int, side: str, sel_hash: str, content_hash: str, profile: str,
        mode: str | None = None, model: str | None = None,
    ) -> dict[str, Any] | None:
        sql = """SELECT messages_json FROM threads WHERE card_id=? AND side=? AND selection_hash=?
                 AND content_hash=? AND profile=?"""
        args: list[Any] = [card_id, side, sel_hash, content_hash, profile]
        if mode is not None:
            sql += " AND mode=?"
            args.append(mode)
        if model is not None:
            sql += " AND model=?"
            args.append(model)
        with self._lock:
            row = self.db.execute(sql + " ORDER BY updated_at DESC LIMIT 1", args).fetchone()
        return json.loads(row["messages_json"]) if row else None

    def history(self, profile: str, days: int = 7, limit: int = 200) -> list[dict[str, Any]]:
        since = time.time() - days * 86400
        with self._lock:
            rows = self.db.execute(
                """SELECT id, card_id, note_id, title, deck, model, side, mode, updated_at, selection_hash
                   FROM threads WHERE profile=? AND updated_at>=? ORDER BY updated_at DESC LIMIT ?""",
                (profile, since, limit * 3),
            ).fetchall()
        # One entry per card/side/selection: the most recent conversation.
        seen: set[tuple[Any, ...]] = set()
        out = []
        for r in rows:
            key = (r["card_id"], r["side"], r["selection_hash"])
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(r))
        return out[:limit]

    # -- usage ------------------------------------------------------------------------------
    def record_usage(self, usage: dict[str, Any] | None, month: str | None = None) -> None:
        usage = usage or {}
        month = month or month_key()
        with self._lock:
            self.db.execute(
                """INSERT INTO usage (month, requests, prompt_tokens, completion_tokens, cost_usd)
                   VALUES (?, 1, ?, ?, ?)
                   ON CONFLICT (month) DO UPDATE SET requests=requests+1,
                       prompt_tokens=prompt_tokens+excluded.prompt_tokens,
                       completion_tokens=completion_tokens+excluded.completion_tokens,
                       cost_usd=cost_usd+excluded.cost_usd""",
                (month, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
                 float(usage.get("cost") or 0.0)),
            )
            self.db.commit()

    def month_usage(self, month: str | None = None) -> dict[str, Any]:
        month = month or month_key()
        with self._lock:
            row = self.db.execute("SELECT * FROM usage WHERE month=?", (month,)).fetchone()
        if row is None:
            return {"month": month, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
        return dict(row)

    # -- metrics (§15) -------------------------------------------------------------------------
    def record_event(self, card_id: int, mode: str, cost: float | None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO events (ts, month, card_id, mode, cost) VALUES (?,?,?,?,?)",
                (time.time(), month_key(), card_id, mode, cost),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def mark_followed_up(self, event_id: int) -> None:
        with self._lock:
            self.db.execute("UPDATE events SET followed_up=1 WHERE id=?", (event_id,))
            self.db.commit()

    def record_grade(self, event_id: int, grade: int, seconds: float) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE events SET grade=?, seconds_to_grade=? WHERE id=? AND grade IS NULL",
                (grade, seconds, event_id),
            )
            self.db.commit()

    def metrics(self, month: str | None = None) -> dict[str, Any]:
        month = month or month_key()
        with self._lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM events WHERE month=?", (month,))]
        explains = [r for r in rows if r["mode"] in ("concise", "selection", "explain_no_reveal")]
        hints = [r for r in rows if r["mode"] == "hint"]
        times = sorted(r["seconds_to_grade"] for r in explains if r["seconds_to_grade"] is not None)
        costs = sorted(r["cost"] for r in explains if r["cost"] is not None)
        graded_hints = [r for r in hints if r["grade"] is not None]
        return {
            "explanations": len(explains),
            "median_seconds_to_grade": _median(times),
            "depth_rate": (sum(r["followed_up"] for r in explains) / len(explains)) if explains else None,
            "hints": len(hints),
            "hint_resolution": (
                sum(1 for r in graded_hints if r["grade"] >= 3) / len(graded_hints) if graded_hints else None
            ),
            "median_cost": _median(costs),
        }

    # -- maintenance ----------------------------------------------------------------------------
    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def clear(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM explanations")
            self.db.execute("DELETE FROM threads")
            self.db.commit()
            self.db.execute("VACUUM")

    def evict(self, retention_days: int, max_bytes: int = MAX_BYTES) -> int:
        """Drop entries past retention, then LRU-evict until under ``max_bytes``."""
        cutoff = time.time() - retention_days * 86400
        removed = 0
        with self._lock:
            removed += self.db.execute("DELETE FROM explanations WHERE accessed_at < ?", (cutoff,)).rowcount
            removed += self.db.execute("DELETE FROM threads WHERE updated_at < ?", (cutoff,)).rowcount
            self.db.commit()

            def content_bytes() -> int:
                row = self.db.execute(
                    """SELECT COALESCE(SUM(LENGTH(text) + COALESCE(LENGTH(reasoning),0)
                       + COALESCE(LENGTH(reasoning_details_json),0)), 0) FROM explanations"""
                ).fetchone()
                row2 = self.db.execute("SELECT COALESCE(SUM(LENGTH(messages_json)),0) FROM threads").fetchone()
                return int(row[0]) + int(row2[0])

            while content_bytes() > max_bytes:
                n = self.db.execute(
                    """DELETE FROM explanations WHERE id IN
                       (SELECT id FROM explanations ORDER BY accessed_at ASC LIMIT 50)"""
                ).rowcount
                n += self.db.execute(
                    """DELETE FROM threads WHERE id IN
                       (SELECT id FROM threads ORDER BY updated_at ASC LIMIT 20)"""
                ).rowcount
                removed += n
                if n == 0:
                    break
            self.db.commit()
            if removed:
                self.db.execute("VACUUM")
        return removed


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2
