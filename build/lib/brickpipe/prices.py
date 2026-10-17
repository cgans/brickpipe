"""What the wanted list would cost, from BrickLink's price guide.

Runs after the owned bricks are subtracted, so it prices only what is left
to buy. One price-guide call per lot, using the BrickLink item and colour ids
the availability check resolved (or the LDraw ids when it did not run).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal

from . import bricklink
from .bom import BomRow
from .catalog import Colour
from .checks import Finding

PRICEY_UNIT = Decimal("1.50")  # a single piece costing more than this gets flagged
FEW_LOTS = 3


@dataclass
class Line:
    row: BomRow
    item: str
    colour_id: int | None
    colour_name: str
    avg: Decimal | None = None  # per piece
    low: Decimal | None = None
    high: Decimal | None = None
    lots: int = 0  # sellers listing it
    pieces: int = 0  # pieces for sale across them
    note: str = ""

    @property
    def total(self) -> Decimal:
        return (self.avg or Decimal(0)) * self.row.quantity

    @property
    def total_low(self) -> Decimal:
        return (self.low or Decimal(0)) * self.row.quantity


@dataclass
class Estimate:
    lines: list[Line] = field(default_factory=list)
    currency: str = ""
    guide_type: str = "stock"
    new_or_used: str = "N"
    region: str = ""
    error: str | None = None

    @property
    def total(self) -> Decimal:
        return sum((ln.total for ln in self.lines), Decimal(0))

    @property
    def total_low(self) -> Decimal:
        return sum((ln.total_low for ln in self.lines), Decimal(0))

    @property
    def unpriced(self) -> list[Line]:
        return [ln for ln in self.lines if ln.avg is None]

    def summary(self) -> dict:
        return {
            "total": str(self.total.quantize(Decimal("0.01"))),
            "total_low": str(self.total_low.quantize(Decimal("0.01"))),
            "currency": self.currency, "lots": len(self.lines),
            "unpriced": len(self.unpriced), "pieces": sum(ln.row.quantity for ln in self.lines),
            "guide_type": self.guide_type, "new_or_used": self.new_or_used, "region": self.region,
            "error": self.error,
        }


def _money(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except ArithmeticError:
        return None


def estimate(
    rows: list[BomRow],
    colours: dict[int, Colour],
    client: bricklink.Client,
    overrides: dict[tuple[str, int], tuple[str, int]] | None = None,
    *,
    guide_type: str = "stock",
    new_or_used: str = "N",
    region: str = "",
    currency: str = "",
) -> Estimate:
    """Price every row. Rows BrickLink does not know, or nobody is selling,
    get no price and a note; a BrickLink error stops the estimate and is
    recorded in `error` rather than raised, so the run still finishes."""
    est = Estimate(guide_type=guide_type, new_or_used=new_or_used, region=region, currency=currency)
    for row in rows:
        if overrides and (row.part, row.colour) in overrides:
            item, colour_id = overrides[(row.part, row.colour)]
        else:
            colour = colours.get(row.colour)
            item, colour_id = row.part, (colour.bricklink_id if colour else None)
        colour_name = colours[row.colour].name if row.colour in colours else str(row.colour)
        line = Line(row, item, colour_id, colour_name)
        est.lines.append(line)
        if colour_id is None:
            line.note = "colour has no BrickLink id"
            continue
        try:
            data = client.price(item, colour_id, guide_type=guide_type, new_or_used=new_or_used,
                                region=region, currency_code=currency)
        except bricklink.BrickLinkError as err:
            est.error = str(err)
            break
        if not data:
            line.note = "not in BrickLink's catalogue under this id"
            continue
        line.lots = int(data.get("unit_quantity") or 0)
        line.pieces = int(data.get("total_quantity") or 0)
        est.currency = est.currency or str(data.get("currency_code") or "")
        if line.lots == 0 or line.pieces == 0:
            line.note = "nobody is selling it" if guide_type == "stock" else "none sold in six months"
            continue
        line.avg = _money(data.get("qty_avg_price")) or _money(data.get("avg_price"))
        line.low = _money(data.get("min_price"))
        line.high = _money(data.get("max_price"))
        if line.lots <= FEW_LOTS:
            line.note = f"only {line.lots} seller(s)"
    return est


def findings(est: Estimate) -> list[Finding]:
    """Problems the agent can act on: lots nobody sells, and lots that are
    expensive per piece or come from very few sellers."""
    out: list[Finding] = []
    if est.error:
        out.append(Finding("warning", "price", f"Price estimate did not finish: {est.error}"))
    for ln in est.lines:
        where = f"{ln.item} in {ln.colour_name} (BrickLink colour {ln.colour_id})"
        if ln.colour_id is None or "not in BrickLink" in ln.note:
            continue  # the availability check already reports unknown parts and colours
        if ln.avg is None:
            out.append(Finding(
                "warning", "price",
                f"{where}: {ln.note}, so it cannot be bought on BrickLink right now",
                fix="Use a different part or colour for this piece",
            ))
        elif ln.avg >= PRICEY_UNIT:
            out.append(Finding(
                "warning", "price",
                f"{where}: about {ln.avg:.2f} {est.currency} each, {ln.total:.2f} for {ln.row.quantity}"
                + (f", from only {ln.lots} seller(s)" if ln.lots <= FEW_LOTS else ""),
                fix="Swap it for a commoner part or colour if the design allows",
            ))
        elif ln.lots <= FEW_LOTS:
            out.append(Finding(
                "warning", "price",
                f"{where}: only {ln.lots} seller(s) list it, which makes it hard to order",
                fix="Swap it for a commoner part or colour if the design allows",
            ))
    return out


def note(est: Estimate) -> str:
    """One sentence for the report and the page."""
    condition = "new" if est.new_or_used == "N" else "used"
    source = "current listings" if est.guide_type == "stock" else "sales in the last six months"
    where = f" in {est.region.replace('_', ' ')}" if est.region else ""
    priced = [ln for ln in est.lines if ln.avg is not None]
    if not priced:
        text = "No price could be found for any lot"
    else:
        text = (f"Estimated cost: about {est.total:.2f} {est.currency} for the "
                f"{sum(ln.row.quantity for ln in priced)} pieces to buy ({condition}, {source}{where}), "
                f"or {est.total_low:.2f} at every seller's lowest price")
    if est.unpriced:
        text += f"; {len(est.unpriced)} lot(s) had no price"
    if est.error:
        text += f". The estimate stopped early: {est.error}"
    return text + ". Details are in `prices.csv`."


def to_csv(est: Estimate) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["ldraw_part", "ldraw_colour", "colour_name", "bricklink_item", "bricklink_colour",
                     "quantity", "avg_each", "lowest_each", "highest_each", "line_total", "sellers",
                     "pieces_for_sale", "currency", "note"])
    for ln in est.lines:
        fmt = lambda v: "" if v is None else f"{v:.2f}"  # noqa: E731
        writer.writerow([ln.row.part, ln.row.colour, ln.colour_name, ln.item,
                         "" if ln.colour_id is None else ln.colour_id, ln.row.quantity,
                         fmt(ln.avg), fmt(ln.low), fmt(ln.high), fmt(ln.total) if ln.avg is not None else "",
                         ln.lots, ln.pieces, est.currency, ln.note])
    return out.getvalue()
