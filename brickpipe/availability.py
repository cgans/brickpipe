"""Was each part ever produced in the colour the model asks for?

Uses Rebrickable's catalogue of official sets. A part and colour combination
that appears in no set was almost certainly never moulded, so it cannot be
bought. Combinations that appear in only a few sets exist but tend to be
scarce and expensive.

The same lookups also return BrickLink's own ids for the part and colour,
which replace the guesses used when this check is not run.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from .bom import BomRow
from .catalog import PartCatalog
from .ldraw import PlacedPart
from .checks import Finding
from .rebrickable import Client

RARE_SETS = 3  # in this many sets or fewer counts as rare

OK = "ok"
RARE = "rare"
NOT_MADE = "not made in this colour"
PART_UNKNOWN = "part not found"
COLOUR_UNKNOWN = "colour not found"
NOT_A_PART = "not a part"


@dataclass
class Availability:
    row: BomRow
    status: str
    rebrickable_part: str = ""
    bricklink_part: str = ""
    bricklink_colour: int | None = None
    colour_name: str = ""
    sets: int = 0
    alternatives: list[str] = field(default_factory=list)  # colour names


def ext_ids(item: dict, system: str) -> list:
    """Read external ids from either shape Rebrickable uses.

    Parts:   {"external_ids": {"LDraw": ["3001"]}}
    Colours: {"external_ids": {"LDraw": {"ext_ids": [0, 256]}}}
    """
    value = (item.get("external_ids") or {}).get(system)
    if isinstance(value, dict):
        value = value.get("ext_ids")
    return [v for v in (value or []) if v is not None]


def index_colours(colours: list[dict]) -> dict[int, dict]:
    """LDraw colour code -> Rebrickable colour record."""
    table: dict[int, dict] = {}
    for colour in colours:
        for code in ext_ids(colour, "LDraw"):
            try:
                table.setdefault(int(code), colour)
            except (TypeError, ValueError):
                continue
    return table


def moved_target(part: str, catalog: PartCatalog | None) -> str | None:
    """LDraw marks renamed parts '~Moved to <new id>'."""
    desc = catalog.description(part) if catalog else None
    if desc and desc.startswith("~Moved to "):
        return desc[len("~Moved to "):].strip().lower() or None
    return None


def resolve_parts(
    parts: list[str], client: Client, catalog: PartCatalog | None
) -> dict[str, dict]:
    """LDraw part id -> Rebrickable part record, for every part that resolves."""
    resolved: dict[str, dict] = {}

    # Most ids are shared between the two catalogues, so try them in bulk.
    by_num = {p["part_num"].lower(): p for p in client.parts_by_number(parts)}
    for part in parts:
        record = by_num.get(part)
        if record is None:
            continue
        ldraw = [str(i).lower() for i in ext_ids(record, "LDraw")]
        if not ldraw or part in ldraw:
            resolved[part] = record

    # The rest are looked up one at a time by LDraw id, then by renamed id.
    for part in parts:
        if part in resolved:
            continue
        candidates = [part] + ([t] if (t := moved_target(part, catalog)) else [])
        for candidate in candidates:
            matches = client.parts_by_ldraw_id(candidate)
            if not matches and candidate != part:
                matches = client.parts_by_number([candidate])
            if matches:
                resolved[part] = matches[0]
                break
    return resolved


def check(
    rows: list[BomRow], client: Client, catalog: PartCatalog | None = None
) -> list[Availability]:
    colour_table = index_colours(client.colours())
    real_parts = sorted({r.part for r in rows if "/" not in r.part})
    part_table = resolve_parts(real_parts, client, catalog)
    produced = {
        part: client.part_colours(record["part_num"])
        for part, record in part_table.items()
    }

    results: list[Availability] = []
    for row in rows:
        if "/" in row.part:
            results.append(Availability(row, NOT_A_PART))
            continue
        record = part_table.get(row.part)
        colour = colour_table.get(row.colour)
        item = Availability(row, OK)
        if colour:
            item.colour_name = colour.get("name", "")
            bl = ext_ids(colour, "BrickLink")
            item.bricklink_colour = int(bl[0]) if bl else None
        if record:
            item.rebrickable_part = record["part_num"]
            bl_parts = ext_ids(record, "BrickLink")
            item.bricklink_part = str(bl_parts[0]) if bl_parts else ""

        if record is None:
            item.status = PART_UNKNOWN
        elif colour is None:
            item.status = COLOUR_UNKNOWN
        else:
            made = {c["color_id"]: c for c in produced[row.part]}
            hit = made.get(colour["id"])
            if hit is None:
                item.status = NOT_MADE
                common = sorted(made.values(), key=lambda c: -c.get("num_sets", 0))
                item.alternatives = [c.get("color_name", "") for c in common[:5]]
            else:
                item.sets = hit.get("num_sets", 0)
                item.status = RARE if item.sets <= RARE_SETS else OK
        results.append(item)
    return results


def findings(
    results: list[Availability], pieces: list[PlacedPart] | None = None
) -> list[Finding]:
    out: list[Finding] = []
    for a in results:
        what = f"{a.row.quantity}x {a.row.part}"
        used = tuple(
            p for p in pieces or () if (p.part, p.colour) == (a.row.part, a.row.colour)
        )
        if a.status == NOT_MADE:
            alt = f" It was made in: {', '.join(a.alternatives)}." if a.alternatives else ""
            out.append(Finding(
                "error", "availability",
                f"{what} was never produced in {a.colour_name or a.row.colour}.{alt}",
                "Recolour these pieces to a colour the part was made in, or swap "
                "them for a similar part that exists in the wanted colour.",
                used,
            ))
        elif a.status == PART_UNKNOWN:
            out.append(Finding(
                "warning", "availability",
                f"{what} could not be matched to a Rebrickable part, so it was not checked",
            ))
        elif a.status == COLOUR_UNKNOWN:
            out.append(Finding(
                "warning", "availability",
                f"{what}: LDraw colour {a.row.colour} is unknown to Rebrickable",
                "Use a standard LDraw colour code for these pieces.", used,
            ))
    rare = [a for a in results if a.status == RARE]
    if rare:
        pieces = sum(a.row.quantity for a in rare)
        out.append(Finding(
            "warning", "availability",
            f"{len(rare)} part and colour combination(s) ({pieces} pieces) appear in "
            f"{RARE_SETS} sets or fewer and may be scarce; see availability.csv",
        ))
    return out


def bricklink_overrides(results: list[Availability]) -> dict[tuple[str, int], tuple[str, int]]:
    """(LDraw part, LDraw colour) -> (BrickLink part id, BrickLink colour id)."""
    return {
        (a.row.part, a.row.colour): (a.bricklink_part, a.bricklink_colour)
        for a in results
        if a.bricklink_part and a.bricklink_colour is not None
    }


def to_csv(results: list[Availability]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([
        "ldraw_part", "ldraw_colour", "colour_name", "quantity", "status",
        "sets_containing", "rebrickable_part", "bricklink_part", "bricklink_colour",
        "made_in_instead",
    ])
    for a in results:
        writer.writerow([
            a.row.part, a.row.colour, a.colour_name, a.row.quantity, a.status,
            a.sets if a.status in (OK, RARE) else "",
            a.rebrickable_part, a.bricklink_part,
            "" if a.bricklink_colour is None else a.bricklink_colour,
            "; ".join(a.alternatives),
        ])
    return out.getvalue()
