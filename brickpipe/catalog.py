"""Colour and part reference data.

Colours come from the bundled colours.csv (LDraw code -> name and BrickLink
colour id). Treat that table as a starting point: check the colours in a
wanted list before paying for an order.

Part names and bounding boxes are optional and come from the read-only
SQLite database that ships with LDraw Nova (data/ldraw-info.db). brickpipe
never bundles or modifies that file; point at it with --nova-db.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class Colour:
    code: int
    name: str
    bricklink_id: int | None


def load_colours() -> dict[int, Colour]:
    text = resources.files("brickpipe").joinpath("data/colours.csv").read_text("utf-8")
    table: dict[int, Colour] = {}
    for row in csv.DictReader(text.splitlines()):
        code = int(row["ldraw_code"])
        bl = row["bricklink_id"].strip()
        table[code] = Colour(code, row["name"], int(bl) if bl else None)
    return table


@dataclass(frozen=True)
class BBox:
    """Part bounding box in the part's own coordinates (LDraw units)."""

    min: tuple[float, float, float]
    max: tuple[float, float, float]


class PartCatalog:
    """Part descriptions and bounding boxes from LDraw Nova's database."""

    def __init__(self, db_path: str | Path):
        path = Path(db_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Nova database not found: {path}")
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._names: dict[str, str | None] = {}
        self._boxes: dict[str, BBox | None] = {}

    def description(self, part: str) -> str | None:
        if part not in self._names:
            row = self._db.execute(
                "SELECT description FROM PART_INFOS WHERE alias = ?", (f"{part}.dat",)
            ).fetchone()
            self._names[part] = row[0] if row else None
        return self._names[part]

    def bbox(self, part: str) -> BBox | None:
        if part not in self._boxes:
            row = self._db.execute(
                "SELECT min_x, min_y, min_z, max_x, max_y, max_z "
                "FROM PART_BBOXES WHERE alias = ?",
                (f"{part}.dat",),
            ).fetchone()
            try:
                box = BBox(tuple(map(float, row[:3])), tuple(map(float, row[3:]))) if row else None
            except (TypeError, ValueError):
                box = None
            self._boxes[part] = box
        return self._boxes[part]
