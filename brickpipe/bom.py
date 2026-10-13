"""Bill of materials: count pieces and export shopping lists."""
from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .catalog import Colour, PartCatalog
from .ldraw import PlacedPart


@dataclass(frozen=True)
class BomRow:
    part: str
    colour: int
    quantity: int


def build(pieces: list[PlacedPart]) -> list[BomRow]:
    counts = Counter((p.part, p.colour) for p in pieces)
    return [
        BomRow(part, colour, qty)
        for (part, colour), qty in sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]


def to_csv(
    rows: list[BomRow], colours: dict[int, Colour], catalog: PartCatalog | None = None
) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["ldraw_part", "description", "ldraw_colour", "colour_name", "bricklink_colour", "quantity"]
    )
    for row in rows:
        colour = colours.get(row.colour)
        writer.writerow(
            [
                row.part,
                (catalog.description(row.part) if catalog else None) or "",
                row.colour,
                colour.name if colour else "",
                colour.bricklink_id if colour and colour.bricklink_id is not None else "",
                row.quantity,
            ]
        )
    return out.getvalue()


def to_bricklink_xml(rows: list[BomRow], colours: dict[int, Colour]) -> tuple[str, list[BomRow]]:
    """BrickLink wanted-list XML (Want > Upload).

    Returns (xml, skipped). Rows whose colour has no BrickLink id in the table
    are left out and returned so the caller can report them.

    LDraw part ids are used as BrickLink item ids. They match for most common
    parts, but not all: BrickLink flags unknown ids when the file is uploaded.
    """
    items: list[str] = []
    skipped: list[BomRow] = []
    for row in rows:
        colour = colours.get(row.colour)
        if colour is None or colour.bricklink_id is None:
            skipped.append(row)
            continue
        items.append(
            "  <ITEM>"
            "<ITEMTYPE>P</ITEMTYPE>"
            f"<ITEMID>{escape(row.part)}</ITEMID>"
            f"<COLOR>{colour.bricklink_id}</COLOR>"
            f"<MINQTY>{row.quantity}</MINQTY>"
            "</ITEM>"
        )
    return "<INVENTORY>\n" + "\n".join(items) + "\n</INVENTORY>\n", skipped


def to_flat_ldr(pieces: list[PlacedPart], title: str) -> str:
    """Every piece in one flat model, with no submodels.

    BrickLink and Rebrickable both accept an LDraw file upload and do their
    own part and colour matching, which is more reliable than the bundled
    table. A flat file avoids any trouble with nested submodels on upload.
    """
    lines = [f"0 {title}", f"0 Name: {title}.ldr", "0 Author: brickpipe"]
    lines.extend(p.to_line() for p in pieces)
    return "\n".join(lines) + "\n"
