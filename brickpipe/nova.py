"""Client for the LDraw Nova web app (ldraw-nova-docker), enough to drive a
repair loop: find or create a chat, send a message with an attached file,
wait for the agent's turn to finish, and fetch the model it produced.

Talks to the same HTTP API the browser UI uses, read from the backend source
(web/backend/main.py in ldraw-nova-docker v0.6.0):

    GET  /api/chats                      list chats
    POST /api/chats                      {"llm_model_id": ...} -> chat
    GET  /api/chats/{id}                 {"chat", "messages", "models"}
    POST /api/chats/{id}/messages        {"text", "documents", "options"} -> 202
    GET  /api/chats/{id}/stream          server-sent events until "done"
    POST /api/chats/{id}/approvals/{aid} {"approved": true}
    GET  /files/...                      a model file

Nova has no login, so there is no credential here.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

DEFAULT_URL = "http://localhost:8765"
TURN_TIMEOUT = 60 * 60  # the agent can run for a long time on a big model


class NovaError(RuntimeError):
    pass


@dataclass
class ProducedModel:
    id: str
    name: str
    file: str
    url: str  # path on the Nova server, e.g. /files/generated/x.mpd
    created_at: float
    parts: int | None
    image_url: str | None = None  # snapshot, once Nova has rendered it


@dataclass
class Turn:
    """What happened during one agent turn."""

    finished: bool
    error: str | None = None
    approvals: int = 0
    seconds: float = 0.0
    usage: dict | None = None  # Nova's token totals for the turn, when it reports them


def document_payload(path: Path) -> dict:
    """A file as Nova's message endpoint expects it (name + base64 data URL)."""
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"name": path.name, "data": f"data:application/octet-stream;base64,{data}"}


