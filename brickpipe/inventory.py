"""Bricks the user already has, and what is left to buy after them.

An inventory is a count per part and colour. It can come from:

* Rebrickable, where many people record the sets and loose parts they own.
  The API's `allparts` call lists everything, as Rebrickable part numbers and
  Rebrickable colour ids.
* A CSV or pasted lines: `part,colour,quantity`, where part is an LDraw or
  BrickLink id and colour is an LDraw code or a colour name.

Subtracting it from a parts list gives, per lot, how many are in hand and how
many to buy. The BrickLink wanted list then carries only what is left.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field

from .bom import BomRow
from .catalog import Colour


@dataclass
class Inventory:
    by_ldraw: dict[tuple[str, int], int] = field(default_factory=dict)  # (part, LDraw colour)
    by_rebrickable: dict[tuple[str, int], int] = field(default_factory=dict)  # (part_num, colour id)
    problems: list[str] = field(default_factory=list)
    source: str = ""

    def total(self) -> int:
        return sum(self.by_ldraw.values()) + sum(self.by_rebrickable.values())

    def add_ldraw(self, part: str, colour: int, quantity: int) -> None:
        key = (part.strip().lower(), int(colour))
        self.by_ldraw[key] = self.by_ldraw.get(key, 0) + int(quantity)

    def add_rebrickable(self, part_num: str, colour_id: int, quantity: int) -> None:
        key = (str(part_num).strip().lower(), int(colour_id))
        self.by_rebrickable[key] = self.by_rebrickable.get(key, 0) + int(quantity)


def from_rebrickable_items(items: list[dict]) -> Inventory:
    """Items as Rebrickable's /users/{token}/allparts/ returns them.

    Accepts both the nested shape ({"part": {"part_num"}, "color": {"id"},
    "quantity"}) and flat fields, since the response is not documented.
    """
    inv = Inventory(source="rebrickable")
    for item in items:
        part = item.get("part") or {}
        colour = item.get("color") or {}
        part_num = (part.get("part_num") if isinstance(part, dict) else None) or item.get("part_num")
        colour_id = colour.get("id") if isinstance(colour, dict) and "id" in colour else item.get("color_id")
        qty = item.get("quantity")
        if part_num is None or colour_id is None or qty is None:
            inv.problems.append(f"skipped an item with missing fields: {list(item)[:6]}")
            continue
        try:
            inv.add_rebrickable(str(part_num), int(colour_id), int(qty))
        except (TypeError, ValueError):
            inv.problems.append(f"skipped an item with odd values: {part_num} {colour_id} {qty}")
    return inv


def from_text(text: str, colours: dict[int, Colour]) -> Inventory:
    """`part,colour,quantity` per line; a header line is fine. Colour is an
    LDraw code (15), a colour name (White), or empty for "any colour"
    (recorded as colour -1 and matched last)."""
    inv = Inventory(source="text")
    names = {c.name.lower(): code for code, c in colours.items()}
    reader = csv.reader(io.StringIO(text))
    for n, row in enumerate(reader, 1):
        cells = [c.strip() for c in row]
        if not any(cells) or cells[0].startswith("#"):
            continue
        if n == 1 and cells[0].lower() in ("part", "ldraw_part", "item"):
            continue
        if len(cells) < 2:
            inv.problems.append(f"line {n}: expected part,colour,quantity")
            continue
        part, colour_text = cells[0], cells[1]
        qty_text = cells[2] if len(cells) > 2 and cells[2] else "1"
        try:
            qty = int(float(qty_text))
        except ValueError:
            inv.problems.append(f"line {n}: quantity '{qty_text}' is not a number")
            continue
        if colour_text == "":
            colour = -1
        elif colour_text.lstrip("-").isdigit():
            colour = int(colour_text)
        elif colour_text.lower() in names:
            colour = names[colour_text.lower()]
        else:
            inv.problems.append(f"line {n}: unknown colour '{colour_text}'")
            continue
        inv.add_ldraw(part, colour, qty)
    return inv


@dataclass
class Covered:
    row: BomRow
    have: int

    @property
    def buy(self) -> int:
        return max(0, self.row.quantity - self.have)


def subtract(
    rows: list[BomRow],
    inventory: Inventory,
    aliases: dict[tuple[str, int], tuple[str | None, int | None, str | None]] | None = None,
    leftover: dict[tuple[str, int], int] | None = None,
) -> list[Covered]:
    """How much of each lot the inventory covers.

    If `leftover` is given, it is filled with the typed/CSV entries (LDraw
    keys) that no lot used, so a typo or wrong colour does not go unnoticed.

    `aliases` maps (LDraw part, LDraw colour) to (Rebrickable part_num,
    Rebrickable colour id, BrickLink part id) when known from the availability
    check; it lets a Rebrickable inventory match LDraw lots and a typed
    BrickLink id match either.
    """
    ldraw = dict(inventory.by_ldraw)
    rb = dict(inventory.by_rebrickable)
    out: list[Covered] = []
    for row in rows:
        need = row.quantity
        have = 0
        rb_part, rb_colour, bl_part = (aliases or {}).get((row.part, row.colour), (None, None, None))
        keys_ldraw = [(row.part, row.colour)]
        if bl_part and bl_part.lower() != row.part:
            keys_ldraw.append((bl_part.lower(), row.colour))
        if rb_part and rb_part.lower() != row.part:
            keys_ldraw.append((rb_part.lower(), row.colour))
        keys_ldraw += [(p, -1) for p, _ in list(keys_ldraw)]  # "any colour" entries last
        for key in keys_ldraw:
            if need <= have and key[1] != -1:
                break
            got = ldraw.get(key, 0)
            take = min(got, need - have)
            if take > 0:
                ldraw[key] = got - take
                have += take
        if have < need and rb_part is not None and rb_colour is not None:
            key = (rb_part.lower(), rb_colour)
            got = rb.get(key, 0)
            take = min(got, need - have)
            if take > 0:
                rb[key] = got - take
                have += take
        out.append(Covered(row, have))
    if leftover is not None:
        leftover.update({k: v for k, v in ldraw.items() if v > 0})
    return out


def to_csv(covered: list[Covered], colours: dict[int, Colour],
           leftover: dict[tuple[str, int], int] | None = None) -> str:
    """Rows with need 0 at the end are listed bricks the model does not use."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["ldraw_part", "ldraw_colour", "colour_name", "need", "have", "buy"])
    for c in covered:
        colour = colours.get(c.row.colour)
        writer.writerow([c.row.part, c.row.colour, colour.name if colour else "",
                         c.row.quantity, c.have, c.buy])
    for (part, code), qty in sorted((leftover or {}).items()):
        colour = colours.get(code)
        writer.writerow([part, code, colour.name if colour else ("any" if code == -1 else ""),
                         0, qty, 0])
    return out.getvalue()


def remaining_rows(covered: list[Covered]) -> list[BomRow]:
    return [BomRow(c.row.part, c.row.colour, c.buy) for c in covered if c.buy > 0]


def summary(covered: list[Covered], leftover: dict[tuple[str, int], int] | None = None) -> dict:
    need = sum(c.row.quantity for c in covered)
    have = sum(c.have for c in covered)
    return {"need": need, "have": have, "buy": need - have,
            "lots_covered": sum(1 for c in covered if c.buy == 0),
            "lots_total": len(covered),
            "unused": sum((leftover or {}).values()),
            "unused_lots": len(leftover or {})}
