"""Sanity checks that catch problems before any bricks are ordered.

None of this proves a model can be built. In particular the floating-piece
check only asks whether bounding boxes touch, which is necessary for two
pieces to be attached but not sufficient.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from .catalog import Colour, PartCatalog
from .ldraw import INHERIT_COLOUR, Document, PlacedPart, mat_vec


@dataclass(frozen=True)
class Finding:
    level: str  # "error" or "warning"
    check: str
    message: str


def check_colours(pieces: list[PlacedPart], colours: dict[int, Colour]) -> list[Finding]:
    out: list[Finding] = []
    unresolved = sum(1 for p in pieces if p.colour == INHERIT_COLOUR)
    if unresolved:
        out.append(Finding(
            "error", "colour",
            f"{unresolved} piece(s) have no real colour (colour 16 was never resolved)",
        ))
    counts = Counter(
        p.colour for p in pieces if p.colour != INHERIT_COLOUR and p.colour not in colours
    )
    for code, qty in sorted(counts.items()):
        out.append(Finding(
            "warning", "colour",
            f"LDraw colour {code} ({qty} piece(s)) is not in the colour table, "
            "so it is missing from the BrickLink wanted list",
        ))
    return out


def check_embedded(pieces: list[PlacedPart]) -> list[Finding]:
    counts = Counter((p.embedded_as, p.part) for p in pieces if p.embedded_as)
    return [
        Finding(
            "warning", "embedded",
            f"{qty} piece(s) use a part definition carried inside the file "
            f"({name}). Listed as part {part}; confirm that id before ordering.",
        )
        for (name, part), qty in sorted(counts.items())
    ]


def check_duplicates(pieces: list[PlacedPart]) -> list[Finding]:
    seen: Counter = Counter()
    for p in pieces:
        key = (p.part, tuple(round(v, 2) for v in p.pos), tuple(round(v, 3) for v in p.matrix))
        seen[key] += 1
    out = []
    for (part, pos, _), qty in seen.items():
        if qty > 1:
            out.append(Finding(
                "error", "duplicate",
                f"{qty} copies of {part} occupy the same spot at {pos}",
            ))
    return out


def check_parts(pieces: list[PlacedPart], catalog: PartCatalog) -> list[Finding]:
    out: list[Finding] = []
    for part, qty in sorted(Counter(p.part for p in pieces).items()):
        desc = catalog.description(part)
        if desc is None:
            out.append(Finding(
                "error", "part", f"{part} ({qty}x) is not in the LDraw part library"
            ))
        elif desc.startswith("~Moved to"):
            out.append(Finding(
                "warning", "part",
                f"{part} ({qty}x) is an old id: {desc.lstrip('~')}",
            ))
        elif "/" in part or desc.startswith(("~", "_", "=")):
            out.append(Finding(
                "warning", "part",
                f"{part} ({qty}x) looks like drawing geometry or a helper file, "
                f"not a piece you can order: {desc}",
            ))
    return out


def check_steps(doc: Document, max_parts: int) -> list[Finding]:
    out: list[Finding] = []
    for key, model in doc.models.items():
        if key.endswith(".dat"):
            continue  # a part definition, not something to build
        in_step = 0
        worst = 0
        for line in model.lines:
            if line.is_step:
                in_step = 0
            elif line.ref:
                in_step += 1
                worst = max(worst, in_step)
        if worst > max_parts * 2:
            out.append(Finding(
                "warning", "steps",
                f"{model.name} has a step with {worst} pieces, which will be hard to follow",
            ))
    return out


def world_box(piece: PlacedPart, catalog: PartCatalog):
    box = catalog.bbox(piece.part)
    if box is None:
        return None
    corners = [
        mat_vec(piece.matrix, (x, y, z))
        for x in (box.min[0], box.max[0])
        for y in (box.min[1], box.max[1])
        for z in (box.min[2], box.max[2])
    ]
    lo = tuple(piece.pos[i] + min(c[i] for c in corners) for i in range(3))
    hi = tuple(piece.pos[i] + max(c[i] for c in corners) for i in range(3))
    return lo, hi


def check_floating(
    pieces: list[PlacedPart], catalog: PartCatalog, tolerance: float = 1.0
) -> list[Finding]:
    """Flag groups of pieces whose boxes touch nothing in the main structure."""
    boxes = [(i, world_box(p, catalog)) for i, p in enumerate(pieces)]
    known = [(i, b) for i, b in boxes if b is not None]
    if len(known) < 2:
        return []

    parent = {i: i for i, _ in known}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    known.sort(key=lambda item: item[1][0][0])
    for a in range(len(known)):
        ia, (lo_a, hi_a) = known[a]
        for b in range(a + 1, len(known)):
            ib, (lo_b, hi_b) = known[b]
            if lo_b[0] > hi_a[0] + tolerance:
                break
            if all(
                lo_a[k] <= hi_b[k] + tolerance and lo_b[k] <= hi_a[k] + tolerance
                for k in (1, 2)
            ):
                parent[find(ia)] = find(ib)

    groups: dict[int, list[int]] = defaultdict(list)
    for i, _ in known:
        groups[find(i)].append(i)
    if len(groups) < 2:
        return []

    ordered = sorted(groups.values(), key=len, reverse=True)
    out = []
    for group in ordered[1:]:
        sample = pieces[group[0]]
        where = " > ".join(sample.path[1:]) or "main model"
        out.append(Finding(
            "warning", "floating",
            f"{len(group)} piece(s) touch nothing in the main structure "
            f"(e.g. {sample.part} in {where} at "
            f"{tuple(round(v, 1) for v in sample.pos)})",
        ))
    return out
