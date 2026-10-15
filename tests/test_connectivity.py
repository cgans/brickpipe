"""Stud connectivity against a tiny hand-built LDraw library and shadow library.

The part files hold only what the check reads (references to primitives);
the shadow records copy the real LDCad conventions: male studs on stud.dat,
female sockets as gridded records on the part.
"""
import tempfile
import unittest
import zipfile
from pathlib import Path

from brickpipe import checks, cli, connectivity as c, ldraw

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "garden-shed.mpd"

STUD = "1 16 {x} 0 {z} 1 0 0 0 1 0 0 0 1 stud.dat"

PARTS = {
    # 2 x 4 brick: eight studs on top (y=0), body to y=24
    "3001.dat": "0 Brick  2 x  4\n" + "\n".join(
        STUD.format(x=x, z=z) for x in (-30, -10, 10, 30) for z in (-10, 10)),
    # 1 x 1 brick: one stud, via the stud4 group primitive to test nesting
    "3005.dat": "0 Brick  1 x  1\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 stud-one.dat",
    # 4 x 8 plate: 32 studs on top, body to y=8
    "3035.dat": "0 Plate  4 x  8\n" + "\n".join(
        STUD.format(x=x, z=z) for x in (-70, -50, -30, -10, 10, 30, 50, 70) for z in (-30, -10, 10, 30)),
    # 1 x 1 round plate: one stud
    "4073.dat": "0 Plate  1 x  1 Round\n" + STUD.format(x=0, z=0),
    # tile: no studs
    "3070b.dat": "0 Tile  1 x  1\n",
}
PRIMS = {
    "stud.dat": "0 Stud\n",
    "stud-one.dat": "0 Stud group\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 stud.dat",
}
SHADOW_PARTS = {
    "3001.dat": "0 !LDCAD SNAP_CYL [gender=F] [caps=one] [secs=R 6 4] [pos=0 24 0] [grid=C 4 C 2 20 20]",
    "3005.dat": "0 !LDCAD SNAP_CYL [gender=F] [caps=one] [secs=R 6 4] [pos=0 24 0]",
    "3035.dat": "0 !LDCAD SNAP_CYL [gender=F] [caps=one] [secs=R 6 4] [pos=0 8 0] [grid=C 8 C 4 20 20]",
    "4073.dat": "0 !LDCAD SNAP_CYL [gender=F] [caps=one] [secs=R 6 4] [pos=0 8 0]",
    "3070b.dat": "0 !LDCAD SNAP_CYL [gender=F] [caps=one] [secs=S 6 4] [pos=0 8 0]",
}
SHADOW_PRIMS = {
    "stud.dat": "0 !LDCAD SNAP_CYL [ID=studC] [gender=M] [caps=one] [secs=R 6 4]",
}


def write_libraries(tmp: Path, as_zip: bool = False):
    lib = tmp / "ldraw"
    for name, text in PARTS.items():
        (lib / "parts").mkdir(parents=True, exist_ok=True)
        (lib / "parts" / name).write_text(text + "\n")
    for name, text in PRIMS.items():
        (lib / "p").mkdir(parents=True, exist_ok=True)
        (lib / "p" / name).write_text(text + "\n")
    shadow = tmp / "offLibShadow"
    for name, text in SHADOW_PARTS.items():
        (shadow / "parts").mkdir(parents=True, exist_ok=True)
        (shadow / "parts" / name).write_text(text + "\n")
    for name, text in SHADOW_PRIMS.items():
        (shadow / "p").mkdir(parents=True, exist_ok=True)
        (shadow / "p" / name).write_text(text + "\n")
    if as_zip:
        zpath = tmp / "complete.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            for p in lib.rglob("*.dat"):
                z.write(p, "ldraw/" + p.relative_to(lib).as_posix())
        return zpath, shadow
    return lib, shadow


def connectors(tmp: Path, as_zip: bool = False) -> c.Connectors:
    lib, shadow = write_libraries(tmp, as_zip)
    return c.Connectors(c.PartLibrary(lib), c.ShadowLibrary(shadow))


def pieces_of(text: str):
    pieces, _ = ldraw.flatten(ldraw.parse(text))
    return pieces


