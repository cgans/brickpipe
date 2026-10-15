import tempfile
import unittest
from pathlib import Path

from brickpipe import checks, cli, feedback, ldraw
from brickpipe.catalog import load_colours
from brickpipe.checks import Finding

MODEL = (
    "0 FILE house.ldr\n"
    "0 // left-wall: left side\n"
    "1 15 -40 0 0 1 0 0 0 1 0 0 0 1 wall.ldr\n"
    "0 // right-wall: right side\n"
    "1 15 40 0 0 1 0 0 0 1 0 0 0 1 wall.ldr\n"
    "0 FILE wall.ldr\n"
    "0 // base: lower brick\n"
    "1 16 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
    "0 // base-copy: same spot by mistake\n"
    "1 16 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.colours = load_colours()
        self.pieces, _ = ldraw.flatten(ldraw.parse(MODEL))
        self.findings = checks.check_duplicates(self.pieces)

    def test_pieces_remember_where_they_came_from(self):
        first = self.pieces[0]
        self.assertEqual(first.label, "base: lower brick")
        self.assertEqual(first.local, (0, -24, 0))
        self.assertEqual(first.pos, (-40, -24, 0))
        self.assertEqual(first.path[-1], "wall.ldr")

    def test_request_names_submodel_part_position_and_comment(self):
        text = feedback.build("house", len(self.pieces), self.findings, self.colours)
        self.assertIn("# Repair request: house", text)
        self.assertIn("## Must fix", text)
        self.assertIn("What to do: Keep one copy and remove the rest.", text)
        self.assertIn("In `wall.ldr`:", text)
        self.assertIn("`3001` in White at (0, -24, 0)", text)
        self.assertIn('under the comment "base-copy: same spot by mistake"', text)
        self.assertNotIn("## Should fix", text)

    def test_nothing_to_fix_means_no_request(self):
        self.assertIsNone(feedback.build("house", 4, [], self.colours))
        human_only = [Finding("warning", "steps", "a long step")]
        self.assertIsNone(feedback.build("house", 4, human_only, self.colours))

    def test_errors_come_before_warnings_and_numbering_continues(self):
        mixed = [
            Finding("warning", "floating", "two pieces float", "attach them"),
            Finding("error", "duplicate", "two copies overlap", "remove one"),
        ]
        text = feedback.build("house", 4, mixed, self.colours)
        self.assertLess(text.index("### 1. two copies overlap"),
                        text.index("### 2. two pieces float"))
        self.assertIn("1 problem(s) make that impossible", text)

    def test_long_lists_are_capped(self):
        many = tuple(
            ldraw.PlacedPart("3005", 4, (x, 0, 0), ldraw.IDENTITY, ("m.ldr", "m.ldr"), 1,
                             local=(x, 0, 0))
            for x in range(0, 2000, 20)
        )
        text = feedback.build(
            "m", 100, [Finding("error", "x", "msg", "fix", many)], self.colours
        )
        self.assertIn("and 92 more line(s) in this submodel", text)
        self.assertLess(len(text.splitlines()), 40)

    def test_cli_writes_and_removes_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "house.mpd"
            bad.write_text(MODEL)
            out = Path(tmp) / "out"
            cli.main([str(bad), "-o", str(out)])
            self.assertTrue((out / "feedback.md").is_file())
            self.assertIn("feedback.md", (out / "report.md").read_text())

            bad.write_text(MODEL.rsplit("0 // base-copy", 1)[0])  # repaired
            cli.main([str(bad), "-o", str(out)])
            self.assertFalse((out / "feedback.md").exists())


if __name__ == "__main__":
    unittest.main()
