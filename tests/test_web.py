"""The web app's API against a stand-in Nova (the FakeNova from test_loop)."""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from brickpipe import nova, web
from test_loop import BROKEN, FakeNova


def http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fake = FakeNova()
        cls.original_init = nova.Client.__init__

        def patched(self, base_url=nova.DEFAULT_URL, opener=None, public_url=None):
            cls.original_init(self, base_url, cls.fake, public_url)
        nova.Client.__init__ = patched
        cls.server = web.make_server(0, Path(cls.tmp.name) / "runs", nova_db="")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        nova.Client.__init__ = cls.original_init
        cls.tmp.cleanup()

    def wait_done(self, run_id):
        for _ in range(100):
            status, run = http("GET", f"{self.base}/api/runs/{run_id}")
            if run["status"] in ("done", "failed", "stopped"):
                return run
            time.sleep(0.05)
        self.fail("run did not finish")

    def test_page_and_settings(self):
        with urllib.request.urlopen(f"{self.base}/") as r:
            self.assertIn(b"<title>brickpipe</title>", r.read())
        status, settings = http("GET", f"{self.base}/api/settings")
        self.assertEqual(settings["rounds"], 3)
        self.assertIn("prices", settings["features"])

    def test_nova_listing(self):
        status, body = http("GET", f"{self.base}/api/nova/chats")
        self.assertIn("BRAT", [c["title"] for c in body["chats"]])  # other tests add chats to the shared fake

    def test_empty_request_rejected(self):
        status, body = http("POST", f"{self.base}/api/runs", {})
        self.assertEqual(status, 400)
        self.assertIn("prompt", body["error"])

    def test_run_from_latest_chat(self):
        status, run = http("POST", f"{self.base}/api/runs",
                           {"chat_id": "20261014-220000-abc123", "model_file": "v1.mpd", "rounds": 2})
        self.assertEqual(status, 202)
        run = self.wait_done(run["id"])
        self.assertEqual(run["status"], "done")
        self.assertTrue(run["result"]["clean"])
        self.assertEqual([r["errors"] for r in run["rounds"]], [1, 0])
        self.assertEqual(run["chat_url"], "http://localhost:8765/chat/20261014-220000-abc123")
        self.assertEqual(run["rounds"][0]["tokens"]["input"], 41000)  # from Nova's usage event
        self.assertEqual(run["result"]["tokens"], {"input": 41000, "output": 400, "cache_read": 19000,
                                                   "cache_write": 2000, "steps": 2, "cost_usd": 0.12, "turns": 1})
        self.assertIn("bricklink_wanted.xml", run["files"])
        self.assertFalse(run["request"]["availability"])
        self.assertNotIn("rebrickable_key", run["request"])
        with urllib.request.urlopen(f"{self.base}/api/runs/{run['id']}/files/parts.csv") as r:
            self.assertIn(b"3001", r.read())
        status, _ = http("GET", f"{self.base}/api/runs/{run['id']}/files/../state.json")
        self.assertEqual(status, 404)

    def test_run_from_prompt_creates_a_chat(self):
        chats_before = len(self.fake.chats)
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed"})
        run = self.wait_done(run["id"])
        self.assertEqual(run["status"], "done", run.get("error"))
        self.assertEqual(len(self.fake.chats), chats_before + 1)
        first = self.fake.messages[-2]  # the design message, then the repair
        self.assertTrue(first["text"].startswith("a shed"))
        self.assertEqual(run["design_tokens"]["input"], 41000)
        self.assertEqual(run["result"]["tokens"]["turns"], 2)  # design turn + one repair
        self.assertIn("checked for parts that cannot be bought", first["text"])

    def test_bricklink_secrets_stay_out_of_state_and_api(self):
        from brickpipe import bricklink
        from test_prices import FakeBrickLink, guide
        fake = FakeBrickLink({})
        original = bricklink.Client.__init__

        def patched(self, creds=None, cache_dir=None, fetch=None, sleep=None):
            assert creds.token_secret == "ts-secret"
            original(self, fetch=fake, sleep=lambda s: None)
        bricklink.Client.__init__ = patched
        import os
        body = {"chat_id": "latest", "rounds": 0, "price_region": "eu",
                "bricklink": {"consumer_key": "ck", "consumer_secret": "cs-secret",
                              "token": "tk", "token_secret": "ts-secret"}}
        try:
            os.environ.pop("BRICKPIPE_FEATURES", None)
            status, off = http("POST", f"{self.base}/api/runs", body)
            off = self.wait_done(off["id"])
            self.assertIsNone(off["result"]["price"])  # flag off: ignored even with values
            self.assertFalse(fake.calls)
            os.environ["BRICKPIPE_FEATURES"] = "prices"
            status, run = http("POST", f"{self.base}/api/runs", body)
            run = self.wait_done(run["id"])
        finally:
            bricklink.Client.__init__ = original
            os.environ.pop("BRICKPIPE_FEATURES", None)
        self.assertTrue(run["request"]["prices"])
        self.assertNotIn("bricklink", run["request"])
        self.assertIn("price", run["result"])
        self.assertEqual(run["result"]["price"]["region"], "eu")
        state = (Path(self.tmp.name) / "runs" / run["id"] / "state.json").read_text()
        self.assertNotIn("secret", state)
        self.assertTrue(fake.calls)  # the run really asked for prices
        self.assertIn("prices.csv", run["files"])

    def test_own_ai_key_is_registered_in_nova_and_used_for_the_run(self):
        status, acc = http("GET", f"{self.base}/api/nova/llm-models")
        self.assertEqual(acc["default_id"], "host")
        self.assertEqual(acc["models"][0]["name"], "Chris's Claude")
        self.assertNotIn("litellm_params", json.dumps(acc))  # keys never reach the page, masked or not
        status, cat = http("GET", f"{self.base}/api/nova/model-catalog")
        self.assertIn("anthropic/claude-sonnet-5", [m["model"] for m in cat["models"]])
        status, made = http("POST", f"{self.base}/api/nova/llm-models",
                            {"model": "openai/gpt-6-luna", "api_key": "sk-family", "name": "gpt-6-luna (own key)"})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.llm_entries[made["id"]]["key"], "sk-family")  # Nova holds the key
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", "llm_model_id": made["id"]})
        run = self.wait_done(run["id"])
        self.assertEqual(run["status"], "done")
        chat = self.fake.chats[run["chat_id"]]
        self.assertEqual(chat["llm_model_id"], made["id"])  # the chat was created on the family member's account
        self.assertTrue(all(m.get("llm_model_id") == made["id"] for m in self.fake.messages[-2:]))  # design + repair
        self.assertNotIn("sk-family", json.dumps(run))
        status, gone = http("POST", f"{self.base}/api/nova/llm-models/{made['id']}/delete", {})
        self.assertTrue(gone["deleted"])
        self.assertNotIn(made["id"], self.fake.llm_entries)

    def test_effort_is_sent_with_design_and_repairs(self):
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", "effort": "Medium"})
        run = self.wait_done(run["id"])
        self.assertEqual([m["options"].get("effort") for m in self.fake.messages[-2:]], ["medium", "medium"])
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", "effort": "turbo"})
        self.wait_done(run["id"])
        self.assertNotIn("effort", self.fake.messages[-1]["options"])  # unknown values fall back to Nova's default

    def test_history_lists_runs_newest_first(self):
        for _ in range(2):
            status, run = http("POST", f"{self.base}/api/runs", {"chat_id": "latest", "rounds": 0})
            self.wait_done(run["id"])
            time.sleep(0.01)
        status, body = http("GET", f"{self.base}/api/runs")
        self.assertGreaterEqual(len(body["runs"]), 1)
        starts = [r["started_at"] for r in body["runs"]]
        self.assertEqual(starts, sorted(starts, reverse=True))


