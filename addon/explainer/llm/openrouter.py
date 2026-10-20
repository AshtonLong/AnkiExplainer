"""OpenRouter HTTP client (spec §7).

* ``SSEParser`` — incremental Server-Sent-Events parser (split lines across
  chunks, ``\\r\\n``/``\\r`` endings, comment lines, multi-line data, ``[DONE]``).
* ``StreamAccumulator`` — folds chunks into answer text, reasoning text,
  merged ``reasoning_details``, usage and the resolved model; a top-level
  ``error`` mid-stream ends the stream and keeps the partial text.
* ``OpenRouterClient`` — the only network code in the add-on: one chat request
  per explicit user action, plus ``GET /api/v1/key`` for "Test connection".
  Timeouts: connect 10 s, first byte 30 s, idle 60 s. 429s are retried with
  1/2/4 s backoff (max 3). ``CancelToken`` aborts a request from any thread.

Runs on a background thread; never touches Qt.
"""

from __future__ import annotations

import codecs
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

BASE_URL = "https://openrouter.ai/api/v1"
REFERER = "https://github.com/AshtonLong/anki-explainer"
APP_TITLE = "Anki Explainer"

CONNECT_TIMEOUT = 10.0
FIRST_BYTE_TIMEOUT = 30.0
IDLE_TIMEOUT = 60.0
NON_STREAM_TIMEOUT = 180.0
RETRY_BACKOFF = (1.0, 2.0, 4.0)

KEYS_URL = "https://openrouter.ai/keys"
CREDITS_URL = "https://openrouter.ai/settings/credits"
PROVIDER_SETTINGS_URL = "https://openrouter.ai/settings/preferences"
PRIVACY_SETTINGS_URL = "https://openrouter.ai/settings/privacy"

DONE = object()


# --- SSE ------------------------------------------------------------------------


