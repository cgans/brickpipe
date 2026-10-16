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
    fix: str = ""  # what the model's author should do; empty if nothing
    pieces: tuple[PlacedPart, ...] = ()  # the pieces involved, when known


def check_colours(pieces: list[PlacedPart], colours: dict[int, Colour]) -> list[Finding]:
    out: list[Finding] = []
    unresolved = sum(1 for p in pieces if p.colour == INHERIT_COLOUR)
    if unresolved:
        out.append(Finding(
            "error", "colour",
            f"{unresolved} piece(s) have no real colour (colour 16 was never resolved)",
            "Give each of these pieces, or the submodel reference that places "
            "them, a real LDraw colour code.",
            tuple(p for p in pieces if p.colour == INHERIT_COLOUR),
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
    seen: dict[tuple, list[PlacedPart]] = defaultdict(list)
    for p in pieces:
        key = (p.part, tuple(round(v, 2) for v in p.pos), tuple(round(v, 3) for v in p.matrix))
        seen[key].append(p)
    out = []
    for (part, pos, _), group in seen.items():
        if len(group) > 1:
            out.append(Finding(
                "error", "duplicate",
                f"{len(group)} copies of {part} occupy the same spot at {pos}",
                "Keep one copy and remove the rest.",
                tuple(group),
            ))
    return out


def check_parts(pieces: list[PlacedPart], catalog: PartCatalog) -> list[Finding]:
    out: list[Finding] = []
    for part, qty in sorted(Counter(p.part for p in pieces).items()):
        desc = catalog.description(part)
        used = tuple(p for p in pieces if p.part == part)
        if desc is None:
            out.append(Finding(
                "error", "part", f"{part} ({qty}x) is not in the LDraw part library",
                "Replace it with a part that exists in the LDraw library.", used,
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
                "Replace it with a complete real part, or remove it.", used,
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

    box_of = dict(known)
    ordered = sorted(groups.values(), key=len, reverse=True)
    main = [box_of[i] for i in ordered[0]]
    out = []
    for group in ordered[1:]:
        sample = pieces[group[0]]
        where = " > ".join(sample.path[1:]) or "main model"
        gap = nearest_gap([box_of[i] for i in group], main)
        gap_text = ", ".join(
            f"{round(g, 1)} along {axis}" for g, axis in zip(gap, "XYZ") if g > 0.05
        )
        out.append(Finding(
            "warning", "floating",
            f"{len(group)} piece(s) touch nothing in the main structure "
            f"(e.g. {sample.part} in {where} at "
            f"{tuple(round(v, 1) for v in sample.pos)}). "
            f"Nearest gap to the main structure, in LDraw units: {gap_text}.",
            "Move these pieces so they sit on or clip to the main structure, add "
            "the pieces that should connect them, or remove them. This check "
            "only tests whether bounding boxes touch, so if they are attached "
            "by a connection that leaves a gap, say so and leave them.",
            tuple(pieces[i] for i in group),
        ))
    return out


def connectivity_report(pieces: list[PlacedPart], connectors, catalog: PartCatalog | None):
    """Run the connectivity analysis once; feed the result to check_connectivity
    and to the report's summary line."""
    from . import connectivity

    boxes = [world_box(p, catalog) if catalog else None for p in pieces]
    return connectivity.analyse(pieces, connectors, boxes if catalog else None)


KIND_NAMES = {"cyl": "stud, pin or axle", "clip": "bar in clip", "fgr": "hinge", "gen": "generic"}


def connectivity_summary(report) -> str:
    """One sentence saying what the check found, so a run can be told apart
    from one that found nothing."""
    kinds = ", ".join(f"{report.by_kind.get(k, 0)} {name}" for k, name in KIND_NAMES.items()
                      if report.by_kind.get(k, 0) or k == "cyl")
    text = f"{report.connections} connection(s) found ({kinds})"
    if report.press_fit_only:
        text += f"; {len(report.press_fit_only)} piece(s) are held only by a press fit (tyres on rims and the like)"
    return text


def check_connectivity(
    pieces: list[PlacedPart], connectors, catalog: PartCatalog | None, report=None
) -> list[Finding]:
    """Pieces that no stud, pin, axle, bar, clip or hinge attaches to the rest
    of the model.

    `connectors` is a connectivity.Connectors built from the LDraw library and
    the shadow library. Needs both; the caller decides whether to run it.
    """
    if report is None:
        report = connectivity_report(pieces, connectors, catalog)
    out: list[Finding] = []
    if len(report.groups) <= 1:
        pass
    else:
        unverified = set(report.unverified)
        unknown = set(report.unknown)
        for group in report.groups[1:]:
            members = [i for i in group if i not in unknown]
            if not members:
                continue
            firm = [i for i in members if i not in unverified]
            sample = pieces[members[0]]
            where = " > ".join(sample.path[1:]) or "main model"
            if firm:
                out.append(Finding(
                    "error", "connectivity",
                    f"{len(members)} piece(s) are not clipped to the model: no stud, pin, "
                    f"axle, bar, clip or hinge joins them to it (e.g. {sample.part} in {where} at "
                    f"{tuple(round(v, 1) for v in sample.pos)})",
                    "Move these pieces so their studs or sockets line up with the pieces "
                    "they should attach to, or add the pieces that would join them.",
                    tuple(pieces[i] for i in members),
                ))
            else:
                out.append(Finding(
                    "warning", "connectivity",
                    f"{len(members)} piece(s) carry only a connector type this check does "
                    f"not understand, so they could not be verified (e.g. {sample.part} in {where})",
                    "", tuple(pieces[i] for i in members),
                ))
    if report.unknown:
        parts = sorted({pieces[i].part for i in report.unknown})
        out.append(Finding(
            "warning", "connectivity",
            f"{len(report.unknown)} piece(s) have no connector data, so their attachment "
            f"was not checked: {', '.join(parts[:8])}{'…' if len(parts) > 8 else ''}",
        ))
    return out


def nearest_gap(group, main) -> tuple[float, float, float]:
    """Per-axis gap between the closest pair of boxes from the two sets."""
    best = None
    best_d = float("inf")
    for lo_a, hi_a in group:
        for lo_b, hi_b in main:
            gap = tuple(max(0.0, lo_b[k] - hi_a[k], lo_a[k] - hi_b[k]) for k in range(3))
            d = sum(g * g for g in gap)
            if d < best_d:
                best, best_d = gap, d
    return best or (0.0, 0.0, 0.0)
