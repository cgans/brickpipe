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


def to_bricklink_xml(
    rows: list[BomRow],
    colours: dict[int, Colour],
    overrides: dict[tuple[str, int], tuple[str, int]] | None = None,
) -> tuple[str, list[BomRow]]:
    """BrickLink wanted-list XML (Want > Upload).

    Returns (xml, skipped). Rows whose colour has no BrickLink id are left out
    and returned so the caller can report them.

    Without `overrides`, LDraw part ids are used as BrickLink item ids and
    colours come from the bundled table. `overrides` maps (LDraw part, LDraw
    colour) to BrickLink's own (item id, colour id), as returned by the
    availability check, and wins where present. Rows that land on the same
    BrickLink item and colour are merged.
    """
    wanted: dict[tuple[str, int], int] = {}
    skipped: list[BomRow] = []
    for row in rows:
        if overrides and (row.part, row.colour) in overrides:
            item_id, colour_id = overrides[(row.part, row.colour)]
        else:
            colour = colours.get(row.colour)
            if colour is None or colour.bricklink_id is None:
                skipped.append(row)
                continue
            item_id, colour_id = row.part, colour.bricklink_id
        wanted[(item_id, colour_id)] = wanted.get((item_id, colour_id), 0) + row.quantity

    items = [
        "  <ITEM>"
        "<ITEMTYPE>P</ITEMTYPE>"
        f"<ITEMID>{escape(item_id)}</ITEMID>"
        f"<COLOR>{colour_id}</COLOR>"
        f"<MINQTY>{quantity}</MINQTY>"
        "</ITEM>"
        for (item_id, colour_id), quantity in wanted.items()
    ]
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
