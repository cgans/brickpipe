"""Add or tighten build steps.

LPub3D turns "0 STEP" markers into instruction pages. Nova writes steps for
some submodels and not others, and the ones it writes can hold hundreds of
pieces. Here every stretch of pieces between two STEP markers that is longer
than the limit is reordered bottom-up and cut into smaller steps. Stretches
already within the limit are left exactly as they are.

This is a heuristic. It orders by height only; it does not know whether a
hand can reach the spot or whether a piece is hidden by an earlier one.
"""
from __future__ import annotations

from .catalog import PartCatalog
from .ldraw import Document, Line, Model, Ref, is_part_file, mat_vec

PLATE_HEIGHT = 8.0  # LDraw units; a brick is 24


def bottom_y(ref: Ref, catalog: PartCatalog | None) -> float:
    """Lowest point of a placed piece. Y points down, so lowest = largest Y."""
    box = catalog.bbox(ref.key[:-4]) if catalog and is_part_file(ref.key) else None
    if box is None:
        return ref.pos[1]
    corners = [
        (x, y, z) for x in (box.min[0], box.max[0])
        for y in (box.min[1], box.max[1])
        for z in (box.min[2], box.max[2])
    ]
    return ref.pos[1] + max(mat_vec(ref.matrix, c)[1] for c in corners)


def split_segment(
    lines: list[Line], catalog: PartCatalog | None, max_parts: int
) -> list[Line] | None:
    """Reorder one STEP-free stretch bottom-up and cut it into steps.

    Returns None when the stretch is already small enough.
    """
    if sum(1 for ln in lines if ln.ref) <= max_parts:
        return None

    # Each piece travels with the comment lines written just above it.
    blocks: list[tuple[list[Line], Ref]] = []
    pending: list[Line] = []
    for line in lines:
        if line.ref:
            blocks.append((pending + [line], line.ref))
            pending = []
        else:
            pending.append(line)

    def layer(ref: Ref) -> int:
        return round(bottom_y(ref, catalog) / PLATE_HEIGHT)

    order = sorted(range(len(blocks)), key=lambda i: (-layer(blocks[i][1]), i))

    out: list[Line] = []
    in_step = 0
    current_layer: int | None = None
    for i in order:
        block, ref = blocks[i]
        this_layer = layer(ref)
        if in_step and (this_layer != current_layer or in_step >= max_parts):
            out.append(Line("0 STEP"))
            in_step = 0
        out.extend(block)
        in_step += 1
        current_layer = this_layer
    return out + pending


def add_steps(
    model: Model, catalog: PartCatalog | None = None, max_parts: int = 8
) -> bool:
    """Cut oversized steps in one model. Returns True if anything changed."""
    # Header lines (title, Name:, Author:, !LDRAW_ORG ...) stay at the top.
    start = 0
    for start, line in enumerate(model.lines):
        if line.ref or line.is_step or line.raw.startswith("0 //"):
            break
    else:
        return False

    changed = False
    out = model.lines[:start]
    segment: list[Line] = []
    for line in model.lines[start:] + [Line("0 STEP")]:  # sentinel closes the last one
        if not line.is_step:
            segment.append(line)
            continue
        replacement = split_segment(segment, catalog, max_parts)
        changed = changed or replacement is not None
        out.extend(replacement if replacement is not None else segment)
        out.append(line)
        segment = []
    out.pop()  # the sentinel

    if changed:
        model.lines = out
    return changed


def add_steps_everywhere(
    doc: Document, catalog: PartCatalog | None = None, max_parts: int = 8
) -> list[str]:
    """Tighten steps in every submodel. Returns the names changed."""
    return [
        m.name
        for key, m in doc.models.items()
        if not is_part_file(key) and add_steps(m, catalog, max_parts)
    ]
