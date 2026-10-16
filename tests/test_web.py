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

        def patched(self, base_url=nova.DEFAULT_URL, opener=None):
            cls.original_init(self, base_url, cls.fake)
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
        self.assertEqual([c["title"] for c in body["chats"]], ["BRAT"])

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

    def test_history_lists_runs_newest_first(self):
        for _ in range(2):
            status, run = http("POST", f"{self.base}/api/runs", {"chat_id": "latest", "rounds": 0})
            self.wait_done(run["id"])
            time.sleep(0.01)
        status, body = http("GET", f"{self.base}/api/runs")
        self.assertGreaterEqual(len(body["runs"]), 1)
        starts = [r["started_at"] for r in body["runs"]]
        self.assertEqual(starts, sorted(starts, reverse=True))


if __name__ == "__main__":
    unittest.main()
