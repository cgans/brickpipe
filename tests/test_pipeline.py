import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from brickpipe import bom, checks, cli, ldraw, steps
from brickpipe.catalog import load_colours

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "garden-shed.mpd"


def placement(p):
    return (p.part, p.colour, tuple(round(v, 3) for v in p.pos),
            tuple(round(v, 4) for v in p.matrix))


class FlattenTests(unittest.TestCase):
    def setUp(self):
        self.doc = ldraw.load(EXAMPLE)
        self.pieces, self.problems = ldraw.flatten(self.doc)

    def test_counts_pieces_through_submodels(self):
        self.assertEqual(len(self.pieces), 7)
        self.assertEqual(self.problems, [])

    def test_submodel_inherits_colour(self):
        bricks = [p for p in self.pieces if p.part == "3001"]
        self.assertEqual({p.colour for p in bricks}, {15})

    def test_submodel_position_and_rotation_applied(self):
        left = [p for p in self.pieces if p.part == "3001" and p.pos[0] < 0]
        self.assertEqual(sorted(p.pos for p in left), [(-60, -56, 0), (-60, -32, 0)])
        self.assertEqual(left[0].matrix, (0, 0, 1, 0, 1, 0, -1, 0, 0))

    def test_missing_submodel_reported(self):
        doc = ldraw.parse("0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 gone.ldr\n")
        pieces, problems = ldraw.flatten(doc)
        self.assertEqual(pieces, [])
        self.assertIn("gone.ldr", problems[0])

    def test_submodel_loop_does_not_hang(self):
        doc = ldraw.parse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 b.ldr\n"
            "0 FILE b.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 a.ldr\n"
        )
        _, problems = ldraw.flatten(doc)
        self.assertIn("loop", problems[0])

    def test_embedded_part_definition_is_one_piece(self):
        doc = ldraw.parse(
            "0 FILE a.ldr\n"
            "1 4 0 0 0 1 0 0 0 1 0 0 0 1 kit-01-42082---13971.dat\n"
            "1 4 40 0 0 1 0 0 0 1 0 0 0 1 kit-01-42082---13971.dat\n"
            "0 FILE kit-01-42082---13971.dat\n"
            "1 16 0 0 0 1 0 0 0 1 0 0 0 1 4-4cylo.dat\n"
            "1 16 0 0 0 1 0 0 0 1 0 0 0 1 kit-02-42082---13971s01.dat\n"
            "0 FILE kit-02-42082---13971s01.dat\n"
            "1 16 0 0 0 1 0 0 0 1 0 0 0 1 4-4ring3.dat\n"
        )
        pieces, problems = ldraw.flatten(doc)
        self.assertEqual(problems, [])
        self.assertEqual([p.part for p in pieces], ["13971", "13971"])
        self.assertEqual(pieces[0].embedded_as, "kit-01-42082---13971.dat")
        found = checks.check_embedded(pieces)
        self.assertEqual(len(found), 1)
        self.assertIn("2 piece(s)", found[0].message)

    def test_part_definitions_get_no_build_steps(self):
        body = "".join(
            f"1 16 0 {y} 0 1 0 0 0 1 0 0 0 1 4-4cylo.dat\n" for y in range(0, 200, 8)
        )
        text = (
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 own.dat\n"
            "0 FILE own.dat\n" + body
        )
        doc = ldraw.parse(text)
        self.assertEqual(steps.add_steps_everywhere(doc, max_parts=8), [])
        self.assertEqual(ldraw.dump(doc).count("0 STEP"), 0)

    def test_plain_ldr_without_file_header(self):
        doc = ldraw.parse("0 Title\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n", "x.ldr")
        pieces, _ = ldraw.flatten(doc)
        self.assertEqual([p.part for p in pieces], ["3001"])


class BomTests(unittest.TestCase):
    def setUp(self):
        self.pieces, _ = ldraw.flatten(ldraw.load(EXAMPLE))
        self.rows = bom.build(self.pieces)
        self.colours = load_colours()

    def test_rows(self):
        got = {(r.part, r.colour): r.quantity for r in self.rows}
        self.assertEqual(
            got, {("3001", 15): 4, ("3035", 2): 1, ("3035", 4): 1, ("4073", 14): 1}
        )

    def test_bricklink_xml(self):
        xml, skipped = bom.to_bricklink_xml(self.rows, self.colours)
        items = ET.fromstring(xml).findall("ITEM")
        self.assertEqual(len(items), 4)
        self.assertEqual(skipped, [])
        white = next(i for i in items if i.findtext("ITEMID") == "3001")
        self.assertEqual(white.findtext("COLOR"), "1")
        self.assertEqual(white.findtext("MINQTY"), "4")
        self.assertEqual(sum(int(i.findtext("MINQTY")) for i in items), 7)

    def test_unknown_colour_is_skipped_not_guessed(self):
        rows = [bom.BomRow("3001", 99999, 2)]
        xml, skipped = bom.to_bricklink_xml(rows, self.colours)
        self.assertEqual(skipped, rows)
        self.assertEqual(ET.fromstring(xml).findall("ITEM"), [])

    def test_flat_file_round_trips(self):
        flat = ldraw.parse(bom.to_flat_ldr(self.pieces, "shed"), "shed.ldr")
        again, _ = ldraw.flatten(flat)
        self.assertEqual(sorted(map(placement, again)), sorted(map(placement, self.pieces)))