class HostedTests(unittest.TestCase):
    """A locked server for family use: browsers cannot redirect it, links to
    Nova use the public address, and the server's Rebrickable key is used."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fake = FakeNova()
        cls.original_init = nova.Client.__init__

        def patched(self, base_url=nova.DEFAULT_URL, opener=None, public_url=None):
            cls.seen_base = base_url
            cls.original_init(self, base_url, cls.fake, public_url)
        nova.Client.__init__ = patched
        cls.server = web.make_server(0, Path(cls.tmp.name) / "runs", nova_url="http://ldraw-nova-app:8000",
                                     nova_db="", host="127.0.0.1", nova_public_url="https://nova.example.test/",
                                     locked=True, rebrickable_key="server-key")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        nova.Client.__init__ = cls.original_init
        cls.tmp.cleanup()

    def test_settings_show_lock_and_key_presence_only(self):
        status, s = http("GET", f"{self.base}/api/settings")
        self.assertTrue(s["locked"])
        self.assertTrue(s["has_server_key"])
        self.assertNotIn("rebrickable_key", s)
        self.assertEqual(s["nova_public_url"], "https://nova.example.test")

    def test_browser_cannot_redirect_a_locked_server(self):
        http("GET", f"{self.base}/api/nova/chats?nova_url=http://evil.example.test")
        self.assertEqual(self.seen_base, "http://ldraw-nova-app:8000")
        status, run = http("POST", f"{self.base}/api/runs", {
            "chat_id": "latest", "rounds": 0, "nova_url": "http://evil.example.test",
            "nova_db": "/etc/passwd", "ldraw_dir": "/etc"})
        for _ in range(100):
            status, run = http("GET", f"{self.base}/api/runs/{run['id']}")
            if run["status"] in ("done", "failed", "stopped"):
                break
            time.sleep(0.05)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["request"]["nova_url"], "http://ldraw-nova-app:8000")
        self.assertEqual(run["request"]["nova_db"], "")
        self.assertTrue(run["chat_url"].startswith("https://nova.example.test/chat/"))
        self.assertTrue(run["request"]["availability"])  # the server's key was used


if __name__ == "__main__":
    unittest.main()
