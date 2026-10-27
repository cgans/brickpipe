"""The web app's API against a stand-in Nova (the FakeNova from test_loop)."""
import hashlib
import json
import stat
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

    def test_unlocked_forget_works_without_a_token_but_never_for_the_default(self):
        status, made = http("POST", f"{self.base}/api/nova/llm-models", {"model": "openai/gpt-6-luna", "api_key": "sk-local"})
        self.assertTrue(made["token"])
        status, gone = http("POST", f"{self.base}/api/nova/llm-models/{made['id']}/delete", {})
        self.assertEqual((status, gone["deleted"]), (200, True))
        self.assertNotIn(made["id"], self.fake.llm_entries)
        status, err = http("POST", f"{self.base}/api/nova/llm-models/host/delete", {})
        self.assertEqual(status, 403)
        self.assertIn("default", err["error"])
        self.assertIn("host", self.fake.llm_entries)

    def test_effort_is_sent_with_design_and_repairs(self):
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", "effort": "Medium"})
        run = self.wait_done(run["id"])
        self.assertEqual([m["options"].get("effort") for m in self.fake.messages[-2:]], ["medium", "medium"])
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", "effort": "turbo"})
        self.wait_done(run["id"])
        self.assertNotIn("effort", self.fake.messages[-1]["options"])  # unknown values fall back to Nova's default

    def test_refine_a_finished_run_in_its_chat(self):
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed"})
        run = self.wait_done(run["id"])
        self.assertTrue(run["result"]["clean"])
        self.assertEqual([r["number"] for r in run["rounds"]], [1, 2])
        sent_before = len(self.fake.messages)

        status, body = http("POST", f"{self.base}/api/runs/{run['id']}/refine", {"text": "  "})
        self.assertEqual(status, 400)
        status, body = http("POST", f"{self.base}/api/runs/{run['id']}/refine",
                            {"text": "Make the roof blue", "effort": "low"})
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "designing")
        status, again = http("POST", f"{self.base}/api/runs/{run['id']}/refine", {"text": "and taller"})
        self.assertEqual(status, 409)  # one change at a time
        run = self.wait_done(run["id"])

        self.assertEqual(run["status"], "done")
        change_msg = self.fake.messages[sent_before]
        self.assertTrue(change_msg["text"].startswith("Make the roof blue"))
        self.assertIn("This is a change to the model you delivered", change_msg["text"])
        self.assertEqual(change_msg["options"]["effort"], "low")
        # the changed model is checked and repaired, carrying on the round numbers
        self.assertEqual([r["number"] for r in run["rounds"]], [1, 2, 3, 4])
        change = run["refinements"][0]
        self.assertEqual((change["text"], change["first_round"], change["error"]), ("Make the roof blue", 3, None))
        self.assertTrue(change["model"].endswith(".mpd"))
        self.assertTrue(run["result"]["clean"])
        self.assertEqual(run["result"]["tokens"]["turns"], 4)  # design, repair, change, repair
        self.assertTrue((Path(self.tmp.name) / "runs" / run["id"] / "round-4" / "report.md").exists())

    def test_a_change_that_delivers_nothing_keeps_the_last_result(self):
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed"})
        run = self.wait_done(run["id"])
        rounds_before = run["rounds"]
        self.fake.deliver = False
        try:
            http("POST", f"{self.base}/api/runs/{run['id']}/refine", {"text": "What colour is it?"})
            run = self.wait_done(run["id"])
        finally:
            self.fake.deliver = True
        self.assertEqual(run["status"], "done")
        self.assertIn("without delivering a changed model", run["refinements"][0]["error"])
        self.assertEqual(run["rounds"], rounds_before)
        self.assertTrue(run["result"]["clean"])

    def test_rerun_a_failed_run_in_a_new_chat(self):
        self.fake.fail_turn = True
        try:
            status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a barn", "rounds": 2})
            run = self.wait_done(run["id"])
        finally:
            self.fake.fail_turn = False
        self.assertEqual(run["status"], "failed")
        first_chat = run["chat_id"]

        status, new = http("POST", f"{self.base}/api/runs/{run['id']}/rerun", {"effort": "low"})
        self.assertEqual(status, 202)
        self.assertNotEqual(new["id"], run["id"])
        self.assertEqual(new["rerun_of"], run["id"])
        self.assertEqual(new["request"]["prompt"], "a barn")
        self.assertEqual(new["request"]["rounds"], 2)
        new = self.wait_done(new["id"])
        self.assertEqual(new["status"], "done")
        self.assertTrue(new["result"]["clean"])
        self.assertNotEqual(new["chat_id"], first_chat)  # a fresh Nova chat
        status, old = http("GET", f"{self.base}/api/runs/{run['id']}")
        self.assertEqual(old["rerun_as"], new["id"])

    def test_rerun_and_delete_refuse_a_run_in_progress(self):
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a tower"})
        status, body = http("POST", f"{self.base}/api/runs/{run['id']}/delete")
        self.assertEqual(status, 409)
        status, body = http("POST", f"{self.base}/api/runs/{run['id']}/rerun")
        self.assertEqual(status, 409)
        self.wait_done(run["id"])
        status, body = http("POST", f"{self.base}/api/runs/nope/delete")
        self.assertEqual(status, 404)

    def test_delete_a_run(self):
        status, run = http("POST", f"{self.base}/api/runs", {"chat_id": "latest", "rounds": 0})
        self.wait_done(run["id"])
        folder = Path(self.tmp.name) / "runs" / run["id"]
        self.assertTrue(folder.is_dir())
        status, body = http("POST", f"{self.base}/api/runs/{run['id']}/delete")
        self.assertEqual((status, body), (200, {"deleted": run["id"]}))
        self.assertFalse(folder.exists())
        status, body = http("GET", f"{self.base}/api/runs/{run['id']}")
        self.assertEqual(status, 404)
        status, body = http("GET", f"{self.base}/api/runs?limit=0")
        self.assertNotIn(run["id"], [r["id"] for r in body["runs"]])

    def test_deleted_runs_stay_gone_after_a_restart(self):
        runs_dir = Path(self.tmp.name) / "runs"
        status, run = http("POST", f"{self.base}/api/runs", {"chat_id": "latest", "rounds": 0})
        self.wait_done(run["id"])
        http("POST", f"{self.base}/api/runs/{run['id']}/delete")
        self.assertNotIn(run["id"], web.Runner(runs_dir).runs)

    def test_listing_pages_and_counts(self):
        for _ in range(3):
            status, run = http("POST", f"{self.base}/api/runs", {"chat_id": "latest", "rounds": 0})
            self.wait_done(run["id"])
        status, everything = http("GET", f"{self.base}/api/runs?limit=0")
        total = everything["total"]
        self.assertEqual(len(everything["runs"]), total)
        status, two = http("GET", f"{self.base}/api/runs?limit=2&offset=1")
        self.assertEqual(two["total"], total)
        self.assertEqual([r["id"] for r in two["runs"]], [r["id"] for r in everything["runs"][1:3]])
        entry = two["runs"][0]
        self.assertNotIn("log", entry)  # the listing is light
        self.assertIn("status", entry)
        status, bad = http("GET", f"{self.base}/api/runs?limit=lots")
        self.assertEqual(status, 400)

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


