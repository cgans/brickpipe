import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from brickpipe import availability, cli, inventory, rebrickable
from brickpipe.bom import BomRow
from brickpipe.catalog import load_colours
from test_availability import FakeApi, ROWS, make_client

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "garden-shed.mpd"
COLOURS = load_colours()


class TextInventoryTests(unittest.TestCase):
    def test_parses_codes_names_blank_and_header(self):
        inv = inventory.from_text(
            "part,colour,quantity\n3001, White, 3\n3035,2,1\n4073, , 5\n# comment\n\n3001,Nope,1\n9999,4\n"
        , COLOURS)
        self.assertEqual(inv.by_ldraw[("3001", 15)], 3)
        self.assertEqual(inv.by_ldraw[("3035", 2)], 1)
        self.assertEqual(inv.by_ldraw[("4073", -1)], 5)
        self.assertEqual(inv.by_ldraw[("9999", 4)], 1)  # quantity defaults to 1
        self.assertEqual(len(inv.problems), 1)
        self.assertIn("unknown colour 'Nope'", inv.problems[0])


class SubtractTests(unittest.TestCase):
    rows = [BomRow("3001", 15, 4), BomRow("3035", 2, 1), BomRow("3035", 4, 1), BomRow("4073", 14, 1)]

    def test_exact_and_partial_and_any_colour(self):
        inv = inventory.from_text("3001, White, 3\n3035, Green, 5\n4073, , 1\n", COLOURS)
        covered = inventory.subtract(self.rows, inv)
        self.assertEqual([(c.have, c.buy) for c in covered], [(3, 1), (1, 0), (0, 1), (1, 0)])
        left = inventory.remaining_rows(covered)
        self.assertEqual([(r.part, r.colour, r.quantity) for r in left], [("3001", 15, 1), ("3035", 4, 1)])
        self.assertEqual(inventory.summary(covered), {"need": 7, "have": 5, "buy": 2,
                                                      "lots_covered": 2, "lots_total": 4,
                                                      "unused": 0, "unused_lots": 0,
                                                      "spare": 0, "spare_lots": 0})

    def test_listed_bricks_the_model_does_not_use_are_reported(self):
        inv = inventory.from_text("3001, White, 3\n9999, Red, 10\n3001, Red, 2\n", COLOURS)
        leftover = {}
        covered = inventory.subtract(self.rows, inv, leftover=leftover)
        self.assertEqual(leftover, {("9999", 4): 10, ("3001", 4): 2})
        summary = inventory.summary(covered, leftover, inv)
        self.assertEqual((summary["unused"], summary["unused_lots"]), (12, 2))
        self.assertEqual((summary["spare"], summary["spare_lots"]), (0, 0))
        csv_text = inventory.to_csv(covered, COLOURS, leftover)
        self.assertIn("9999,4,Red,0,10,0", csv_text)

    def test_partly_used_lines_are_spare_not_unused(self):
        inv = inventory.from_text("3001, , 9\n7777, Red, 1\n", COLOURS)  # model needs 4 of 3001
        leftover = {}
        covered = inventory.subtract(self.rows, inv, leftover=leftover)
        summary = inventory.summary(covered, leftover, inv)
        self.assertEqual((summary["spare"], summary["spare_lots"]), (5, 1))
        self.assertEqual((summary["unused"], summary["unused_lots"]), (1, 1))

    def test_any_colour_is_shared_across_lots(self):
        inv = inventory.from_text("3035, , 1\n", COLOURS)
        covered = inventory.subtract(self.rows, inv)
        self.assertEqual(sum(c.have for c in covered if c.row.part == "3035"), 1)

    def test_rebrickable_items_match_through_aliases(self):
        items = [
            {"part": {"part_num": "3001"}, "color": {"id": 15}, "quantity": 10},
            {"part": {"part_num": "6141"}, "color": {"id": 14}, "quantity": 2},  # LDraw 4073
            {"part_num": "3035", "color_id": 2, "quantity": 1},  # flat shape
            {"nonsense": True},
        ]
        inv = inventory.from_rebrickable_items(items)
        self.assertEqual(len(inv.problems), 1)
        results = availability.check(self.rows, make_client())
        covered = inventory.subtract(self.rows, inv, availability.aliases(results))
        self.assertEqual([(c.row.part, c.have) for c in covered],
                         [("3001", 4), ("3035", 1), ("3035", 0), ("4073", 1)])

    def test_bricklink_id_in_text_matches_ldraw_lot(self):
        results = availability.check(self.rows, make_client())
        inv = inventory.from_text("6141, Yellow, 1\n", COLOURS)  # Rebrickable's id for 4073
        covered = inventory.subtract(self.rows, inv, availability.aliases(results))
        self.assertEqual(next(c.have for c in covered if c.row.part == "4073"), 1)


class ClientTests(unittest.TestCase):
    def test_sign_in_and_owned_parts_are_not_cached(self):
        calls = []

        class Api(FakeApi):
            def __call__(self, url):
                if "/users/" in url:
                    calls.append(url)
                    return {"next": None, "results": [
                        {"part": {"part_num": "3001"}, "color": {"id": 15}, "quantity": 2}]}
                return super().__call__(url)

        posts = []
        def post(url, data):
            posts.append((url, data))
            return {"user_token": "tok123"}

        with tempfile.TemporaryDirectory() as tmp:
            client = rebrickable.Client(fetch=Api(), cache_dir=Path(tmp), sleep=lambda s: None, post=post)
            self.assertEqual(client.sign_in("chris", "pw"), "tok123")
            self.assertIn(b"password=pw", posts[0][1])
            client.owned_parts("tok123"); client.owned_parts("tok123")
        self.assertEqual(len(calls), 2)  # fetched twice: never served from cache
        self.assertIn("/users/tok123/allparts/", calls[0])


class PipelineTests(unittest.TestCase):
    def test_wanted_list_holds_only_what_is_left(self):
        inv = inventory.from_text("3001, White, 4\n3035, Red, 1\n", COLOURS)
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_pipeline(EXAMPLE, Path(tmp) / "out", owned=inv)
            xml = (Path(tmp) / "out" / "bricklink_wanted.xml").read_text()
            report = (Path(tmp) / "out" / "report.md").read_text()
            owned_csv = (Path(tmp) / "out" / "owned.csv").read_text()
        items = {(i.findtext("ITEMID"), i.findtext("COLOR")): i.findtext("MINQTY")
                 for i in ET.fromstring(xml).findall("ITEM")}
        self.assertEqual(items, {("3035", "6"): "1", ("4073", "3"): "1"})
        self.assertEqual(result.owned["have"], 5)
        self.assertIn("You already have 5 of the 7 pieces", report)
        self.assertIn("3001,15,White,4,4,0", owned_csv)

    def test_cli_owned_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mine.csv").write_text("3001, White, 2\n3001, Red, 5\n")
            code = cli.main([str(EXAMPLE), "-o", str(Path(tmp) / "out"), "--owned", str(Path(tmp) / "mine.csv")])
            report = (Path(tmp) / "out" / "report.md").read_text()
        self.assertEqual(code, 0)
        self.assertIn("You already have 2 of the 7 pieces", report)
        self.assertIn("1 line(s) you typed, 5 brick(s), matched nothing in this model", report)


if __name__ == "__main__":
    unittest.main()
