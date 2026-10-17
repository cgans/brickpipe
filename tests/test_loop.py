"""The repair loop against a stand-in Nova server.

FakeNova answers the same routes as ldraw-nova-docker's backend and plays an
agent that, when sent a repair request, removes a duplicate piece from the
model and delivers the result. Nothing here touches the network.
"""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from brickpipe import cli, ldraw, loop, nova

BROKEN = (
    "0 FILE house.ldr\n"
    "0 // base: lower brick\n"
    "1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
    "0 // base-copy: same spot by mistake\n"
    "1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
    "0 // roof\n"
    "1 15 0 -48 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
)
FIXED = BROKEN.replace("0 // base-copy: same spot by mistake\n1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n", "")


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeNova:
    """Serves Nova's API. `repairs` controls what the agent delivers."""

    def __init__(self, repairs=True, deliver=True, fail_turn=False):
        self.repairs, self.deliver, self.fail_turn = repairs, deliver, fail_turn
        self.chats = {"20261014-220000-abc123": {
            "id": "20261014-220000-abc123", "title": "BRAT", "updated_at": 2.0, "created_at": 1.0}}
        self.models = {"20261014-220000-abc123": [
            {"id": "m1", "name": "v1", "file": "v1.mpd", "model_url": "/files/generated/v1.mpd?v=1",
             "created_at": 1.0, "parts": 3}]}
        self.files = {"/files/generated/v1.mpd": BROKEN}
        self.messages = []
        self.approvals = []
        self.running = False
        self.pending_approval = None

    def __call__(self, req):
        from urllib.parse import urlsplit
        parts = urlsplit(req.full_url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        if path == "/api/chats" and method == "GET":
            return self.json({"chats": list(self.chats.values())})
        if path == "/api/chats" and method == "POST":
            cid = f"new-{len(self.chats)}"
            self.chats[cid] = {"id": cid, "title": "New chat", "updated_at": 9.0, "created_at": 9.0}
            self.models[cid] = []
            return self.json(self.chats[cid])
        if path == "/api/models":
            return self.json({"models": [dict(m, gallery=False) for ms in self.models.values() for m in ms],
                              "pending": 0})
        if path.startswith("/api/chats/") and path.endswith("/messages"):
            chat_id = path.split("/")[3]
            self.messages.append(body)
            self.running = True
            if body["options"].get("permissions") == "ask":
                self.pending_approval = {"id": "a1", "name": "write_file", "arguments": "{}"}
            return self.json({"started": True}, status=202)
        if path.startswith("/api/chats/") and "/approvals/" in path:
            self.approvals.append(path.rsplit("/", 1)[1])
            self.pending_approval = None
            return self.json({"accepted": True})
        if path.startswith("/api/chats/") and path.endswith("/stream"):
            return self.stream(path.split("/")[3])
        if path.startswith("/api/chats/"):
            chat_id = path.split("/")[3]
            if chat_id not in self.chats:
                raise urllib.error.HTTPError(req.full_url, 404, "no such chat", {}, io.BytesIO(b""))
            return self.json({"chat": {**self.chats[chat_id], "running": self.running},
                              "messages": [],
                              "models": {m["id"]: m for m in self.models[chat_id]}})
        if path.split("?")[0] in self.files:
            return Response(self.files[path.split("?")[0]].encode())
        raise AssertionError(f"unexpected {method} {path}")

    def stream(self, chat_id):
        events = []
        if self.pending_approval:
            events.append(("approval", self.pending_approval))
            # an approval is pending until the client grants it; the stream
            # ends here and the client reconnects
            events.append(("reconnect", {}))
            return self.sse(events)
        if self.fail_turn:
            events.append(("turn_error", {"message": "provider rate limit"}))
        elif self.deliver:
            last = self.messages[-1] if self.messages else {}
            text = FIXED if (self.repairs and last.get("documents")) else BROKEN
            n = len(self.models[chat_id]) + 1
            self.files[f"/files/generated/v{n}.mpd"] = text
            self.models[chat_id].append({
                "id": f"m{n}", "name": f"v{n}", "file": f"v{n}.mpd",
                "model_url": f"/files/generated/v{n}.mpd?v={n}",
                "created_at": 10.0 ** 12, "parts": 2 if self.repairs else 3})
        events.append(("usage", {"step": {"input": 1000, "output": 10, "cache_read": 0, "cache_write": 0},
                                 "total": {"input": 41000, "output": 400, "cache_read": 19000, "cache_write": 2000,
                                           "steps": 2, "cost_usd": 0.12, "model": "fake"}}))
        events.append(("done", {}))
        self.running = False
        return self.sse(events)

    @staticmethod
    def sse(events):
        text = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
        return Response(text.encode())

    @staticmethod
    def json(data, status=200):
        return Response(json.dumps(data).encode())


def run(fake, tmp, **kw):
    model = Path(tmp) / "house.mpd"
    model.write_text(BROKEN)
    client = nova.Client("http://localhost:8765", opener=fake)
    return loop.run(client, "latest", model, Path(tmp) / "out", log=lambda s: None, **kw)


class LoopTests(unittest.TestCase):
    def test_one_round_fixes_the_model(self):
        fake = FakeNova()
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run(fake, tmp)
            self.assertTrue(outcome.clean)
            self.assertEqual([r.sent for r in outcome.rounds], [True, False])
            self.assertEqual(outcome.rounds[0].result.errors, 1)
            self.assertEqual(outcome.rounds[1].result.errors, 0)
            self.assertEqual(outcome.final_model.name, "v2.mpd")
            pieces, _ = ldraw.flatten(ldraw.load(outcome.final_model))
            self.assertEqual(len(pieces), 2)
            self.assertTrue((Path(tmp) / "out" / "loop.md").is_file())
            self.assertTrue((Path(tmp) / "out" / "round-1" / "feedback.md").is_file())
            self.assertFalse((Path(tmp) / "out" / "round-2" / "feedback.md").exists())

        sent = fake.messages[0]
        self.assertEqual(sent["options"], {"mode": "agent", "permissions": "full"})
        self.assertEqual(sent["documents"][0]["name"], "feedback.md")
        self.assertTrue(sent["documents"][0]["data"].startswith("data:"))
        self.assertIn("repair request", sent["text"])

    def test_gives_up_after_round_limit(self):
        fake = FakeNova(repairs=False)
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run(fake, tmp, max_rounds=2)
        self.assertFalse(outcome.clean)
        self.assertIn("limit of 2", outcome.stopped_because)
        self.assertEqual(len(fake.messages), 2)
        self.assertEqual(len(outcome.rounds), 3)

    def test_agent_delivers_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run(FakeNova(deliver=False), tmp)
        self.assertFalse(outcome.clean)
        self.assertIn("without delivering", outcome.stopped_because)

    def test_turn_error_stops_the_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run(FakeNova(fail_turn=True), tmp)
        self.assertIn("rate limit", outcome.stopped_because)

    def test_ask_mode_still_grants_streamed_approvals_when_told_to(self):
        fake = FakeNova()
        client = nova.Client("http://localhost:8765", opener=fake)
        client.send("20261014-220000-abc123", "hi", options={"permissions": "ask"})
        turn = client.wait_for_turn("20261014-220000-abc123", approve_tools=True)
        self.assertTrue(turn.finished)
        self.assertEqual(fake.approvals, ["a1"])

    def test_unknown_chat_is_a_clear_error(self):
        client = nova.Client("http://localhost:8765", opener=FakeNova())
        with self.assertRaisesRegex(nova.NovaError, "No Nova chat matches"):
            client.find_chat("nope")

    def test_cli_entry(self):
        fake = FakeNova()
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "house.mpd"
            model.write_text(BROKEN)
            # cli builds its own nova.Client; swap the opener in for the test
            original = nova.Client.__init__

            def patched(self, base_url=nova.DEFAULT_URL, opener=None):
                original(self, base_url, fake)
            nova.Client.__init__ = patched
            try:
                code = cli.main([str(model), "-o", str(Path(tmp) / "out", ), "--nova-chat", "latest"])
            finally:
                nova.Client.__init__ = original
            self.assertEqual(code, 0)
            self.assertIn("clean", (Path(tmp) / "out" / "loop.md").read_text())


if __name__ == "__main__":
    unittest.main()
