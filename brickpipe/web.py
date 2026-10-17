"""brickpipe's own web page: a control panel for the design-check-repair loop.

    python -m brickpipe.web            # then open http://localhost:8766

Standard library only. One page (web/index.html) talks to a small JSON API;
runs execute in background threads and stream their progress to the page as
server-sent events. Runs and their files live under ./runs/<id>/.

The Rebrickable key is kept in the browser and sent with each run request; the
server holds it in memory for that run only and never writes it to disk.
"""
from __future__ import annotations

import json
import os
import mimetypes
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
import urllib.parse
from urllib.parse import parse_qs, unquote, urlparse

from . import bricklink, connectivity, features, inventory, loop, nova, rebrickable
from .catalog import load_colours
from .cli import PipelineError, run_pipeline

DEFAULT_PORT = 8766
RUN_FILES = ("report.md", "feedback.md", "parts.csv", "bricklink_wanted.xml",
             "flat.ldr", "instructions.mpd", "availability.csv", "owned.csv", "prices.csv",
             "connectivity.csv")

DESIGN_MESSAGE_SUFFIX = (
    "\n\nWhen the model is finished, deliver it as an .mpd. It will be checked "
    "for parts that cannot be bought and sent back to you to repair."
)


def guess_nova_db(start: Path) -> str:
    """Nova's database if the repos sit next to brickpipe, else empty."""
    for base in (start, start.parent, start.parent.parent):
        candidate = base / "ldraw-nova" / "data" / "ldraw-info.db"
        if candidate.is_file():
            return str(candidate)
    return ""


# -- run state ------------------------------------------------------------------