class RecordTests(unittest.TestCase):
    def test_grid_forms(self):
        self.assertEqual(len(c._grid("C 8 C 4 20 20")), 32)
        self.assertEqual(len(c._grid("8 4 20 20")), 32)
        self.assertEqual(len(c._grid("C 6 1 20 0")), 6)
        self.assertEqual(c._grid(None), [(0.0, 0.0)])
        centred = c._grid("C 2 C 2 20 20")
        self.assertEqual(sorted(centred), [(-10.0, -10.0), (-10.0, 10.0), (10.0, -10.0), (10.0, 10.0)])
        corner = c._grid("2 2 20 20")
        self.assertEqual(sorted(corner), [(0.0, 0.0), (0.0, 20.0), (20.0, 0.0), (20.0, 20.0)])

    def test_cyl_with_orientation(self):
        feats = c.cyl_features({"gender": "F", "secs": "R 4 4", "pos": "0 -4 0",
                                "ori": "1 0 0 0 -1 0 0 0 -1"})
        self.assertEqual(len(feats), 1)
        self.assertEqual(feats[0].axis, (0.0, -1.0, 0.0))
        self.assertEqual(feats[0].radius, 4.0)

    def test_shadow_parse_kinds(self):
        s = c.parse_shadow(
            "0 !LDCAD SNAP_CLEAR\n0 !LDCAD SNAP_INCL [ref=connhole.dat] [pos=-20 -20 0]\n"
            "0 !LDCAD SNAP_CLP [gender=M] [radius=4]\n0 !LDCAD SNAP_CYL [gender=M] [secs=R 6 4]\n"
        )
        self.assertTrue(s.clear)
        self.assertEqual(s.includes[0][0], "connhole.dat")
        self.assertEqual(s.other, 1)
        self.assertEqual(len(s.cyls), 1)


class FeatureTests(unittest.TestCase):
    def test_brick_has_eight_studs_and_eight_sockets(self):
        with tempfile.TemporaryDirectory() as tmp:
            feats, other = connectors(Path(tmp)).features("3001")
        males = [f for f in feats if f.gender == "M"]
        females = [f for f in feats if f.gender == "F"]
        self.assertEqual(len(males), 8)
        self.assertEqual(len(females), 8)
        self.assertEqual({f.pos[1] for f in males}, {0.0})
        self.assertEqual({f.pos[1] for f in females}, {24.0})
        self.assertEqual(other, 0)

    def test_studs_found_through_nested_primitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            feats, _ = connectors(Path(tmp)).features("3005")
        self.assertEqual([f.gender for f in feats].count("M"), 1)

    def test_unknown_part_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(connectors(Path(tmp)).features("99999"))

    def test_zip_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            feats, _ = connectors(Path(tmp), as_zip=True).features("3035")
        self.assertEqual(len([f for f in feats if f.gender == "M"]), 32)


class AnalyseTests(unittest.TestCase):
    def analyse(self, text, boxes=None):
        with tempfile.TemporaryDirectory() as tmp:
            pieces = pieces_of(text)
            return pieces, c.analyse(pieces, connectors(Path(tmp)), boxes)

    def test_brick_on_plate_connects(self):
        _, rep = self.analyse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
            "1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"  # brick top at -24, bottom at 0 = plate top
        )
        self.assertEqual(len(rep.groups), 1)
        self.assertEqual(rep.connections, 8)

    def test_offset_by_half_a_stud_does_not_connect(self):
        _, rep = self.analyse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
            "1 4 20 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        )
        self.assertEqual(len(rep.groups), 1)  # shifted by exactly one stud still lines up
        _, rep = self.analyse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
            "1 4 5 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        )
        self.assertEqual(len(rep.groups), 2)

    def test_floating_brick_is_its_own_group(self):
        _, rep = self.analyse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
            "1 4 0 -40 0 1 0 0 0 1 0 0 0 1 3001.dat\n"  # a gap under it
        )
        self.assertEqual([len(g) for g in rep.groups], [1, 1])

    def test_touching_but_sideways_does_not_connect(self):
        # brick rotated 90 degrees about X: its studs point along Z, not into the plate
        _, rep = self.analyse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
            "1 4 0 -24 0 1 0 0 0 0 -1 0 1 0 3001.dat\n"
        )
        self.assertEqual(len(rep.groups), 2)

    def test_rotated_brick_still_connects(self):
        # brick turned 90 degrees about Y: studs still point up, lands on the plate grid
        _, rep = self.analyse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
            "1 4 0 -24 0 0 0 1 0 1 0 -1 0 0 3001.dat\n"
        )
        self.assertEqual(len(rep.groups), 1)

    def test_tile_on_top_connects_but_nothing_on_a_tile(self):
        _, rep = self.analyse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
            "1 4 -30 -8 -10 1 0 0 0 1 0 0 0 1 3070b.dat\n"   # tile on the brick: connected
            "1 4 -30 -16 -10 1 0 0 0 1 0 0 0 1 4073.dat\n"  # round plate on the tile: loose
        )
        self.assertEqual([len(g) for g in rep.groups], [2, 1])

    def test_unknown_part_is_reported_not_flagged(self):
        pieces, rep = self.analyse(
            "0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
            "1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
            "1 4 200 0 0 1 0 0 0 1 0 0 0 1 99999.dat\n"
        )
        self.assertEqual(rep.unknown, [2])

    def test_press_fit_overlap_joins(self):
        text = ("0 FILE a.ldr\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
                "1 4 300 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n")
        boxes = [((-80, -4, -40), (80, 8, 40)), ((-80, -4, -40), (80, 8, 40))]  # pretend they overlap
        _, rep = self.analyse(text, boxes)
        self.assertEqual(len(rep.groups), 1)


