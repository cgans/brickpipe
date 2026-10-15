"""The Nova database reader, against a small stand-in database.

This also runs on Windows in CI, where the database path has a drive letter
and backslashes.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from brickpipe import cli
from brickpipe.catalog import PartCatalog

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "garden-shed.mpd"

PARTS = {
    # alias: (description, min xyz, max xyz)
    "3001.dat": ("Brick  2 x  4", (-40, -4, -20), (40, 24, 20)),
    "3035.dat": ("Plate  4 x  8", (-80, -4, -40), (80, 8, 40)),
    "4073.dat": ("~Moved to 6141", (-10, -4, -10), (10, 8, 10)),
}


def make_db(folder: Path) -> Path:
    path = folder / "nova db" / "ldraw-info.db"  # a space, as in "Program Files"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE PART_INFOS (alias VARCHAR PRIMARY KEY, name VARCHAR, description TEXT)")
    db.execute(
        "CREATE TABLE PART_BBOXES (alias VARCHAR PRIMARY KEY, min_x REAL, min_y REAL, "
        "min_z REAL, max_x REAL, max_y REAL, max_z REAL)"
    )
    for alias, (desc, lo, hi) in PARTS.items():
        db.execute("INSERT INTO PART_INFOS VALUES (?, ?, ?)", (alias, alias, desc))
        db.execute("INSERT INTO PART_BBOXES VALUES (?, ?, ?, ?, ?, ?, ?)", (alias, *lo, *hi))
    db.commit()
    db.close()
    return path


class CatalogTests(unittest.TestCase):
    def test_reads_names_and_boxes(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = PartCatalog(make_db(Path(tmp)))
            try:
                self.assertEqual(catalog.description("3001"), "Brick  2 x  4")
                self.assertEqual(catalog.bbox("3035").max, (80, 8, 40))
                self.assertIsNone(catalog.description("nope"))
                self.assertIsNone(catalog.bbox("nope"))
            finally:
                catalog.close()

    def test_missing_database_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            PartCatalog(Path("no-such-folder") / "ldraw-info.db")

    def test_full_run_with_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            code = cli.main([str(EXAMPLE), "-o", str(out), "--nova-db", str(make_db(Path(tmp)))])
            report = (out / "report.md").read_text(encoding="utf-8")
            parts = (out / "parts.csv").read_text(encoding="utf-8")
            request = (out / "feedback.md").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("Brick  2 x  4", parts)
        self.assertIn("[floating] 1 piece(s) touch nothing", report)
        self.assertIn("4073 (1x) is an old id", report)
        self.assertIn("lamp: a yellow stud that is not attached to anything", request)


if __name__ == "__main__":
    unittest.main()
