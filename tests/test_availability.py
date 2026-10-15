"""Availability check against canned Rebrickable responses.

The fixtures follow the response shapes in Rebrickable's API v3 docs. No test
here touches the network.
"""
import csv
import io
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from brickpipe import availability, bom, cli, rebrickable
from brickpipe.bom import BomRow
from brickpipe.catalog import load_colours

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "garden-shed.mpd"


def colour(rb_id, name, ldraw, bricklink):
    return {
        "id": rb_id, "name": name, "rgb": "000000", "is_trans": False,
        "external_ids": {
            "LDraw": {"ext_ids": ldraw, "ext_descrs": [[name]]},
            "BrickLink": {"ext_ids": bricklink, "ext_descrs": [[name]]},
        },
    }


COLOURS = [
    colour(0, "Black", [0, 256], [11]),
    colour(2, "Green", [2], [6]),
    colour(4, "Red", [4], [5]),
    colour(14, "Yellow", [14], [3]),
    colour(15, "White", [15], [1]),
]

PARTS = {
    "3001": {"part_num": "3001", "name": "Brick 2 x 4",
             "external_ids": {"LDraw": ["3001"], "BrickLink": ["3001"]}},
    "3035": {"part_num": "3035", "name": "Plate 4 x 8",
             "external_ids": {"LDraw": ["3035"], "BrickLink": ["3035"]}},
    # LDraw calls this 4073; Rebrickable files it under 6141.
    "6141": {"part_num": "6141", "name": "Plate Round 1 x 1",
             "external_ids": {"LDraw": ["6141", "4073"], "BrickLink": ["4073"]}},
}

PART_COLOURS = {
    "3001": [{"color_id": 15, "color_name": "White", "num_sets": 900}],
    "3035": [
        {"color_id": 4, "color_name": "Red", "num_sets": 2},
        {"color_id": 0, "color_name": "Black", "num_sets": 300},
        {"color_id": 15, "color_name": "White", "num_sets": 120},
    ],
    "6141": [{"color_id": 14, "color_name": "Yellow", "num_sets": 500}],
}