class SSEParser:
    """Incremental SSE parser. ``feed()`` returns completed event payloads:
    ``str`` for a ``data:`` event, or the ``DONE`` sentinel for ``[DONE]``."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""
        self._data: list[str] = []
        self.done = False

    def feed(self, chunk: bytes | str) -> list[Any]:
        if isinstance(chunk, bytes):
            chunk = self._decoder.decode(chunk)
        self._buf += chunk
        events: list[Any] = []
        while True:
            idx_n = self._buf.find("\n")
            idx_r = self._buf.find("\r")
            if idx_n < 0 and idx_r < 0:
                break
            if idx_r >= 0 and (idx_n < 0 or idx_r < idx_n):
                # Need to see the next char to know whether this is "\r\n".
                if idx_r == len(self._buf) - 1:
                    break
                line = self._buf[:idx_r]
                skip = 2 if self._buf[idx_r + 1] == "\n" else 1
                self._buf = self._buf[idx_r + skip:]
            else:
                line = self._buf[:idx_n]
                self._buf = self._buf[idx_n + 1:]
            ev = self._line(line)
            if ev is not None:
                events.append(ev)
        return events

    def flush(self) -> list[Any]:
        events: list[Any] = []
        if self._buf:
            ev = self._line(self._buf)
            self._buf = ""
            if ev is not None:
                events.append(ev)
        ev = self._dispatch()
        if ev is not None:
            events.append(ev)
        return events

    def _line(self, line: str) -> Any:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None  # comment, e.g. ": OPENROUTER PROCESSING"
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        return None

    def _dispatch(self) -> Any:
        if not self._data:
            return None
        data = "\n".join(self._data)
        self._data = []
        if data.strip() == "[DONE]":
            self.done = True
            return DONE
        return data


# --- errors -------------------------------------------------------------------------


@dataclass
class ApiError(Exception):
    kind: str
    message: str
    status: int | None = None
    detail: str = ""
    retry_after: float | None = None
    action: str = "retry"  # retry | settings | credits | provider_settings
    partial: bool = False

    def __str__(self) -> str:
        return f"{self.kind} ({self.status}): {self.message}"


class Cancelled(Exception):
    pass


MESSAGES = {
    "auth": ("OpenRouter didn't accept this API key.", "settings"),
    "credits": ("Your OpenRouter credits have run out.", "credits"),
    "moderation": ("The model provider declined this request.", "retry"),
    "routing": (
        "OpenRouter couldn't route this request. Check your OpenRouter provider settings.",
        "provider_settings",
    ),
    "timeout": ("The model took too long to respond.", "retry"),
    "rate_limit": ("Rate limited by OpenRouter. Try again in a moment.", "retry"),
    "server": ("The provider had a problem.", "retry"),
    "offline": ("You appear to be offline.", "retry"),
    "bad_request": ("OpenRouter rejected the request.", "retry"),
    "unknown": ("Something went wrong talking to OpenRouter.", "retry"),
}


def make_error(kind: str, status: int | None = None, detail: str = "", **kw: Any) -> ApiError:
    message, action = MESSAGES.get(kind, MESSAGES["unknown"])
    if kind == "bad_request" and detail:
        message = f"OpenRouter rejected the request: {detail[:200]}"
    return ApiError(kind=kind, message=message, status=status, detail=detail, action=action, **kw)


def classify_status(status: int, body: Any = None) -> str:
    error_type = ""
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict):
            error_type = str((err.get("metadata") or {}).get("error_type", "") or "")
    if status == 401:
        return "auth"
    if status == 402:
        return "credits"
    if status == 403:
        return "moderation"
    if status in (404, 503):
        return "routing"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status == 400:
        return "bad_request"
    if status >= 500:
        if error_type == "provider_unavailable":
            return "routing"
        if error_type == "timeout":
            return "timeout"
        return "server"
    return "unknown"


def error_from_body(status: int, body: Any, retry_after: float | None = None) -> ApiError:
    detail = ""
    code = status
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        detail = str(body["error"].get("message", ""))
        try:
            code = int(body["error"].get("code", status))
        except (TypeError, ValueError):
            code = status
    kind = classify_status(code if code >= 400 else status, body)
    return make_error(kind, status=code, detail=detail, retry_after=retry_after)


# --- stream accumulation --------------------------------------------------------------


@dataclass
class StreamResult:
    content: str = ""
    reasoning: str = ""
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    model: str | None = None
    finish_reason: str | None = None
    error: ApiError | None = None
    generation_id: str | None = None


class StreamAccumulator:
    def __init__(self) -> None:
        self.result = StreamResult()
        self._details: dict[tuple[Any, str], dict[str, Any]] = {}
        self._detail_order: list[tuple[Any, str]] = []

    def apply(self, obj: dict[str, Any]) -> tuple[str, str]:
        """Fold one chunk; returns (content_delta, reasoning_delta)."""
        r = self.result
        if obj.get("model"):
            r.model = obj["model"]
        if obj.get("id") and not r.generation_id:
            r.generation_id = obj["id"]
        if isinstance(obj.get("usage"), dict):
            r.usage = obj["usage"]
        if isinstance(obj.get("error"), dict):
            status = obj["error"].get("code")
            try:
                status_i = int(status)
            except (TypeError, ValueError):
                status_i = 500
            err = error_from_body(status_i, obj)
            err.partial = True
            r.error = err
        content = ""
        reasoning = ""
        choices = obj.get("choices") or []
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta.get("content"), str):
                content = delta["content"]
            rd = delta.get("reasoning_details")
            if isinstance(delta.get("reasoning"), str):
                reasoning = delta["reasoning"]
            elif isinstance(rd, list):
                reasoning = "".join(
                    str(d.get("text") or d.get("summary") or "")
                    for d in rd
                    if isinstance(d, dict) and d.get("type") in ("reasoning.text", "reasoning.summary")
                )
            if isinstance(rd, list):
                self._merge_details(rd)
            if choice.get("finish_reason"):
                r.finish_reason = choice["finish_reason"]
        r.content += content
        r.reasoning += reasoning
        return content, reasoning

    def _merge_details(self, details: list[Any]) -> None:
        for d in details:
            if not isinstance(d, dict):
                continue
            key = (d.get("index", len(self._detail_order)), str(d.get("type", "")))
            cur = self._details.get(key)
            if cur is None:
                self._details[key] = dict(d)
                self._detail_order.append(key)
                continue
            for k, v in d.items():
                if k in ("text", "summary", "data") and isinstance(v, str):
                    cur[k] = (cur.get(k) or "") + v
                elif v is not None:
                    cur[k] = v
        self.result.reasoning_details = [self._details[k] for k in self._detail_order]


# --- cancellation ----------------------------------------------------------------------


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._response: Any = None
        self.reason = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            resp = self._response
        if resp is not None:
            _abort_response(resp)

    def attach(self, resp: Any) -> None:
        with self._lock:
            self._response = resp
            cancelled = self._event.is_set()
        if cancelled:
            _abort_response(resp)

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; True if cancelled meanwhile."""
        return self._event.wait(seconds)


def _raw_socket(resp: Any) -> Any:
    try:
        return resp.raw._fp.fp.raw._sock  # urllib3 → http.client → SocketIO
    except Exception:
        return None


def _abort_response(resp: Any) -> None:
    sock = _raw_socket(resp)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
    try:
        resp.close()
    except Exception:
        pass


# --- client ---------------------------------------------------------------------------------