@dataclass
class Run:
    id: str
    dir: Path
    request: dict
    status: str = "queued"  # queued, designing, checking, repairing, done, failed, stopped
    phase: str = ""  # what the agent is doing right now
    chat_id: str | None = None
    chat_url: str | None = None
    rounds: list[dict] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    log: list[str] = field(default_factory=list)
    agent_text: str = ""  # what the agent is saying right now (this turn)
    activity: list[dict] = field(default_factory=list)  # tool calls, newest last
    tools_running: dict = field(default_factory=dict)
    events_seen: int = 0
    design_tokens: dict | None = None  # Nova's token totals for the design turn
    stop_requested: bool = False
    subscribers: list[queue.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def public(self) -> dict:
        req = {k: v for k, v in self.request.items()
               if k not in ("rebrickable_key", "rebrickable_user_token", "bricklink")}
        req["availability"] = bool(self.request.get("rebrickable_key"))
        req["prices"] = bricklink.Credentials.from_dict(self.request.get("bricklink") or {}) is not None
        req["owned_from_rebrickable"] = bool(self.request.get("rebrickable_user_token"))
        req["owned_lines"] = len([ln for ln in (self.request.get("owned_text") or "").splitlines() if ln.strip()])
        return {
            "id": self.id, "status": self.status, "phase": self.phase,
            "chat_id": self.chat_id, "chat_url": self.chat_url, "rounds": self.rounds,
            "result": self.result, "error": self.error, "started_at": self.started_at,
            "finished_at": self.finished_at, "log": self.log[-300:], "request": req,
            "agent_text": self.agent_text[-8000:], "events_seen": self.events_seen,
            "activity": self.activity[-60:],
            "design_tokens": self.design_tokens,
            "files": sorted(p.name for p in self.final_dir().iterdir()) if self.final_dir() else [],
        }

    def final_dir(self) -> Path | None:
        rounds = sorted(self.dir.glob("round-*"), key=lambda p: int(p.name.split("-")[1]))
        return rounds[-1] if rounds else None

    def emit(self, event: str, data: dict | None = None) -> None:
        with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait((event, data or {}))
                except queue.Full:
                    self.subscribers.remove(q)
        self.save()

    def note(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {text}")
        self.emit("log", {"text": self.log[-1]})

    def save(self) -> None:
        try:
            (self.dir / "state.json").write_text(json.dumps(self.public(), indent=1), "utf-8")
        except OSError:
            pass


def viewer_urls(agent: nova.Client, model: nova.ProducedModel | None) -> dict:
    """Nova's own 3D viewer and step-by-step player for a model, to embed."""
    if model is None:
        return {}
    query = "model=" + urllib.parse.quote(model.url, safe="") + (
        f"&parts={model.parts}" if model.parts is not None else "")
    base = agent.public_url or agent.base_url
    return {"viewer": f"{base}/viewer/viewer.html?{query}",
            "player": f"{base}/viewer/player.html?{query}"}


def round_dict(rnd: loop.Round) -> dict:
    out = {"number": rnd.number, "model": rnd.model.name, "sent": rnd.sent, "error": rnd.error,
           "sent_at": rnd.sent_at}
    if rnd.result:
        out.update(pieces=rnd.result.piece_count, lots=rnd.result.lot_count,
                   errors=rnd.result.errors, warnings=rnd.result.warnings,
                   clean=not rnd.result.feedback)
    if rnd.turn:
        out.update(agent_seconds=int(rnd.turn.seconds), agent_error=rnd.turn.error,
                   tokens=tokens_dict(rnd.turn.usage))
    return out


def tokens_dict(usage: dict | None) -> dict | None:
    """The part of Nova's usage totals the page shows."""
    if not usage:
        return None
    return {k: usage.get(k) for k in ("input", "output", "cache_read", "cache_write", "steps", "cost_usd", "model")}


def tokens_sum(parts: list[dict | None]) -> dict | None:
    parts = [p for p in parts if p]
    if not parts:
        return None
    out = {k: sum(int(p.get(k) or 0) for p in parts) for k in ("input", "output", "cache_read", "cache_write", "steps")}
    costs = [p.get("cost_usd") for p in parts if p.get("cost_usd") is not None]
    out["cost_usd"] = round(sum(costs), 4) if costs else None
    out["turns"] = len(parts)
    return out


class Runner:
    def __init__(self, runs_dir: Path, settings: dict | None = None):
        self.runs_dir = runs_dir
        self.settings = settings or {}
        self.runs: dict[str, Run] = {}
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        for state in sorted(self.runs_dir.glob("*/state.json")):
            try:
                saved = json.loads(state.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            run = Run(saved["id"], state.parent, saved.get("request", {}))
            for key in ("status", "phase", "chat_id", "chat_url", "rounds", "result",
                        "error", "started_at", "finished_at", "log"):
                if key in saved:
                    setattr(run, key, saved[key])
            if run.status not in ("done", "failed", "stopped"):
                run.status, run.error = "failed", "brickpipe was restarted while this run was in progress"
            self.runs[run.id] = run

    def start(self, request: dict) -> Run:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        run = Run(run_id, self.runs_dir / run_id, request)
        run.dir.mkdir(parents=True)
        self.runs[run_id] = run
        threading.Thread(target=self._execute, args=(run,), daemon=True).start()
        return run

    def _execute(self, run: Run) -> None:
        req = run.request
        cfg = self.settings
        if cfg.get("locked"):
            # Hosted: the server decides where Nova and the libraries are; the
            # browser's values are ignored so nobody can point it elsewhere.
            req["nova_url"], req["nova_db"], req["ldraw_dir"] = cfg["nova_url"], cfg["nova_db"], cfg.get("ldraw_dir") or ""
        agent = nova.Client(req.get("nova_url") or nova.DEFAULT_URL, public_url=cfg.get("nova_public_url"))
        key = req.get("rebrickable_key") or cfg.get("rebrickable_key") or ""
        if key and not req.get("rebrickable_key"):
            req["rebrickable_key"] = key  # the server's key; public() never shows it
        owned = inventory.from_text(req["owned_text"], load_colours()) if req.get("owned_text") else None
        user_token = (req.get("rebrickable_user_token") or "").strip() or None
        client = rebrickable.Client(key=key, cache_dir=rebrickable.default_cache_dir()) if key else None
        nova_db = Path(req["nova_db"]) if req.get("nova_db") else None
        ldraw_dir = Path(req["ldraw_dir"]) if req.get("ldraw_dir") else None
        creds = bricklink.Credentials.from_dict(req.get("bricklink") or {}) if features.enabled("prices") else None
        pricer = bricklink.Client(creds, cache_dir=bricklink.default_cache_dir()) if creds else None
        price_options = {
            "guide_type": "sold" if req.get("price_guide") == "sold" else "stock",
            "new_or_used": "U" if req.get("price_condition") == "U" else "N",
            "region": req.get("price_region") or "", "currency": req.get("price_currency") or "",
        }

        def set_status(status: str, phase: str = "") -> None:
            run.status, run.phase = status, phase
            run.emit("status", {"status": status, "phase": phase})

        last_text_emit = [0.0]
        text_pending = [False]

        def flush_text() -> None:
            if text_pending[0]:
                text_pending[0] = False
                last_text_emit[0] = time.monotonic()
                run.emit("agent_text", {"text": run.agent_text[-8000:]})
        last_output_emit: dict = {}

        def on_event(event: str, data: dict) -> None:
            run.events_seen += 1
            if event != "text":
                flush_text()  # the agent stopped talking; show the last fragment
            if event == "progress" and data.get("summary"):
                run.phase = str(data["summary"])[:200]
                run.note(f"Nova: {run.phase}")
                run.emit("phase", {"phase": run.phase})
            elif event == "tool_start":
                name = data.get("name", "")
                entry = {"id": data.get("id") or f"t{len(run.activity)}", "name": name,
                         "label": tool_label(name), "detail": tool_detail(name, data.get("arguments")),
                         "output": "", "done": False, "at": time.strftime("%H:%M:%S")}
                run.activity.append(entry)
                run.activity = run.activity[-200:]
                run.tools_running[entry["id"]] = entry
                run.phase = nova_phase(name)
                run.note(f"{entry['label']}: {entry['detail']}"[:300])
                run.emit("phase", {"phase": run.phase})
                run.emit("activity", entry)
            elif event == "tool_output":
                entry = run.tools_running.get(data.get("id"))
                if entry is not None and data.get("delta"):
                    entry["output"] = (entry["output"] + str(data["delta"]))[-3000:]
                    now = time.monotonic()
                    if now - last_output_emit.get(entry["id"], 0.0) > 0.5:
                        last_output_emit[entry["id"]] = now
                        run.emit("activity", entry)
            elif event == "tool_end":
                entry = run.tools_running.pop(data.get("id"), None)
                if entry is not None:
                    entry["done"] = True
                    if entry["name"] == "view_image":
                        try:
                            entry["images"] = agent.latest_images(run.chat_id)
                        except nova.NovaError:
                            pass
                    run.emit("activity", entry)
            elif event == "text" and data.get("delta"):
                run.agent_text = (run.agent_text + str(data["delta"]))[-8000:]
                text_pending[0] = True
                if time.monotonic() - last_text_emit[0] > 0.4 or "\n" in data["delta"]:
                    flush_text()
            elif event == "model":
                run.note(f"Nova published model: {data.get('name', '')}")
            elif event == "turn_error":
                run.note(f"Nova reported an error: {data.get('message', '')}")

        def produced() -> dict[str, nova.ProducedModel]:
            try:
                return {m.file: m for m in agent.models(run.chat_id)}
            except nova.NovaError:
                return {}

        def model_images() -> dict[str, str]:
            return {f: agent.absolute(m.image_url) for f, m in produced().items() if m.image_url}

        def decorate(entry: dict, models: dict[str, nova.ProducedModel]) -> None:
            m = models.get(entry["model"])
            entry["image"] = entry.get("image") or (agent.absolute(m.image_url) if m and m.image_url else None)
            entry.update(viewer_urls(agent, m))

        def on_round(rnd: loop.Round) -> None:
            if rnd.sent and not rnd.turn:
                run.agent_text = ""  # a new turn is starting
                run.emit("agent_text", {"text": ""})
            entry = round_dict(rnd)
            decorate(entry, produced())
            run.rounds = [r for r in run.rounds if r["number"] != entry["number"]] + [entry]
            run.rounds.sort(key=lambda r: r["number"])
            set_status("repairing" if rnd.sent and not rnd.turn else "checking", run.phase)
            run.emit("round", entry)

        try:
            model, chat_id = self._starting_model(run, agent, set_status, on_event)
            set_status("checking")
            outcome = loop.run(
                agent, chat_id, model, run.dir,
                max_rounds=int(req.get("rounds") or 3), approve_tools=True,
                log=run.note, on_event=on_event, on_round=on_round,
                stop=lambda: run.stop_requested,
                nova_db=nova_db, check_availability=bool(client), client=client,
                owned=owned, owned_rebrickable_token=user_token if client else None,
                ldraw_dir=ldraw_dir, pricer=pricer, price_options=price_options,
            )
            last = next((r for r in reversed(outcome.rounds) if r.result), None)
            run.result = {
                "clean": outcome.clean, "reason": outcome.stopped_because,
                "final_model": outcome.final_model.name if outcome.final_model else None,
                "owned": last.result.owned if last else None,
                "price": last.result.price if last else None,
                "connectivity": last.result.connectivity if last else None,
                "tokens": tokens_sum([run.design_tokens, *(r.get("tokens") for r in run.rounds)]),
            }
            run.status = "stopped" if run.stop_requested else "done"
            models = produced()  # snapshots may have finished rendering since
            for entry in run.rounds:
                decorate(entry, models)
        except (nova.NovaError, PipelineError, rebrickable.RebrickableError) as err:
            run.error = str(err)
            run.status = "failed"
            run.note(f"Failed: {err}")
        except Exception as err:  # noqa: BLE001 - surface anything to the page
            run.error = f"{type(err).__name__}: {err}"
            run.status = "failed"
            run.note(f"Failed: {run.error}")
        run.finished_at = time.time()
        run.phase = ""
        run.emit("finished", run.public())

    def _starting_model(self, run: Run, agent: nova.Client, set_status, on_event) -> tuple[Path, str]:
        """Resolve the request into (model path, chat id), designing first if asked."""
        req = run.request
        models_dir = run.dir / "models"
        if req.get("prompt"):
            set_status("designing", "Starting a new Nova chat")
            chat = agent.create_chat()
            run.chat_id, run.chat_url = chat["id"], agent.chat_url(chat["id"])
            run.emit("chat", {"chat_id": run.chat_id, "chat_url": run.chat_url})
            run.note(f"Asked Nova to design: {req['prompt'][:120]}")
            run.agent_text = ""
            agent.send(chat["id"], req["prompt"] + DESIGN_MESSAGE_SUFFIX,
                       options={"mode": "agent", "permissions": "full"})
            set_status("designing", "The agent is designing the model")
            turn = agent.wait_for_turn(chat["id"], approve_tools=True, on_event=on_event,
                                       stop=lambda: run.stop_requested)
            if turn.error:
                raise nova.NovaError(f"Design turn failed: {turn.error}")
            produced = agent.models(chat["id"])
            if not produced:
                raise nova.NovaError(
                    "The agent finished without delivering a model. Open the chat in "
                    "Nova to see what it said."
                )
            path = agent.download(produced[-1], models_dir)
            run.design_tokens = tokens_dict(turn.usage)
            run.note(f"Nova delivered {path.name} in {int(turn.seconds)} s")
            return path, chat["id"]

        chat_id = req.get("chat_id") or "latest"
        chat = agent.find_chat(chat_id)
        run.chat_id, run.chat_url = chat["id"], agent.chat_url(chat["id"])
        run.emit("chat", {"chat_id": run.chat_id, "chat_url": run.chat_url})
        if req.get("model_file"):
            wanted = req["model_file"]
            for m in agent.generated_models():
                if m.get("file") == wanted and m.get("model_url"):
                    pm = nova.ProducedModel(m.get("id", wanted), m.get("name", ""), wanted,
                                            m["model_url"].split("?", 1)[0], 0.0, m.get("parts"))
                    return agent.download(pm, models_dir), chat["id"]
            raise nova.NovaError(f"Nova has no model file named {wanted}")
        produced = agent.models(chat["id"])
        if not produced:
            raise nova.NovaError("That chat has not produced a model yet.")
        return agent.download(produced[-1], models_dir), chat["id"]


def tool_label(tool: str) -> str:
    return {
        "run_shell": "Ran shell", "run_python": "Ran Python", "run_toolkit": "Ran Nova toolkit",
        "publish_model": "Published model", "view_image": "Viewed image", "read_file": "Read file",
        "write_file": "Wrote file", "list_files": "Listed files",
    }.get(tool, tool or "Tool")


def tool_detail(tool: str, arguments) -> str:
    """The one argument worth showing for a tool call, shortened."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        return ""
    if tool == "run_toolkit":
        value = " ".join(str(a) for a in args.get("arguments", []))
    else:
        value = args.get("command") or args.get("code") or args.get("path") or ""
    value = " ".join(str(value).split())
    return value[:220] + ("…" if len(value) > 220 else "")


def nova_phase(tool: str) -> str:
    return {
        "run_toolkit": "Running LDraw Nova tools",
        "run_python": "Running the model generator",
        "run_shell": "Running a build command",
        "publish_model": "Checking and publishing the model",
        "view_image": "Reviewing a rendered image",
        "read_file": "Reading files",
        "write_file": "Saving build files",
        "list_files": "Looking at files",
    }.get(tool, "Working")


# -- HTTP ------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    runner: Runner
    settings: dict
    page: bytes

    def log_message(self, fmt, *args):  # quiet
        pass

    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def run_for(self, run_id: str) -> Run | None:
        return self.runner.runs.get(run_id)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.page)))
            self.end_headers()
            self.wfile.write(self.page)
            return
        if path == "/api/settings":
            found = connectivity.find_ldraw_library()
            shown = {k: v for k, v in self.settings.items() if k != "rebrickable_key"}
            shown["has_server_key"] = bool(self.settings.get("rebrickable_key"))
            return self.send_json({**shown, "ldraw_dir": str(found or ""),
                                   "ldraw_download": self.ldraw_download,
                                   "features": features.all_flags()})
        if path == "/api/runs":
            runs = sorted(self.runner.runs.values(), key=lambda r: r.started_at, reverse=True)
            return self.send_json({"runs": [r.public() for r in runs[:50]]})
        if path == "/api/nova/models":
            return self.proxy_nova(lambda agent: {"models": [
                {k: m.get(k) for k in ("file", "name", "parts", "image_url", "chats")}
                for m in agent.generated_models() if not m.get("gallery")
            ]})
        if path == "/api/nova/chats":
            return self.proxy_nova(lambda agent: {"chats": [
                {"id": c["id"], "title": c.get("title", ""), "updated_at": c.get("updated_at")}
                for c in agent.chats()
            ]})
        m = re.fullmatch(r"/api/runs/([\w-]+)", path)
        if m:
            run = self.run_for(m[1])
            return self.send_json(run.public()) if run else self.send_json({"error": "no such run"}, 404)
        m = re.fullmatch(r"/api/runs/([\w-]+)/events", path)
        if m:
            return self.stream(m[1])
        m = re.fullmatch(r"/api/runs/([\w-]+)/files/([\w.-]+)", path)
        if m:
            return self.send_file(m[1], m[2])
        self.send_json({"error": "not found"}, 404)

    ldraw_download: dict = {"state": "idle"}

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rebrickable/login":
            body = self.read_json()
            try:
                client = rebrickable.Client(key=body.get("api_key") or self.settings.get("rebrickable_key") or "")
                token = client.sign_in(body.get("username") or "", body.get("password") or "")
            except rebrickable.RebrickableError as err:
                return self.send_json({"error": str(err)}, 400)
            return self.send_json({"user_token": token})
        if path == "/api/ldraw/download":
            return self.start_ldraw_download()
        if path == "/api/runs":
            body = self.read_json()
            if not (body.get("prompt") or body.get("chat_id") or body.get("model_file")):
                return self.send_json({"error": "Give a prompt, or pick a model or chat."}, 400)
            if self.settings.get("locked"):
                body.pop("rebrickable_key", None) if self.settings.get("rebrickable_key") else None
            run = self.runner.start(body)
            return self.send_json(run.public(), 202)
        m = re.fullmatch(r"/api/runs/([\w-]+)/stop", path)
        if m:
            run = self.run_for(m[1])
            if not run:
                return self.send_json({"error": "no such run"}, 404)
            run.stop_requested = True
            run.note("Stop requested; finishing the current step")
            return self.send_json({"stopping": True})
        self.send_json({"error": "not found"}, 404)

    def start_ldraw_download(self):
        state = type(self).ldraw_download
        if state.get("state") == "running":
            return self.send_json(state, 202)
        state.update(state="running", done=0, total=0, error=None, path=None)

        def progress(done, total):
            state.update(done=done, total=total)

        def work():
            try:
                path = connectivity.download_library(progress=progress)
                state.update(state="done", path=str(path))
                self.settings["ldraw_dir"] = str(path)
            except Exception as err:  # noqa: BLE001
                state.update(state="failed", error=str(err))

        threading.Thread(target=work, daemon=True).start()
        return self.send_json(state, 202)

    def proxy_nova(self, fn):
        nova_url = parse_qs(urlparse(self.path).query).get("nova_url", [nova.DEFAULT_URL])[0]
        if self.settings.get("locked"):
            nova_url = self.settings["nova_url"]
        try:
            return self.send_json(fn(nova.Client(nova_url, public_url=self.settings.get("nova_public_url"))))
        except nova.NovaError as err:
            return self.send_json({"error": str(err)}, 502)

    def send_file(self, run_id: str, name: str) -> None:
        run = self.run_for(run_id)
        folder = run.final_dir() if run else None
        target = (folder / name) if folder else None
        if not target or not target.is_file() or target.parent != folder:
            return self.send_json({"error": "no such file"}, 404)
        data = target.read_bytes()
        ctype = mimetypes.guess_type(name)[0] or "text/plain"
        if name.endswith((".md", ".ldr", ".mpd", ".xml", ".csv")):
            ctype = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if "download" in parse_qs(urlparse(self.path).query):
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        self.wfile.write(data)

    def stream(self, run_id: str) -> None:
        run = self.run_for(run_id)
        if not run:
            return self.send_json({"error": "no such run"}, 404)
        q: queue.Queue = queue.Queue(maxsize=500)
        with run.lock:
            run.subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.write_event("snapshot", run.public())
            while True:
                try:
                    event, data = q.get(timeout=15)
                except queue.Empty:
                    self.write_event("ping", {})
                    continue
                self.write_event(event, data)
                if event == "finished":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with run.lock:
                if q in run.subscribers:
                    run.subscribers.remove(q)

    def write_event(self, event: str, data) -> None:
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()


def make_server(port: int = DEFAULT_PORT, runs_dir: Path | None = None,
                nova_url: str = nova.DEFAULT_URL, nova_db: str | None = None,
                host: str = "127.0.0.1", nova_public_url: str | None = None,
                locked: bool = False, rebrickable_key: str | None = None) -> ThreadingHTTPServer:
    """`host` 0.0.0.0 makes the page reachable from other machines; combine it
    with `locked` so the server's Nova and library settings cannot be changed
    from a browser, and `nova_public_url` for the address browsers use to
    reach Nova when it differs from `nova_url`."""
    here = Path.cwd()
    handler = type("BoundHandler", (Handler,), {})
    from . import __version__
    handler.settings = {
        "version": __version__,
        "nova_url": nova_url,
        "nova_public_url": (nova_public_url or "").rstrip("/") or None,
        "nova_db": nova_db if nova_db is not None else guess_nova_db(here),
        "ldraw_dir": str(connectivity.find_ldraw_library() or ""),
        "rounds": 3,
        "locked": bool(locked),
        "rebrickable_key": rebrickable_key or None,
    }
    handler.runner = Runner(runs_dir or here / "runs", handler.settings)
    handler.page = resources.files("brickpipe").joinpath("web/index.html").read_bytes()
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import webbrowser

    env = os.environ.get
    parser = argparse.ArgumentParser(prog="brickpipe.web", description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=int(env("BRICKPIPE_PORT") or DEFAULT_PORT))
    parser.add_argument("--host", default=env("BRICKPIPE_HOST") or "127.0.0.1",
                        help="address to listen on; 0.0.0.0 for other machines (default: this machine only)")
    parser.add_argument("--nova-url", default=env("BRICKPIPE_NOVA_URL") or nova.DEFAULT_URL)
    parser.add_argument("--nova-public-url", default=env("BRICKPIPE_NOVA_PUBLIC_URL") or None,
                        help="address browsers use to open Nova, when it differs from --nova-url")
    parser.add_argument("--nova-db", default=env("BRICKPIPE_NOVA_DB") or None, help="path to Nova's data/ldraw-info.db")
    parser.add_argument("--runs-dir", type=Path, default=Path(env("BRICKPIPE_RUNS_DIR")) if env("BRICKPIPE_RUNS_DIR") else None,
                        help="where runs are kept (default ./runs)")
    parser.add_argument("--locked", action="store_true", default=env("BRICKPIPE_LOCKED", "").lower() in ("1", "true", "yes"),
                        help="hosted mode: browsers cannot change the Nova or library settings")
    parser.add_argument("--get-ldraw", action="store_true", default=env("BRICKPIPE_GET_LDRAW", "").lower() in ("1", "true", "yes"),
                        help="download the LDraw library at start if none is found")
    parser.add_argument("--no-browser", action="store_true", default=env("BRICKPIPE_NO_BROWSER", "").lower() in ("1", "true", "yes"))
    args = parser.parse_args(argv)

    if args.get_ldraw and not connectivity.find_ldraw_library():
        print("Downloading the LDraw part library (about 150 MB)…", flush=True)
        try:
            print(f"Saved {connectivity.download_library()}")
        except OSError as err:
            print(f"brickpipe: library download failed: {err}; the connectivity check stays off until it exists.")

    try:
        server = make_server(args.port, args.runs_dir, args.nova_url, args.nova_db, host=args.host,
                             nova_public_url=args.nova_public_url, locked=args.locked,
                             rebrickable_key=env(rebrickable.KEY_ENV))
    except OSError as err:
        print(f"brickpipe: could not listen on port {args.port} ({err}).\n"
              "Another brickpipe page is probably still running: close its window, "
              "or start this one with --port 8767.")
        return 1
    url = f"http://localhost:{args.port}"
    from . import __version__
    print(f"brickpipe {__version__} is at {url}" + (f" (listening on {args.host})" if args.host != "127.0.0.1" else "")
          + ("  [locked: hosted mode]" if args.locked else "") + "  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