class CheckTests(unittest.TestCase):
    def test_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connectors(Path(tmp))
            pieces = pieces_of(
                "0 FILE a.ldr\n0 // base\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3035.dat\n"
                "0 // wall\n1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
                "0 // lamp\n1 14 0 -60 0 1 0 0 0 1 0 0 0 1 4073.dat\n"
                "1 4 200 0 0 1 0 0 0 1 0 0 0 1 99999.dat\n"
            )
            found = checks.check_connectivity(pieces, conn, None)
        errors = [f for f in found if f.level == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("1 piece(s) are not clipped to the model", errors[0].message)
        self.assertEqual(errors[0].pieces[0].label, "lamp")
        self.assertTrue(any("no connector data" in f.message for f in found))


class PipelineTests(unittest.TestCase):
    def test_example_shed_flags_only_the_lamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib, shadow = write_libraries(Path(tmp))
            result = cli.run_pipeline(EXAMPLE, Path(tmp) / "out", ldraw_dir=lib, shadow_dir=shadow)
            report = (Path(tmp) / "out" / "report.md").read_text()
            request = (Path(tmp) / "out" / "feedback.md").read_text()
        conn = [f for f in result.findings if f.check == "connectivity"]
        self.assertEqual([f.level for f in conn], ["error"])
        self.assertIn("1 piece(s) are not clipped", conn[0].message)
        self.assertEqual(conn[0].pieces[0].part, "4073")
        self.assertNotIn("connectivity check was skipped", report)
        self.assertIn("not clipped to the model", request)

    def test_skipped_without_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_pipeline(EXAMPLE, Path(tmp) / "out")
            report = (Path(tmp) / "out" / "report.md").read_text()
        self.assertFalse([f for f in result.findings if f.check == "connectivity"])
        self.assertIn("connectivity check was skipped", report)


class DownloadTests(unittest.TestCase):
    def test_download_writes_zip_and_is_found(self):
        import io
        with tempfile.TemporaryDirectory() as tmp:
            lib, _ = write_libraries(Path(tmp), as_zip=True)
            payload = lib.read_bytes()

            class Resp(io.BytesIO):
                headers = {"Content-Length": str(len(payload))}
                def __enter__(self): return self
                def __exit__(self, *a): self.close()

            seen = []
            target = Path(tmp) / "managed"
            got = c.download_library(target, progress=lambda d, t: seen.append((d, t)),
                                     opener=lambda req: Resp(payload))
            self.assertEqual(got, target / "complete.zip")
            self.assertEqual(seen[-1], (len(payload), len(payload)))
            self.assertFalse((target / "complete.zip.part").exists())
            self.assertEqual(c.find_ldraw_library([got]), got)

    def test_bad_download_is_rejected(self):
        import io
        with tempfile.TemporaryDirectory() as tmp:
            class Resp(io.BytesIO):
                headers = {}
                def __enter__(self): return self
                def __exit__(self, *a): self.close()
            with self.assertRaises(OSError):
                c.download_library(Path(tmp), opener=lambda req: Resp(b"<html>not a zip"))
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