EventSink = Callable[[str, Any], None]


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        session_factory: Callable[[], Any] | None = None,
        sleep_backoff: tuple[float, ...] = RETRY_BACKOFF,
        first_byte_timeout: float = FIRST_BYTE_TIMEOUT,
        idle_timeout: float = IDLE_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._session_factory = session_factory
        self.backoff = sleep_backoff
        self.first_byte_timeout = first_byte_timeout
        self.idle_timeout = idle_timeout

    def _session(self) -> Any:
        if self._session_factory:
            return self._session_factory()
        import requests

        return requests.Session()

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": REFERER,
            "X-Title": APP_TITLE,
        }

    # -- chat ----------------------------------------------------------------------
    def chat(self, body: dict[str, Any], on_event: EventSink, cancel: CancelToken) -> StreamResult:
        """Run one chat completion (streaming or not) with 429 retries."""
        attempt = 0
        while True:
            if cancel.cancelled:
                raise Cancelled()
            try:
                if body.get("stream", True):
                    return self._stream_once(body, on_event, cancel)
                return self._complete_once(body, on_event, cancel)
            except ApiError as err:
                if err.kind != "rate_limit" or err.partial or attempt >= len(self.backoff):
                    raise
                wait = self.backoff[attempt]
                if err.retry_after:
                    wait = min(max(wait, err.retry_after), 10.0)
                attempt += 1
                on_event("retry", {"seconds": wait, "attempt": attempt, "max": len(self.backoff)})
                if cancel.wait(wait):
                    raise Cancelled()
                on_event("phase", "asking")

    def _post(self, session: Any, body: dict[str, Any], stream: bool, read_timeout: float) -> Any:
        import requests

        try:
            return session.post(
                f"{self.base_url}/chat/completions",
                data=json.dumps(body),
                headers=self.headers(),
                stream=stream,
                timeout=(CONNECT_TIMEOUT, read_timeout),
            )
        except requests.exceptions.ConnectTimeout as e:
            raise make_error("timeout", detail=type(e).__name__) from None
        except requests.exceptions.ReadTimeout as e:
            raise make_error("timeout", detail=type(e).__name__) from None
        except requests.exceptions.SSLError as e:
            raise make_error("offline", detail=f"ssl: {type(e).__name__}") from None
        except requests.exceptions.ConnectionError as e:
            raise make_error("offline", detail=type(e).__name__) from None

    @staticmethod
    def _http_error(resp: Any) -> ApiError:
        try:
            body = resp.json()
        except Exception:
            body = None
        retry_after = None
        ra = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
        if ra:
            try:
                retry_after = float(ra)
            except ValueError:
                retry_after = None
        return error_from_body(resp.status_code, body, retry_after)

    def _stream_once(self, body: dict[str, Any], on_event: EventSink, cancel: CancelToken) -> StreamResult:
        import requests

        session = self._session()
        started = time.monotonic()
        state = {"last": started, "first": False, "timed_out": False, "done": False}
        resp_holder: dict[str, Any] = {}

        def watchdog() -> None:
            while not state["done"] and not cancel.cancelled:
                time.sleep(0.25)
                now = time.monotonic()
                limit = self.idle_timeout if state["first"] else self.first_byte_timeout
                if now - state["last"] > limit and "resp" in resp_holder:
                    state["timed_out"] = True
                    _abort_response(resp_holder["resp"])
                    return

        try:
            resp = self._post(session, body, True, self.first_byte_timeout)
            resp_holder["resp"] = resp
            cancel.attach(resp)
            if cancel.cancelled:
                raise Cancelled()
            if resp.status_code != 200:
                err = self._http_error(resp)
                resp.close()
                raise err
            sock = _raw_socket(resp)
            if sock is not None:
                try:
                    sock.settimeout(self.idle_timeout)
                except Exception:
                    pass
            state["last"] = time.monotonic()
            threading.Thread(target=watchdog, daemon=True, name="explainer-watchdog").start()

            parser = SSEParser()
            acc = StreamAccumulator()
            try:
                for chunk in resp.iter_content(chunk_size=None):
                    if cancel.cancelled:
                        raise Cancelled()
                    if not chunk:
                        continue
                    state["last"] = time.monotonic()
                    for ev in parser.feed(chunk):
                        if self._handle_event(ev, acc, on_event, state):
                            return self._finish(acc)
                for ev in parser.flush():
                    if self._handle_event(ev, acc, on_event, state):
                        return self._finish(acc)
            except Cancelled:
                raise
            except ApiError:
                raise
            except Exception as e:
                if cancel.cancelled:
                    raise Cancelled() from None
                # requests re-raises urllib3 read timeouts as ConnectionError.
                timed_out = state["timed_out"] or isinstance(e, (requests.exceptions.Timeout, socket.timeout))
                if timed_out or "timed out" in str(e).lower():
                    raise make_error("timeout", detail="idle", partial=bool(acc.result.content)) from None
                raise make_error("offline", detail=type(e).__name__, partial=bool(acc.result.content)) from None
            if cancel.cancelled:
                raise Cancelled()
            if state["timed_out"]:
                raise make_error("timeout", detail="idle", partial=bool(acc.result.content))
            return self._finish(acc)
        finally:
            state["done"] = True
            try:
                session.close()
            except Exception:
                pass

    def _handle_event(self, ev: Any, acc: StreamAccumulator, on_event: EventSink, state: dict[str, Any]) -> bool:
        """Process one SSE payload. Returns True when the stream is finished."""
        if ev is DONE:
            return True
        try:
            obj = json.loads(ev)
        except ValueError:
            return False
        if not isinstance(obj, dict):
            return False
        content, reasoning = acc.apply(obj)
        if reasoning:
            on_event("reasoning", reasoning)
        if content:
            state["first"] = True
            on_event("content", content)
        if acc.result.error is not None:
            return True
        return False

    @staticmethod
    def _finish(acc: StreamAccumulator) -> StreamResult:
        if acc.result.error is not None:
            err = acc.result.error
            err.partial = bool(acc.result.content)
            raise err
        return acc.result

    def _complete_once(self, body: dict[str, Any], on_event: EventSink, cancel: CancelToken) -> StreamResult:
        session = self._session()
        try:
            resp = self._post(session, body, False, NON_STREAM_TIMEOUT)
            cancel.attach(resp)
            if cancel.cancelled:
                raise Cancelled()
            if resp.status_code != 200:
                raise self._http_error(resp)
            try:
                obj = resp.json()
            except ValueError:
                raise make_error("server", detail="invalid json") from None
            acc = StreamAccumulator()
            acc.apply(obj)
            if acc.result.error is not None:
                raise acc.result.error
            if acc.result.reasoning:
                on_event("reasoning", acc.result.reasoning)
            if acc.result.content:
                on_event("content", acc.result.content)
            return acc.result
        finally:
            try:
                session.close()
            except Exception:
                pass

    # -- key info -------------------------------------------------------------------------
    def key_info(self) -> dict[str, Any]:
        import requests

        session = self._session()
        try:
            try:
                resp = session.get(
                    f"{self.base_url}/key",
                    headers={k: v for k, v in self.headers().items() if k != "Content-Type"},
                    timeout=(CONNECT_TIMEOUT, 15),
                )
            except requests.exceptions.Timeout:
                raise make_error("timeout") from None
            except requests.exceptions.ConnectionError:
                raise make_error("offline") from None
            if resp.status_code != 200:
                raise self._http_error(resp)
            data = resp.json().get("data") or {}
            return data if isinstance(data, dict) else {}
        finally:
            try:
                session.close()
            except Exception:
                pass


