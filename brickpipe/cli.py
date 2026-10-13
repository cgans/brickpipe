"""Command line entry point: brickpipe MODEL -o OUTDIR"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import bom, checks, ldraw, steps
from .catalog import PartCatalog, load_colours


def build_report(name, pieces, rows, findings, stepped, skipped, catalog_used) -> str:
    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]
    lines = [
        f"# {name}",
        "",
        f"- Pieces: {len(pieces)}",
        f"- Different part and colour combinations: {len(rows)}",
        f"- Problems: {len(errors)} error(s), {len(warnings)} warning(s)",
        f"- Submodels whose build steps were added or tightened: {len(stepped)}",
        "",
    ]
    if not catalog_used:
        lines += [
            "Part and floating-piece checks were skipped because no Nova database "
            "was given (--nova-db).",
            "",
        ]
    for title, group in (("Errors", errors), ("Warnings", warnings)):
        if group:
            lines.append(f"## {title}")
            lines.extend(f"- [{f.check}] {f.message}" for f in group)
            lines.append("")
    if skipped:
        lines.append("## Left out of the BrickLink wanted list")
        lines.extend(
            f"- {r.quantity}x {r.part} in LDraw colour {r.colour}" for r in skipped
        )
        lines.append("")
    lines += [
        "## Files",
        "- `parts.csv`: full parts list",
        "- `bricklink_wanted.xml`: upload at BrickLink > Want > Upload",
        "- `flat.ldr`: every piece in one model; upload to Rebrickable or BrickLink "
        "to let them match parts and colours themselves",
        "- `instructions.mpd`: the model with build steps; open in LPub3D",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brickpipe",
        description="Turn an LDraw model into a parts list, a BrickLink wanted "
        "list and a step-ordered file for LPub3D.",
    )
    parser.add_argument("model", type=Path, help="LDraw .ldr or .mpd file")
    parser.add_argument("-o", "--out", type=Path, default=None, help="output folder")
    parser.add_argument(
        "--nova-db", type=Path, default=None,
        help="path to LDraw Nova's data/ldraw-info.db (adds part names, part "
        "checks, better step ordering and the floating-piece check)",
    )
    parser.add_argument(
        "--max-step-parts", type=int, default=8, help="most pieces per build step"
    )
    parser.add_argument(
        "--keep-steps", action="store_true",
        help="leave the model's build steps exactly as they are",
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit with status 1 if any error is found"
    )
    args = parser.parse_args(argv)

    doc = ldraw.load(args.model)
    colours = load_colours()
    catalog = PartCatalog(args.nova_db) if args.nova_db else None

    pieces, problems = ldraw.flatten(doc)
    if not pieces:
        print("No pieces found in the model.", file=sys.stderr)
        return 1
    rows = bom.build(pieces)

    findings = [checks.Finding("error", "structure", p) for p in problems]
    findings += checks.check_colours(pieces, colours)
    findings += checks.check_duplicates(pieces)
    if catalog:
        findings += checks.check_parts(pieces, catalog)
        findings += checks.check_floating(pieces, catalog)

    stepped = (
        [] if args.keep_steps
        else steps.add_steps_everywhere(doc, catalog, args.max_step_parts)
    )
    findings += checks.check_steps(doc, args.max_step_parts)

    name = args.model.stem
    out = args.out or args.model.with_name(f"{name}-brickpipe")
    out.mkdir(parents=True, exist_ok=True)
    xml, skipped = bom.to_bricklink_xml(rows, colours)
    (out / "parts.csv").write_text(bom.to_csv(rows, colours, catalog), encoding="utf-8")
    (out / "bricklink_wanted.xml").write_text(xml, encoding="utf-8")
    (out / "flat.ldr").write_text(bom.to_flat_ldr(pieces, name), encoding="utf-8")
    (out / "instructions.mpd").write_text(ldraw.dump(doc), encoding="utf-8")
    report = build_report(name, pieces, rows, findings, stepped, skipped, bool(catalog))
    (out / "report.md").write_text(report, encoding="utf-8")

    errors = sum(1 for f in findings if f.level == "error")
    warnings = len(findings) - errors
    print(f"{name}: {len(pieces)} pieces, {len(rows)} lots, "
          f"{errors} error(s), {warnings} warning(s)")
    print(f"Wrote {out}/")
    return 1 if (args.strict and errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
