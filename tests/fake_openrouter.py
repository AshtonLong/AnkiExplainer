"""A scripted fake OpenRouter server for tests and the in-Anki self-test.

Each queued ``Reply`` answers one request. Requests are recorded so tests can
assert exactly how many network calls were made and what was sent.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Reply:
    status: int = 200
    chunks: list[str] = field(default_factory=list)  # SSE text pieces (streamed)
    body: Any = None  # JSON body (non-stream / errors)
    delay: float = 0.0  # before headers
    chunk_delay: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


def sse(*events: Any, done: bool = True, comment: bool = True) -> list[str]:
    out = [": OPENROUTER PROCESSING\n\n"] if comment else []
    for ev in events:
        out.append("data: " + (ev if isinstance(ev, str) else json.dumps(ev)) + "\n\n")
    if done:
        out.append("data: [DONE]\n\n")
    return out


def delta(content: str = "", reasoning: str | None = None, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"content": content}
    if reasoning is not None:
        d["reasoning"] = reasoning
    obj = {"id": "gen-1", "model": "deepseek/deepseek-v4.1-flash-1017", "choices": [{"index": 0, "delta": d}]}
    obj.update(extra)
    return obj


def usage_chunk(cost: float = 0.0004, prompt: int = 300, completion: int = 90) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "model": "deepseek/deepseek-v4.1-flash-1017",
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion, "cost": cost},
    }


def text_reply(text: str, words_per_chunk: int = 3, chunk_delay: float = 0.0, reasoning: str = "") -> Reply:
    parts = text.split(" ")
    events: list[Any] = []
    if reasoning:
        events.append(delta("", reasoning=reasoning))
    for i in range(0, len(parts), words_per_chunk):
        piece = " ".join(parts[i : i + words_per_chunk])
        events.append(delta(piece + (" " if i + words_per_chunk < len(parts) else "")))
    events.append(usage_chunk())
    return Reply(chunks=sse(*events), chunk_delay=chunk_delay)


class FakeOpenRouter:
    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.default: Reply | None = None
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _reply(self) -> Reply:
                with outer._lock:
                    if outer.replies:
                        return outer.replies.pop(0)
                    return outer.default or text_reply("Default answer.")

            def do_GET(self) -> None:  # /api/v1/key
                outer._record("GET", self.path, dict(self.headers), None)
                reply = self._reply()
                body = json.dumps(reply.body or {}).encode()
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer._record("POST", self.path, dict(self.headers), payload)
                reply = self._reply()
                if reply.delay:
                    time.sleep(reply.delay)
                if reply.status != 200 or reply.body is not None:
                    body = json.dumps(reply.body or {}).encode()
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    for k, v in reply.headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for chunk in reply.chunks:
                        if reply.chunk_delay:
                            time.sleep(reply.chunk_delay)
                        data = chunk.encode()
                        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # clients aborting mid-stream (Stop/cancel) is expected

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _record(self, method: str, path: str, headers: dict[str, str], body: Any) -> None:
        with self._lock:
            self.requests.append({"method": method, "path": path, "headers": headers, "body": body, "t": time.monotonic()})

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"

    def start(self) -> "FakeOpenRouter":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def queue(self, *replies: Reply) -> None:
        with self._lock:
            self.replies.extend(replies)

    @property
    def chat_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]