def describe_key_info(data: dict[str, Any]) -> str:
    """Human summary for the settings dialog."""
    lines = []
    label = data.get("label")
    if label:
        lines.append(f"Key: {label}")
    remaining = data.get("limit_remaining")
    limit = data.get("limit")
    usage = data.get("usage")
    if remaining is not None:
        lines.append(f"Credit remaining on this key: ${float(remaining):.2f}" + (f" of ${float(limit):.2f}" if limit is not None else ""))
    else:
        lines.append("No spending limit on this key (account credit applies).")
    if usage is not None:
        lines.append(f"Used so far: ${float(usage):.4f}")
    rl = data.get("rate_limit")
    if isinstance(rl, dict) and rl.get("requests"):
        lines.append(f"Rate limit: {rl.get('requests')} requests / {rl.get('interval', '')}")
    free = data.get("free_model_daily_requests")
    if isinstance(free, dict) and free.get("limit"):
        lines.append(f"Free-model requests today: {free.get('used', 0)}/{free.get('limit')}")
    if data.get("is_free_tier"):
        lines.append("This account is on the free tier; add credits to use paid models.")
    return "\n".join(lines)


def build_body(
    model: str,
    messages: list[dict[str, Any]],
    *,
    stream: bool,
    temperature: float,
    max_tokens: int,
    effort: str,
    budget_tokens: int | None,
    exclude_reasoning: bool,
    supports_reasoning: bool = True,
) -> dict[str, Any]:
    """Request body (§7.1). No provider/routing fields are ever sent."""
    body: dict[str, Any] = {
        "model": model,
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "usage": {"include": True},
        "messages": messages,
    }
    if supports_reasoning:
        reasoning: dict[str, Any]
        if budget_tokens and effort != "none":
            reasoning = {"max_tokens": int(budget_tokens)}
            # reasoning.max_tokens must be strictly below the request max_tokens.
            if body["max_tokens"] <= budget_tokens:
                body["max_tokens"] = int(budget_tokens) + max_tokens
        else:
            reasoning = {"effort": effort}
        if exclude_reasoning and effort != "none":
            reasoning["exclude"] = True
        body["reasoning"] = reasoning
    return body