class FakeApi:
    """Answers the URLs the client builds, and records them."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.calls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.removeprefix("/api/v3")
        if path == "/lego/colors/":
            if query.get("page") == ["2"]:
                return {"next": None, "results": COLOURS[3:]}
            return {"next": url + "&page=2", "results": COLOURS[:3]}
        if path == "/lego/parts/":
            if "part_nums" in query:
                wanted = query["part_nums"][0].split(",")
                hits = [PARTS[n] for n in wanted if n in PARTS]
            else:
                wanted_id = query["ldraw_id"][0]
                hits = [p for p in PARTS.values()
                        if wanted_id in p["external_ids"]["LDraw"]]
            return {"next": None, "results": hits}
        if path.startswith("/lego/parts/") and path.endswith("/colors/"):
            part = path.split("/")[3]
            if part not in PART_COLOURS:
                raise rebrickable.NotFound(url)
            return {"next": None, "results": PART_COLOURS[part]}
        raise AssertionError(f"unexpected URL {url}")


ROWS = [
    BomRow("3001", 15, 4),   # common
    BomRow("3035", 2, 1),    # never made in green
    BomRow("3035", 4, 1),    # made, but in two sets only
    BomRow("4073", 14, 1),   # found under a different Rebrickable number
    BomRow("99999", 4, 2),   # unknown part
    BomRow("3001", 777, 1),  # unknown colour
    BomRow("s/3001s01", 4, 1),  # drawing geometry
]


def make_client(api=None, cache_dir=None, sleeps=None):
    return rebrickable.Client(
        fetch=api or FakeApi(),
        cache_dir=cache_dir,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.results = availability.check(ROWS, make_client())
        self.by_row = {(a.row.part, a.row.colour): a for a in self.results}

    def test_statuses(self):
        got = {key: a.status for key, a in self.by_row.items()}
        self.assertEqual(got, {
            ("3001", 15): availability.OK,
            ("3035", 2): availability.NOT_MADE,
            ("3035", 4): availability.RARE,
            ("4073", 14): availability.OK,
            ("99999", 4): availability.PART_UNKNOWN,
            ("3001", 777): availability.COLOUR_UNKNOWN,
            ("s/3001s01", 4): availability.NOT_A_PART,
        })

    def test_alternatives_most_common_first(self):
        self.assertEqual(
            self.by_row[("3035", 2)].alternatives, ["Black", "White", "Red"]
        )

    def test_part_found_under_other_number(self):
        item = self.by_row[("4073", 14)]
        self.assertEqual(item.rebrickable_part, "6141")
        self.assertEqual(item.bricklink_part, "4073")

    def test_findings(self):
        found = availability.findings(self.results)
        errors = [f.message for f in found if f.level == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("3035 was never produced in Green", errors[0])
        self.assertIn("Black, White, Red", errors[0])
        self.assertEqual(sum(1 for f in found if f.level == "warning"), 3)

    def test_csv_has_one_line_per_row(self):
        lines = list(csv.DictReader(io.StringIO(availability.to_csv(self.results))))
        self.assertEqual(len(lines), len(ROWS))
        self.assertEqual(lines[1]["status"], availability.NOT_MADE)
        self.assertEqual(lines[1]["made_in_instead"], "Black; White; Red")

    def test_wanted_list_uses_bricklink_ids(self):
        overrides = availability.bricklink_overrides(self.results)
        xml, skipped = bom.to_bricklink_xml(ROWS, load_colours(), overrides)
        items = {
            (i.findtext("ITEMID"), i.findtext("COLOR")): i.findtext("MINQTY")
            for i in ET.fromstring(xml).findall("ITEM")
        }
        self.assertEqual(items[("4073", "3")], "1")
        self.assertEqual(items[("3001", "1")], "4")
        self.assertEqual([r.colour for r in skipped], [777])


class ClientTests(unittest.TestCase):
    def test_follows_pagination(self):
        self.assertEqual(len(make_client().colours()), len(COLOURS))

    def test_second_run_is_served_from_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_client(cache_dir=Path(tmp))
            availability.check(ROWS, first)
            self.assertGreater(first.live_calls, 0)

            api = FakeApi()
            second = make_client(api=api, cache_dir=Path(tmp))
            again = availability.check(ROWS, second)
            self.assertEqual(api.calls, [])
            self.assertEqual(len(again), len(ROWS))

    def test_live_calls_are_spaced_out(self):
        sleeps: list[float] = []
        client = make_client(sleeps=sleeps)
        availability.check(ROWS, client)
        self.assertEqual(len(sleeps), client.live_calls - 1)
        self.assertTrue(all(0 < s <= rebrickable.MIN_INTERVAL for s in sleeps))

    def test_key_is_required_and_never_in_urls(self):
        import os
        saved = os.environ.pop(rebrickable.KEY_ENV, None)
        try:
            with self.assertRaises(rebrickable.RebrickableError):
                rebrickable.Client()
        finally:
            if saved is not None:
                os.environ[rebrickable.KEY_ENV] = saved
        api = FakeApi()
        availability.check(ROWS, make_client(api=api))
        self.assertFalse(any("key=" in url for url in api.calls))

    def test_unexpected_response_is_reported_clearly(self):
        client = rebrickable.Client(fetch=lambda url: {"detail": "nope"}, sleep=lambda s: None)
        with self.assertRaisesRegex(rebrickable.RebrickableError, "Unexpected response"):
            client.colours()


class CliTests(unittest.TestCase):
    def test_availability_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(
                [str(EXAMPLE), "-o", tmp, "--availability", "--strict"],
                client=make_client(),
            )
            report = (Path(tmp) / "report.md").read_text()
            self.assertTrue((Path(tmp) / "availability.csv").is_file())
        self.assertEqual(code, 1)  # the green 4 x 8 plate was never made
        self.assertIn("3035 was never produced in Green", report)


if __name__ == "__main__":
    unittest.main()
