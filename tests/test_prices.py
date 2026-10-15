import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from brickpipe import bricklink, cli, prices
from brickpipe.bom import BomRow
from brickpipe.catalog import load_colours

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "garden-shed.mpd"
COLOURS = load_colours()
CREDS = bricklink.Credentials("ck", "cs", "tk", "ts")


def guide(avg, low, high, lots, pieces, currency="USD"):
    return {"meta": {"code": 200}, "data": {
        "item": {"no": "x", "type": "PART"}, "currency_code": currency,
        "min_price": f"{low:.4f}", "max_price": f"{high:.4f}", "avg_price": f"{avg:.4f}",
        "qty_avg_price": f"{avg:.4f}", "unit_quantity": lots, "total_quantity": pieces}}


class FakeBrickLink:
    """Serves price guides by (item, colour) and records the calls."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, url, params):
        item = url.rsplit("/", 2)[-2]
        self.calls.append((item, params))
        entry = self.table.get((item, params.get("color_id")))
        if entry is None:
            return {"meta": {"code": 404, "description": "not found"}}
        if isinstance(entry, Exception):
            raise entry
        return entry


def make_client(table, cache_dir=None):
    fake = FakeBrickLink(table)
    return bricklink.Client(fetch=fake, cache_dir=cache_dir, sleep=lambda s: None), fake


class SignatureTests(unittest.TestCase):
    def test_matches_oauthlib(self):
        # Vector produced by oauthlib 3.2.2 for the same inputs.
        url = "https://api.bricklink.com/api/store/v1/items/PART/3001/price"
        params = {"color_id": "5", "guide_type": "stock", "new_or_used": "N", "region": "north_america"}
        header = bricklink.sign("GET", url, params, CREDS, timestamp="1700000000", nonce="abcdef")
        self.assertTrue(header.startswith('OAuth realm="", '))
        self.assertIn('oauth_signature="Vwnf8Cr3Nm8Mz0AdfUPj1YLsolI%3D"', header)
        self.assertIn('oauth_consumer_key="ck"', header)
        self.assertNotIn("cs", header.replace("oauth_consumer_key", ""))  # secrets never leave

    def test_credentials_need_all_four(self):
        self.assertIsNone(bricklink.Credentials.from_dict({"consumer_key": "a", "token": "b"}))
        self.assertEqual(bricklink.Credentials.from_dict(
            {"consumer_key": "a", "consumer_secret": "b", "token": "c", "token_secret": "d "}).token_secret, "d")

    def test_no_credentials_is_a_clear_error(self):
        with self.assertRaises(bricklink.BrickLinkError) as ctx:
            bricklink.Client(None)
        self.assertIn("BRICKLINK_CONSUMER_KEY", str(ctx.exception))


class ClientTests(unittest.TestCase):
    def test_price_caches_and_handles_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, fake = make_client({("3001", "5"): guide(0.1, 0.05, 0.3, 40, 900)}, Path(tmp))
            first = client.price("3001", 5, region="north_america")
            again = client.price("3001", 5, region="north_america")
            self.assertEqual(first["unit_quantity"], 40)
            self.assertEqual(first, again)
            self.assertEqual(len(fake.calls), 1)  # second call came from the cache
            self.assertEqual(fake.calls[0][1], {"color_id": "5", "guide_type": "stock",
                                                "new_or_used": "N", "region": "north_america"})
            self.assertIsNone(client.price("nope", 5))


class EstimateTests(unittest.TestCase):
    rows = [BomRow("3001", 15, 4), BomRow("3005", 4, 2), BomRow("3024", 0, 10), BomRow("9999", 4, 1)]

    def table(self):
        return {
            ("3001", "1"): guide(0.12, 0.05, 0.40, 50, 2000),   # white 2x4, common
            ("3005", "5"): guide(2.00, 1.50, 3.00, 2, 6),        # red 1x1: pricey, two sellers
            ("3024", "11"): guide(0.03, 0.01, 0.10, 0, 0),       # black 1x1 plate: nobody selling
        }

    def test_totals_notes_and_csv(self):
        client, fake = make_client(self.table())
        est = prices.estimate(self.rows, COLOURS, client, region="europe", currency="EUR")
        self.assertEqual(est.currency, "EUR")  # the requested currency wins over the response's
        self.assertEqual(est.total, Decimal("0.12") * 4 + Decimal("2.00") * 2)
        self.assertEqual(est.total_low, Decimal("0.05") * 4 + Decimal("1.50") * 2)
        notes = {ln.row.part: ln.note for ln in est.lines}
        self.assertEqual(notes["3001"], "")
        self.assertEqual(notes["3005"], "only 2 seller(s)")
        self.assertEqual(notes["3024"], "nobody is selling it")
        self.assertIn("not in BrickLink", notes["9999"])
        self.assertEqual(len(est.unpriced), 2)
        self.assertTrue(all(p.get("region") == "europe" and p.get("currency_code") == "EUR" for _, p in fake.calls))
        csv_text = prices.to_csv(est)
        self.assertIn("3001,15,White,3001,1,4,0.12,0.05,0.40,0.48,50,2000,EUR,", csv_text)
        self.assertIn("3024,0,Black,3024,11,10,,,,,0,0,EUR,nobody is selling it", csv_text)

    def test_overrides_use_bricklink_ids(self):
        client, fake = make_client({("3001b", "1"): guide(0.5, 0.5, 0.5, 9, 9)})
        est = prices.estimate([BomRow("3001", 15, 1)], COLOURS, client, {("3001", 15): ("3001b", 1)})
        self.assertEqual(fake.calls[0][0], "3001b")
        self.assertEqual(est.lines[0].avg, Decimal("0.5"))

    def test_findings_name_what_the_agent_can_change(self):
        client, _ = make_client(self.table())
        est = prices.estimate(self.rows, COLOURS, client)
        found = {f.message.split(":")[0]: f for f in prices.findings(est)}
        self.assertIn("3005 in Red (BrickLink colour 5)", found)
        self.assertIn("2.00 USD each", found["3005 in Red (BrickLink colour 5)"].message)
        self.assertIn("only 2 seller(s)", found["3005 in Red (BrickLink colour 5)"].message)
        self.assertIn("nobody is selling it", found["3024 in Black (BrickLink colour 11)"].message)
        self.assertNotIn("9999 in Red (BrickLink colour 5)", found)  # availability reports unknown parts
        self.assertTrue(all(f.level == "warning" for f in found.values()))

    def test_a_bricklink_error_stops_early_but_keeps_the_run(self):
        table = self.table()
        table[("3005", "5")] = bricklink.BrickLinkError("BrickLink refused the access token for this computer's address.")
        client, _ = make_client(table)
        est = prices.estimate(self.rows, COLOURS, client)
        self.assertIn("refused the access token", est.error)
        self.assertEqual(len(est.lines), 2)  # stopped at the failing lot
        self.assertIn("stopped early", prices.note(est))
        self.assertTrue(any("did not finish" in f.message for f in prices.findings(est)))

    def test_note_reads_well(self):
        client, _ = make_client(self.table())
        est = prices.estimate(self.rows, COLOURS, client, region="north_america")
        text = prices.note(est)
        self.assertTrue(text.startswith("Estimated cost: about 4.48 USD for the 6 pieces to buy (new, current listings in north america)"))
        self.assertIn("2 lot(s) had no price", text)


class PipelineTests(unittest.TestCase):
    def test_prices_flow_into_report_feedback_and_result(self):
        table = {("3001", "1"): guide(0.1, 0.1, 0.1, 30, 300), ("3035", "6"): guide(3.0, 2.0, 4.0, 1, 2)}
        client, _ = make_client(table)
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_pipeline(EXAMPLE, Path(tmp), pricer=client, price_options={"region": "eu"})
            report = (Path(tmp) / "report.md").read_text()
            self.assertTrue((Path(tmp) / "prices.csv").exists())
            feedback = (Path(tmp) / "feedback.md").read_text()
        self.assertIn("Estimated cost: about", report)
        self.assertIn("`prices.csv`", report)
        self.assertIn("3035 in Green (BrickLink colour 6): about 3.00 USD each", feedback)
        self.assertEqual(result.price["region"], "eu")
        self.assertGreater(Decimal(result.price["total"]), 0)

    def test_cli_prices_needs_the_variables(self):
        import os
        for var in bricklink.ENV.values():
            os.environ.pop(var, None)
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main([str(EXAMPLE), "-o", tmp, "--prices"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
