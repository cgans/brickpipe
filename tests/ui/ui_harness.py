"""Shared pieces for the browser tests: a static server for web/index.html, stubbed API
responses, and a unittest base class that gives every test a fresh browser context
(so each one starts with empty localStorage, like a first visit).

The whole suite skips cleanly when Playwright or its Chromium isn't installed.
"""
import copy
import http.server
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright  # noqa: F401 (expect is re-exported to the tests)
except ImportError:  # pragma: no cover - depends on the machine
    raise unittest.SkipTest("playwright is not installed: pip install playwright && playwright install chromium")

WEB = Path(__file__).resolve().parents[2] / "brickpipe" / "web"

MODELS = [
    {"id": "m-anthropic", "name": "Claude Sonnet", "model": "anthropic/claude-sonnet", "auth_mode": None},
]
CATALOG = [
    {"model": "anthropic/claude-sonnet", "name": "Claude Sonnet"},
    {"model": "openai/gpt-5", "name": "GPT-5"},
    {"model": "openrouter/vendor/some-model", "name": "Some Model"},
]
NOVA_MODELS = [
    {"file": "shed.mpd", "name": "Shed", "parts": 150, "chats": [{"id": "c1"}]},
    {"file": "tower.mpd", "name": "Tower", "parts": 80, "chats": [{"id": "c2"}]},
]
NOVA_CHATS = [{"id": "c1", "title": "Shed chat"}, {"id": "c2", "title": "Tower chat"}]


def finished_run(request):
    """The smallest run object the page can render."""
    return {"id": "r1", "status": "done", "phase": "", "chat_id": None, "chat_url": None, "rounds": [],
            "result": {"clean": True}, "error": None, "started_at": 1.0, "finished_at": 2.0, "log": [],
            "request": {**request, "availability": False}, "agent_text": "", "events_seen": 0,
            "activity": [], "design_tokens": None, "refinements": [], "rerun_of": None, "rerun_as": None, "files": []}


class State:
    """What the stubbed API answers, plus every request body the page sent."""

    def __init__(self):
        self.settings = {"version": "0.0-test", "features": {}, "nova_url": "http://nova.test", "nova_db": "",
                         "rounds": 3, "locked": True, "has_server_key": False, "host_name": "", "ldraw_dir": "",
                         "ldraw_download": {}}
        self.accounts = {"default_id": "m-anthropic", "models": copy.deepcopy(MODELS)}
        self.catalog = {"models": copy.deepcopy(CATALOG)}
        self.posts = []  # (path, parsed body)

    def posted(self, path):
        return [body for p, body in self.posts if p == path]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class UiCase(unittest.TestCase):
    viewport = {"width": 1100, "height": 900}
    context_options = {}
    seen_setup = True  # pre-set bp_setup_done so the first-visit dialog stays out of the way

    @classmethod
    def setUpClass(cls):
        # Serve a copy of index.html only, so an owner's local easter/nedry.mp3 never reaches the tests.
        cls._dir = tempfile.TemporaryDirectory()
        shutil.copy(WEB / "index.html", cls._dir.name)
        handler = lambda *a, **k: _Quiet(*a, directory=cls._dir.name, **k)  # noqa: E731
        cls._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls._server.server_address[1]}"
        threading.Thread(target=cls._server.serve_forever, daemon=True).start()
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch()
        except Exception as err:  # noqa: BLE001
            cls._pw.stop()
            cls._server.shutdown(); cls._server.server_close()
            cls._dir.cleanup()
            raise unittest.SkipTest(f"Chromium isn't available ({str(err).splitlines()[0]}): playwright install chromium")

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()
        cls._server.shutdown(); cls._server.server_close()
        cls._dir.cleanup()

    def setUp(self):
        self.state = State()
        self.configure()
        self.ctx = self._browser.new_context(viewport=self.viewport, **self.context_options)
        self.ctx.route("**/api/**", self._api)
        if self.seen_setup:
            self.ctx.add_init_script("try { if (!localStorage.getItem('bp_setup_done')) localStorage.setItem('bp_setup_done', '1'); } catch (e) {}")
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.requests = []
        self.page.on("request", lambda r: self.requests.append(r.url))
        self.addCleanup(self.ctx.close)

    def tearDown(self):
        self.assertEqual(self.errors, [], "the page raised JavaScript errors")

    def configure(self):
        """Tests override this to change self.state before the page loads."""

    def _api(self, route):
        req = route.request
        path = req.url.split("?")[0].split("/api", 1)[1]
        path = "/api" + path
        st = self.state
        body = None
        if req.method == "POST":
            body = json.loads(req.post_data or "{}")
            st.posts.append((path, body))

        def ok(data, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))
        if path == "/api/settings":
            return ok(st.settings)
        if path == "/api/nova/llm-models" and req.method == "GET":
            return ok(st.accounts)
        if path == "/api/nova/llm-models":
            return ok({"id": "k-" + body["model"].split("/")[0], "token": "tok"})
        if path == "/api/nova/model-catalog":
            return ok(st.catalog)
        if path == "/api/nova/models":
            return ok({"models": NOVA_MODELS})
        if path == "/api/nova/chats":
            return ok({"chats": NOVA_CHATS})
        if path == "/api/rebrickable/login":
            return ok({"user_token": "user-tok"})
        if path == "/api/runs" and req.method == "GET":
            return ok({"runs": [], "total": 0})
        if path == "/api/runs":
            return ok(finished_run(body))
        ok({"error": "not stubbed: " + path}, 404)

    def open(self, hash_=""):
        self.page.goto(self.base + "/" + hash_)
        self.page.wait_for_function("document.getElementById('version').textContent !== ''")
        return self.page

    def local(self, key):
        return self.page.evaluate("k => localStorage.getItem(k)", key)
