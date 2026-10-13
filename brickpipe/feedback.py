"""Write a repair request for the agent that designed the model.

The report is written for a person. This is the same information written for
the designing agent: only the problems it can fix, each with what to change
and exactly where, in the terms its own files use (submodel names, the
comment above each line, and positions inside that submodel).

Give the resulting feedback.md to the agent in the conversation that produced
the model, then run brickpipe again on what comes back.
"""
from __future__ import annotations

from collections import defaultdict

from .catalog import Colour
from .checks import Finding
from .ldraw import PlacedPart, fmt

MAX_ITEMS = 40  # problems listed per section
MAX_SUBMODELS = 5  # submodels listed per problem
MAX_LINES = 8  # source lines listed per submodel


def locations(pieces: tuple[PlacedPart, ...], colours: dict[int, Colour]) -> list[str]:
    """Where the pieces are, grouped by the submodel whose file places them."""
    by_model: dict[str, dict[tuple, int]] = defaultdict(dict)
    for p in pieces:
        line = (p.part, p.colour, tuple(round(v, 2) for v in p.local), p.label)
        lines = by_model[p.path[-1]]
        lines[line] = lines.get(line, 0) + 1

    out: list[str] = []
    ranked = sorted(by_model.items(), key=lambda kv: -sum(kv[1].values()))
    for model, lines in ranked[:MAX_SUBMODELS]:
        out.append(f"- In `{model}`:")
        for (part, colour, local, label), count in list(lines.items())[:MAX_LINES]:
            name = colours[colour].name if colour in colours else f"colour {colour}"
            pos = ", ".join(fmt(v) for v in local)
            text = f"  - `{part}` in {name} at ({pos})"
            if label:
                text += f', under the comment "{label}"'
            if count > 1:
                text += f" (this submodel is placed {count} times)"
            out.append(text)
        if len(lines) > MAX_LINES:
            out.append(f"  - and {len(lines) - MAX_LINES} more line(s) in this submodel")
    if len(ranked) > MAX_SUBMODELS:
        rest = sum(sum(lines.values()) for _, lines in ranked[MAX_SUBMODELS:])
        out.append(
            f"- and {rest} more piece(s) in {len(ranked) - MAX_SUBMODELS} other submodel(s)"
        )
    return out


def section(title: str, items: list[Finding], start: int, colours) -> list[str]:
    if not items:
        return []
    out = [f"## {title}", ""]
    for number, finding in enumerate(items[:MAX_ITEMS], start):
        out.append(f"### {number}. {finding.message}")
        out.append("")
        out.append(f"What to do: {finding.fix}")
        if finding.pieces:
            out.append("")
            out.append("Where:")
            out.extend(locations(finding.pieces, colours))
        out.append("")
    if len(items) > MAX_ITEMS:
        out += [f"There are {len(items) - MAX_ITEMS} more of the same kind; fix "
                "these first and the next check will list the rest.", ""]
    return out


def summary(errors: list[Finding], warnings: list[Finding]) -> str:
    parts = []
    if errors:
        parts.append(f"{len(errors)} problem(s) make that impossible as drawn")
    if warnings:
        parts.append(f"{len(warnings)} problem(s) are likely to get in the way")
    return " and ".join(parts) + ". Please repair them."


def build(
    name: str,
    piece_count: int,
    findings: list[Finding],
    colours: dict[int, Colour],
) -> str | None:
    """Return the repair request, or None when there is nothing to fix."""
    fixable = [f for f in findings if f.fix]
    if not fixable:
        return None
    errors = [f for f in fixable if f.level == "error"]
    warnings = [f for f in fixable if f.level == "warning"]

    out = [
        f"# Repair request: {name}",
        "",
        f"The model `{name}` ({piece_count} pieces) was checked to see whether it "
        "can be bought and built with real bricks. " + summary(errors, warnings),
        "",
        "## How to work",
        "",
        "- Change the plan and the generator that produce the model, then "
        "regenerate the `.mpd`. Do not edit the `.mpd` by hand.",
        "- Keep the design as it is: same subject, size, silhouette and palette. "
        "Change only what a fix below requires.",
        "- Positions are in LDraw units, inside the submodel named. Y points "
        "down, so a smaller Y is higher up.",
        "- Build steps are handled after you deliver. Do not spend effort on "
        "STEP lines.",
        "",
    ]
    out += section("Must fix", errors, 1, colours)
    out += section("Should fix", warnings, len(errors[:MAX_ITEMS]) + 1, colours)
    out += [
        "## When you are done",
        "",
        "- Go through the numbered items and say what you changed for each, or "
        "why you left it.",
        "- Give the new total piece count.",
        "- Deliver the regenerated `.mpd`.",
        "",
    ]
    return "\n".join(out)