class StepTests(unittest.TestCase):
    def test_bottom_up_and_nothing_lost(self):
        doc = ldraw.load(EXAMPLE)
        before, _ = ldraw.flatten(doc)
        changed = steps.add_steps_everywhere(doc, max_parts=1)
        self.assertEqual(set(changed), {"garden-shed.ldr", "shed-wall.ldr"})
        after, _ = ldraw.flatten(ldraw.parse(ldraw.dump(doc)))
        self.assertEqual(sorted(map(placement, after)), sorted(map(placement, before)))

        main = doc.main_model
        ys = [r.pos[1] for r in main.refs]
        self.assertEqual(ys, sorted(ys, reverse=True))  # largest Y is lowest
        self.assertEqual(main.lines[0].raw, "0 Small shed on a green base, with two identical side walls")

    def test_comment_travels_with_its_piece(self):
        doc = ldraw.load(EXAMPLE)
        steps.add_steps_everywhere(doc, max_parts=1)
        raws = [ln.raw for ln in doc.main_model.lines]
        i = raws.index("0 // roof: red 4 x 8 plate across both walls")
        self.assertTrue(raws[i + 1].endswith("3035.dat"))
        self.assertTrue(raws[i + 1].startswith("1 4 "))

    def test_small_steps_left_alone(self):
        text = (
            "0 FILE a.ldr\n"
            "1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n0 STEP\n"
            "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        )
        doc = ldraw.parse(text)
        self.assertEqual(steps.add_steps_everywhere(doc, max_parts=8), [])
        self.assertEqual(ldraw.dump(doc), text + "0 NOFILE\n")

    def test_oversized_step_is_cut(self):
        body = "".join(
            f"1 4 {x * 40} 0 0 1 0 0 0 1 0 0 0 1 3005.dat\n" for x in range(20)
        )
        doc = ldraw.parse("0 FILE a.ldr\n" + body)
        steps.add_steps_everywhere(doc, max_parts=8)
        self.assertEqual(doc.main_model.step_count, 2)
        self.assertEqual(len(doc.main_model.refs), 20)


class CheckTests(unittest.TestCase):
    def test_duplicate_detected(self):
        line = "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        pieces, _ = ldraw.flatten(ldraw.parse("0 FILE a.ldr\n" + line + line))
        self.assertEqual(len(checks.check_duplicates(pieces)), 1)

    def test_unresolved_colour_16(self):
        pieces, _ = ldraw.flatten(
            ldraw.parse("0 FILE a.ldr\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n")
        )
        found = checks.check_colours(pieces, load_colours())
        self.assertEqual([f.level for f in found], ["error"])


class FakeCatalog:
    """Stands in for the Nova database: every part is a 2 x 2 brick."""

    def bbox(self, part):
        from brickpipe.catalog import BBox
        return BBox((-20, -4, -20), (20, 24, 20))


class FloatingTests(unittest.TestCase):
    def run_check(self, second_y, second_x=0):
        text = (
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3003.dat\n"
            f"1 4 {second_x} {second_y} 0 1 0 0 0 1 0 0 0 1 3003.dat\n"
        )
        pieces, _ = ldraw.flatten(ldraw.parse(text))
        return checks.check_floating(pieces, FakeCatalog())

    def test_stacked_bricks_are_fine(self):
        self.assertEqual(self.run_check(-24), [])

    def test_gap_above_is_flagged(self):
        self.assertEqual(len(self.run_check(-80)), 1)

    def test_far_to_the_side_is_flagged(self):
        self.assertEqual(len(self.run_check(0, second_x=200)), 1)


class CliTests(unittest.TestCase):
    def test_writes_all_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main([str(EXAMPLE), "-o", tmp]), 0)
            names = {p.name for p in Path(tmp).iterdir()}
        self.assertEqual(
            names,
            {"parts.csv", "bricklink_wanted.xml", "flat.ldr", "instructions.mpd", "report.md"},
        )


if __name__ == "__main__":
    unittest.main()