class Client:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        opener: Callable[[urllib.request.Request], object] | None = None,
        public_url: str | None = None,
    ):
        """`public_url` is the address browsers should use for links into Nova
        when it differs from `base_url` (a hosted setup where brickpipe talks
        to Nova over an internal network)."""
        self.base_url = base_url.rstrip("/")
        self.public_url = (public_url or '').rstrip('/') or None
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=60))

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._open(req) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:300]
            raise NovaError(f"Nova returned HTTP {err.code} for {method} {path}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            raise NovaError(
                f"Could not reach Nova at {self.base_url} ({err}). Is the web app "
                "running? Start it with: docker compose up -d"
            ) from None
        return json.loads(raw) if raw else {}

    def _get_bytes(self, path: str) -> bytes:
        req = urllib.request.Request(self.base_url + path)
        try:
            with self._open(req) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            raise NovaError(f"Nova returned HTTP {err.code} for {path}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            raise NovaError(f"Could not reach Nova at {self.base_url} ({err})") from None

    # -- chats -------------------------------------------------------------

    def chats(self) -> list[dict]:
        return self._request("GET", "/api/chats").get("chats", [])

    def latest_chat(self) -> dict:
        chats = self.chats()
        if not chats:
            raise NovaError("Nova has no chats yet. Design a model in the web app first.")
        return max(chats, key=lambda c: c.get("updated_at") or c.get("created_at") or 0)

    def find_chat(self, chat_id: str) -> dict:
        if chat_id == "latest":
            return self.latest_chat()
        for chat in self.chats():
            if chat["id"] == chat_id or chat["id"].startswith(chat_id):
                return chat
        raise NovaError(f"No Nova chat matches '{chat_id}'. Chats: "
                        + ", ".join(c["id"] for c in self.chats()[:10]))

    def create_chat(self, llm_model_id: str | None = None) -> dict:
        return self._request("POST", "/api/chats", {"llm_model_id": llm_model_id})

    def generated_models(self) -> list[dict]:
        """Every model in Nova's collection, newest first (name, file, parts, image_url)."""
        return self._request("GET", "/api/models").get("models", [])

    def chat_url(self, chat_id: str) -> str:
        return f"{self.public_url or self.base_url}/chat/{chat_id}"

    def absolute(self, url: str | None) -> str | None:
        """A URL Nova returned (usually server-relative) made absolute, for a
        browser to open."""
        if not url:
            return None
        if url.startswith("http"):
            return url
        return (self.public_url or self.base_url) + url

    def latest_images(self, chat_id: str, limit: int = 4) -> list[str]:
        """Images attached to the chat's most recent messages (the agent's renders)."""
        body = self._request("GET", f"/api/chats/{chat_id}")
        urls: list[str] = []
        for message in reversed(body.get("messages", [])):
            for url in message.get("_image_urls") or []:
                urls.append(self.absolute(url))
            if urls:
                break
        return urls[:limit]

    def is_running(self, chat_id: str) -> bool:
        return bool(self._request("GET", f"/api/chats/{chat_id}")["chat"].get("running"))

    def models(self, chat_id: str) -> list[ProducedModel]:
        body = self._request("GET", f"/api/chats/{chat_id}")
        # The chat endpoint returns models as a dict keyed by id (the chat list
        # endpoint uses a list); accept either.
        models = body.get("models") or {}
        if isinstance(models, dict):
            models = list(models.values())
        out = []
        for m in models:
            if not m.get("model_url"):
                continue  # the file is gone or not servable
            out.append(ProducedModel(
                m["id"], m.get("name", ""), m.get("file", ""),
                m["model_url"].split("?", 1)[0], float(m.get("created_at", 0)),
                m.get("parts"), m.get("image_url"),
            ))
        return sorted(out, key=lambda m: m.created_at)

    def download(self, model: ProducedModel, into: Path) -> Path:
        into.mkdir(parents=True, exist_ok=True)
        target = into / (model.file or f"{model.name}.mpd")
        target.write_bytes(self._get_bytes(model.url))
        return target

    # -- turns -------------------------------------------------------------

    def send(
        self, chat_id: str, text: str, documents: list[Path] = (),
        options: dict | None = None,
    ) -> None:
        self._request("POST", f"/api/chats/{chat_id}/messages", {
            "text": text,
            "documents": [document_payload(p) for p in documents],
            "options": options or {},
        })

    def approve(self, chat_id: str, approval_id: str) -> None:
        self._request("POST", f"/api/chats/{chat_id}/approvals/{approval_id}", {"approved": True})

    def events(self, chat_id: str) -> Iterator[tuple[str, dict]]:
        """Server-sent events for the running turn, until the stream closes."""
        req = urllib.request.Request(
            f"{self.base_url}/api/chats/{chat_id}/stream", headers={"Accept": "text/event-stream"}
        )
        try:
            response = self._open(req)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            raise NovaError(f"Could not open Nova's event stream ({err})") from None
        with response:
            event, data = "message", ""
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()
                elif line == "":
                    if data:
                        try:
                            yield event, json.loads(data)
                        except ValueError:
                            yield event, {"raw": data}
                    event, data = "message", ""

    def wait_for_turn(
        self, chat_id: str, approve_tools: bool = True,
        on_event: Callable[[str, dict], None] | None = None,
        timeout: float = TURN_TIMEOUT,
        stop: Callable[[], bool] = lambda: False,
    ) -> Turn:
        """Follow the event stream until the agent's turn ends.

        With approve_tools the turn is sent with permissions "full" (the same
        choice as the UI's Permissions menu), and any approval the agent still
        asks for is granted, so the loop never stalls waiting for a click.
        Nova's tools act inside the chat's own sandbox folder.
        """
        started = time.monotonic()
        turn = Turn(finished=False)
        while time.monotonic() - started < timeout:
            for event, data in self.events(chat_id):
                if on_event:
                    on_event(event, data)
                if stop():
                    turn.error = "stopped by the user"
                    return turn
                if event == "snapshot":
                    if not data.get("running"):
                        turn.finished = True
                        return turn
                    for pending in data.get("approvals", []):
                        if approve_tools:
                            self.approve(chat_id, pending["id"])
                            turn.approvals += 1
                elif event == "approval" and approve_tools:
                    self.approve(chat_id, data["id"])
                    turn.approvals += 1
                elif event == "turn_error":
                    turn.error = data.get("message", "unknown error")
                elif event == "usage" and isinstance(data.get("total"), dict):
                    turn.usage = data["total"]
                elif event == "done":
                    turn.finished = True
                    turn.seconds = time.monotonic() - started
                    return turn
                elif event == "reconnect":
                    break  # the stream asks us to open a new one
            time.sleep(1)
        turn.error = f"Nova's turn did not finish within {int(timeout)} s"
        return turn