class HostedOwnKeyTests(unittest.TestCase):
    """On a locked server only the browser that added a key can remove it,
    and nobody can remove Nova's default entry through brickpipe."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fake = FakeNova()
        cls.original_init = nova.Client.__init__

        def patched(self, base_url=nova.DEFAULT_URL, opener=None, public_url=None):
            cls.original_init(self, base_url, cls.fake, public_url)
        nova.Client.__init__ = patched
        cls.runs = Path(cls.tmp.name) / "runs"
        cls.server = web.make_server(0, cls.runs, nova_url="http://ldraw-nova-app:8000", nova_db="", locked=True)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        nova.Client.__init__ = cls.original_init
        cls.tmp.cleanup()

    def add(self, key="sk-family"):
        status, made = http("POST", f"{self.base}/api/nova/llm-models", {"model": "openai/gpt-6-luna", "api_key": key})
        self.assertEqual(status, 200)
        return made

    def delete(self, entry_id, body):
        return http("POST", f"{self.base}/api/nova/llm-models/{entry_id}/delete", body)

    def test_add_returns_a_token_that_is_only_kept_as_a_hash(self):
        made = self.add("sk-secret-key")
        self.assertEqual(set(made), {"id", "token"})
        self.assertGreaterEqual(len(made["token"]), 40)
        status, acc = http("GET", f"{self.base}/api/nova/llm-models")
        self.assertNotIn(made["token"], json.dumps(acc))
        self.assertNotIn(made["token"], json.dumps(self.fake.llm_entries))  # Nova never sees it
        stored = (self.runs / "own-keys.json")
        text = stored.read_text()
        self.assertNotIn(made["token"], text)
        self.assertNotIn("sk-secret-key", text)
        self.assertEqual(json.loads(text)[made["id"]], hashlib.sha256(made["token"].encode()).hexdigest())
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertFalse((self.runs / "own-keys.json.tmp").exists())  # no temp file left behind

    def test_only_the_right_token_removes_an_entry(self):
        made = self.add()
        other = self.add("sk-someone-else")
        status, err = self.delete(made["id"], {})
        self.assertEqual(status, 403)
        self.assertIn("Only the browser that added", err["error"])
        status, _ = self.delete(made["id"], {"token": "not-the-token"})
        self.assertEqual(status, 403)
        status, _ = self.delete(made["id"], {"token": other["token"]})  # someone else's token
        self.assertEqual(status, 403)
        self.assertIn(made["id"], self.fake.llm_entries)
        status, gone = self.delete(made["id"], {"token": made["token"]})
        self.assertEqual((status, gone["deleted"]), (200, True))
        self.assertNotIn(made["id"], self.fake.llm_entries)
        self.assertNotIn(made["id"], json.loads((self.runs / "own-keys.json").read_text()))
        self.assertIn(other["id"], self.fake.llm_entries)
        # Repeating it succeeds: the entry is gone either way, and the page can tidy up.
        status, again = self.delete(made["id"], {"token": made["token"]})
        self.assertEqual((status, again.get("already_gone")), (200, True))

    def test_entry_nova_no_longer_has_can_be_forgotten(self):
        made = self.add()
        self.fake.llm_entries.pop(made["id"])  # removed in Nova directly
        status, gone = self.delete(made["id"], {})
        self.assertEqual((status, gone["deleted"]), (200, True))
        self.assertNotIn(made["id"], json.loads((self.runs / "own-keys.json").read_text()))

    def test_entry_hidden_from_nova_list_is_still_removed_properly(self):
        # Nova hides entries whose model it no longer supports; they still exist.
        made = self.add()
        self.fake.llm_entries[made["id"]]["unsupported"] = True
        status, acc = http("GET", f"{self.base}/api/nova/llm-models")
        self.assertNotIn(made["id"], [m["id"] for m in acc["models"]])
        status, _ = self.delete(made["id"], {})
        self.assertEqual(status, 403)  # not mistaken for "already gone"
        self.assertIn(made["id"], json.loads((self.runs / "own-keys.json").read_text()))
        status, gone = self.delete(made["id"], {"token": made["token"]})
        self.assertEqual(status, 200)
        self.assertNotIn("already_gone", gone)
        self.assertNotIn(made["id"], self.fake.llm_entries)

    def test_nova_deleted_false_counts_as_already_gone(self):
        made = self.add()
        real = self.fake.__call__
        calls = []

        def vanish_first(req):  # removed in Nova between our check and our delete
            if req.get_method() == "DELETE" and not calls:
                calls.append(1)
                self.fake.llm_entries.pop(made["id"], None)
            return real(req)
        nova.Client.__init__, original = (lambda c, base_url=nova.DEFAULT_URL, opener=None, public_url=None:
                                          type(self).original_init(c, base_url, vanish_first, public_url)), nova.Client.__init__
        try:
            status, gone = self.delete(made["id"], {"token": made["token"]})
        finally:
            nova.Client.__init__ = original
        self.assertEqual((status, gone.get("already_gone")), (200, True))
        self.assertNotIn(made["id"], json.loads((self.runs / "own-keys.json").read_text()))

    def test_failed_token_save_removes_the_new_entry(self):
        tokens = self.server.RequestHandlerClass.key_tokens
        before = set(self.fake.llm_entries)

        def broken(entry_id):
            raise OSError("disk full")
        tokens.issue = broken
        try:
            status, err = http("POST", f"{self.base}/api/nova/llm-models", {"model": "openai/gpt-6-luna", "api_key": "sk-x"})
        finally:
            del tokens.issue
        self.assertEqual(status, 500)
        self.assertIn("Nothing was kept", err["error"])
        self.assertNotIn("sk-x", json.dumps(err))
        self.assertEqual(set(self.fake.llm_entries), before)

    def test_fallback_default_is_protected_when_novas_own_default_is_hidden(self):
        # Runs with no entry named use Nova's first usable entry when its own
        # default is unusable, so that entry is the one to protect.
        self.add()  # so there is a usable entry to fall back to
        self.fake.llm_entries["host"]["unsupported"] = True
        try:
            status, acc = http("GET", f"{self.base}/api/nova/llm-models")
            fallback = acc["default_id"]
            self.assertNotEqual(fallback, "host")
            status, _ = self.delete(fallback, {"token": "anything"})
            self.assertEqual(status, 403)
        finally:
            self.fake.llm_entries["host"].pop("unsupported")

    def test_default_entry_cannot_be_removed(self):
        status, err = self.delete("host", {})
        self.assertEqual(status, 403)
        self.assertIn("default", err["error"])
        self.assertIn("host", self.fake.llm_entries)


class OwnKeyUseTests(unittest.TestCase):
    """Locked: only the browser holding an entry's token can run on it.
    Unlocked: nothing changes. The token is never kept with a run."""
    locked = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fake = FakeNova()
        cls.original_init = nova.Client.__init__

        def patched(self, base_url=nova.DEFAULT_URL, opener=None, public_url=None):
            cls.original_init(self, base_url, cls.fake, public_url)
        nova.Client.__init__ = patched
        cls.runs = Path(cls.tmp.name) / "runs"
        cls.server = web.make_server(0, cls.runs, nova_url="http://ldraw-nova-app:8000", nova_db="", locked=cls.locked)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        nova.Client.__init__ = cls.original_init
        cls.tmp.cleanup()

    def setUp(self):
        status, self.mine = http("POST", f"{self.base}/api/nova/llm-models", {"model": "openai/gpt-6-luna", "api_key": "sk-mine"})
        status, self.theirs = http("POST", f"{self.base}/api/nova/llm-models", {"model": "openai/gpt-6-luna", "api_key": "sk-theirs"})

    def finished_run(self, **extra):
        status, run = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", **extra})
        self.assertEqual(status, 202, run)
        return self.wait(run)

    def wait(self, run):  # one run at a time: the fake Nova is shared
        for _ in range(100):
            status, run = http("GET", f"{self.base}/api/runs/{run['id']}")
            if run["status"] in ("done", "failed", "stopped"):
                return run
            time.sleep(0.05)
        self.fail("run did not finish")

    def refused(self):
        return [{}, {"llm_model_token": "wrong"}, {"llm_model_token": self.theirs["token"]}]

    def assert_token_not_kept(self, *run_ids):
        token = self.mine["token"]
        status, listing = http("GET", f"{self.base}/api/runs")
        self.assertNotIn(token, json.dumps(listing))
        for rid in run_ids:
            status, run = http("GET", f"{self.base}/api/runs/{rid}")
            self.assertNotIn(token, json.dumps(run))
            self.assertNotIn("llm_model_token", (self.runs / rid / "state.json").read_text())
            self.assertNotIn(token, (self.runs / rid / "state.json").read_text())

    def test_new_run(self):
        mine = {"llm_model_id": self.mine["id"]}
        for extra in self.refused():
            status, body = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", **mine, **extra})
            if self.locked:
                self.assertEqual(status, 403, extra)
                self.assertIn("someone else's browser", body["error"])
            else:
                self.assertEqual(status, 202, extra)
                self.wait(body)
        run = self.finished_run(**mine, llm_model_token=self.mine["token"])
        self.assertEqual(self.fake.chats[run["chat_id"]]["llm_model_id"], self.mine["id"])
        self.assert_token_not_kept(run["id"])
        for extra in ({}, {"llm_model_id": ""}, {"llm_model_id": "host"}):  # host's account: no token needed
            self.finished_run(**extra)

    def test_entry_without_a_stored_token(self):
        self.fake.llm_entries["old"] = {"model_name": "added before tokens", "model": "openai/gpt-6-luna", "key": "k"}
        status, body = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", "llm_model_id": "old", "llm_model_token": "x"})
        self.assertEqual(status, 403 if self.locked else 202)
        if status == 202:
            self.wait(body)

    def test_refine(self):
        run = self.finished_run()
        url = f"{self.base}/api/runs/{run['id']}/refine"
        for extra in self.refused() if self.locked else []:
            status, _ = http("POST", url, {"text": "taller", "llm_model_id": self.mine["id"], **extra})
            self.assertEqual(status, 403, extra)
        if not self.locked:  # unlocked: a refine without a token is accepted
            status, _ = http("POST", url, {"text": "wider", "llm_model_id": self.mine["id"]})
            self.assertEqual(status, 202)
            self.wait(run)
        status, body = http("POST", url, {"text": "taller", "llm_model_id": self.mine["id"],
                                          "llm_model_token": self.mine["token"]})
        self.assertEqual(status, 202, body)
        for _ in range(100):
            status, done = http("GET", f"{self.base}/api/runs/{run['id']}")
            if done["status"] in ("done", "failed", "stopped"):
                break
            time.sleep(0.05)
        self.assertEqual(done["request"]["llm_model_id"], self.mine["id"])
        self.assert_token_not_kept(run["id"])

    def test_rerun(self):
        run = self.finished_run()
        url = f"{self.base}/api/runs/{run['id']}/rerun"
        for extra in self.refused():
            status, body = http("POST", url, {"llm_model_id": self.mine["id"], **extra})
            self.assertEqual(status, 403 if self.locked else 202, extra)
            if status == 202:
                self.wait(body)
        status, new = http("POST", url, {"llm_model_id": self.mine["id"], "llm_model_token": self.mine["token"]})
        self.assertEqual(status, 202, new)
        self.wait(new)
        status, again = http("POST", url, {})  # host's account, as the first run used
        self.assertEqual(status, 202)
        self.wait(again)
        self.assert_token_not_kept(run["id"], new["id"], again["id"])

    def test_rerun_of_a_run_on_someone_elses_key_needs_their_token(self):
        run = self.finished_run(llm_model_id=self.theirs["id"], llm_model_token=self.theirs["token"])
        status, body = http("POST", f"{self.base}/api/runs/{run['id']}/rerun", {})  # inherits their entry
        self.assertEqual(status, 403 if self.locked else 202)
        if status == 202:
            self.wait(body)
        status, body = http("POST", f"{self.base}/api/runs/{run['id']}/rerun", {"llm_model_id": ""})  # host's account
        self.assertEqual(status, 202)
        self.wait(body)


    def test_own_key_that_became_the_fallback_default_still_needs_its_token(self):
        # Nova's stored default unusable: its list falls back to the first usable entry.
        self.fake.llm_entries["host"]["unsupported"] = True
        try:
            for eid in list(self.fake.llm_entries):
                if eid not in ("host", self.mine["id"]):
                    self.fake.llm_entries[eid]["unsupported"] = True
            status, acc = http("GET", f"{self.base}/api/nova/llm-models")
            self.assertEqual(acc["default_id"], self.mine["id"])
            status, _ = http("POST", f"{self.base}/api/runs", {"prompt": "a shed", "llm_model_id": self.mine["id"]})
            self.assertEqual(status, 403 if self.locked else 202)
            if status == 202:
                self.wait(_)
            self.finished_run(llm_model_id=self.mine["id"], llm_model_token=self.mine["token"])
        finally:
            for e in self.fake.llm_entries.values():
                e.pop("unsupported", None)


class UnlockedOwnKeyUseTests(OwnKeyUseTests):
    locked = False
