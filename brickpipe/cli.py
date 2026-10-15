"""Command line entry point: brickpipe MODEL -o OUTDIR"""
from __future__ import annotations

import argparse
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import availability, bom, checks, connectivity, feedback, ldraw, nova, rebrickable, steps
from .catalog import PartCatalog, load_colours


def build_report(
    name, pieces, rows, findings, stepped, skipped, catalog_used,
    availability_note=None, has_feedback=False, connectivity_note=None,
) -> str:
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
    if availability_note:
        lines += [availability_note, ""]
    if connectivity_note:
        lines += [connectivity_note, ""]
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
        "- `bricklink_wanted.xml`: paste its contents into the XML box at "
        "BrickLink > Want > Upload",
        "- `flat.ldr`: every piece in one model; upload to Rebrickable or BrickLink "
        "to let them match parts and colours themselves",
        "- `instructions.mpd`: the model with build steps; open in LPub3D",
    ]
    if availability_note and "availability.csv" in availability_note:
        lines.append("- `availability.csv`: every part and colour with its status")
    if has_feedback:
        lines.append(
            "- `feedback.md`: repair request to give to the agent that designed the model"
        )
    lines.append("")
    return "\n".join(lines)


class PipelineError(Exception):
    pass


@dataclass
class Result:
    """What one run produced; the files are in `out`."""

    name: str
    out: Path
    piece_count: int
    lot_count: int
    findings: list
    feedback: str | None  # the repair request text, or None when clean

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.level == "error")

    @property
    def warnings(self) -> int:
        return len(self.findings) - self.errors

    def summary(self) -> str:
        return (f"{self.name}: {self.piece_count} pieces, {self.lot_count} lots, "
                f"{self.errors} error(s), {self.warnings} warning(s)")


def run_pipeline(
    model: Path,
    out: Path | None = None,
    *,
    nova_db: Path | None = None,
    max_step_parts: int = 8,
    keep_steps: bool = False,
    check_availability: bool = False,
    cache_dir: Path | None = None,
    client: rebrickable.Client | None = None,
    ldraw_dir: Path | None = None,
    shadow_dir: Path | None = None,
) -> Result:
    """Run every check on `model` and write the outputs. Raises PipelineError."""
    model = Path(model)
    try:
        doc = ldraw.load(model)
        catalog = PartCatalog(nova_db) if nova_db else None
        connectors = None
        shadow_path = shadow_dir or connectivity.find_shadow_library(nova_db)
        ldraw_path = ldraw_dir or (connectivity.find_ldraw_library() if shadow_path else None)
        if shadow_path and ldraw_path:
            connectors = connectivity.Connectors(
                connectivity.PartLibrary(ldraw_path), connectivity.ShadowLibrary(shadow_path)
            )
    except (OSError, ValueError, zipfile.BadZipFile) as err:
        raise PipelineError(str(err)) from None
    colours = load_colours()

    pieces, problems = ldraw.flatten(doc)
    if not pieces:
        raise PipelineError("No pieces found in the model.")
    rows = bom.build(pieces)

    findings = [
        checks.Finding(
            "error", "structure", p,
            "Define the missing submodel in the file, or remove the reference to it.",
        )
        for p in problems
    ]
    findings += checks.check_colours(pieces, colours)
    findings += checks.check_duplicates(pieces)
    findings += checks.check_embedded(pieces)
    if catalog:
        findings += checks.check_parts(pieces, catalog)
        findings += checks.check_floating(pieces, catalog)
    if connectors:
        findings += checks.check_connectivity(pieces, connectors, catalog)
        connectors.parts.close()

    stepped = (
        [] if keep_steps
        else steps.add_steps_everywhere(doc, catalog, max_step_parts)
    )
    findings += checks.check_steps(doc, max_step_parts)

    connectivity_note = None
    if not connectors:
        missing = []
        if not shadow_path:
            missing.append("LDraw Nova's shadow library (next to its database)")
        if not ldraw_path:
            missing.append("the LDraw part library (--ldraw-dir)")
        connectivity_note = (
            "The stud connectivity check was skipped because it could not find "
            + " and ".join(missing) + "."
        )

    name = model.stem
    out = out or model.with_name(f"{name}-brickpipe")
    out.mkdir(parents=True, exist_ok=True)

    overrides = None
    availability_note = None
    if check_availability:
        try:
            client = client or rebrickable.Client(
                cache_dir=cache_dir or rebrickable.default_cache_dir()
            )
            results = availability.check(rows, client, catalog)
        except rebrickable.RebrickableError as err:
            findings.append(checks.Finding(
                "warning", "availability", f"Availability check did not run: {err}"
            ))
            availability_note = "Availability was not checked; see the warning below."
        else:
            findings += availability.findings(results, pieces)
            overrides = availability.bricklink_overrides(results)
            (out / "availability.csv").write_text(
                availability.to_csv(results), encoding="utf-8"
            )
            availability_note = (
                "Availability was checked against Rebrickable; details are in "
                "`availability.csv`. The wanted list uses BrickLink's own part "
                "and colour ids where Rebrickable supplied them."
            )

    xml, skipped = bom.to_bricklink_xml(rows, colours, overrides)
    (out / "parts.csv").write_text(bom.to_csv(rows, colours, catalog), encoding="utf-8")
    (out / "bricklink_wanted.xml").write_text(xml, encoding="utf-8")
    (out / "flat.ldr").write_text(bom.to_flat_ldr(pieces, name), encoding="utf-8")
    (out / "instructions.mpd").write_text(ldraw.dump(doc), encoding="utf-8")
    request = feedback.build(name, len(pieces), findings, colours)
    feedback_path = out / "feedback.md"
    if request:
        feedback_path.write_text(request, encoding="utf-8")
    elif feedback_path.exists():
        feedback_path.unlink()  # an earlier run's request no longer applies
    report = build_report(
        name, pieces, rows, findings, stepped, skipped, bool(catalog),
        availability_note, bool(request), connectivity_note,
    )
    (out / "report.md").write_text(report, encoding="utf-8")

    if catalog:
        catalog.close()

    return Result(name, out, len(pieces), len(rows), findings, request)


def run_loop(args, client: rebrickable.Client | None) -> int:
    from . import loop

    out = args.out or args.model.with_name(f"{args.model.stem}-repair")
    try:
        outcome = loop.run(
            nova.Client(args.nova_url), args.nova_chat, args.model, out,
            max_rounds=args.rounds, approve_tools=not args.ask_tools,
            nova_db=args.nova_db, max_step_parts=args.max_step_parts,
            keep_steps=args.keep_steps, check_availability=args.availability,
            cache_dir=args.cache_dir, client=client, ldraw_dir=args.ldraw_dir,
        )
    except (nova.NovaError, PipelineError) as err:
        print(f"brickpipe: {err}", file=sys.stderr)
        return 2
    print()
    print(("Clean: " if outcome.clean else "Stopped: ") + outcome.stopped_because)
    if outcome.final_model:
        print(f"Final model: {outcome.final_model}")
    print(f"Wrote {out}/ (see loop.md)")
    return 0 if outcome.clean else 1


def main(argv: list[str] | None = None, client: rebrickable.Client | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brickpipe",
        description="Turn an LDraw model into a parts list, a BrickLink wanted "
        "list and a step-ordered file for LPub3D.",
    )
    parser.add_argument("model", type=Path, nargs="?", help="LDraw .ldr or .mpd file")
    parser.add_argument("-o", "--out", type=Path, default=None, help="output folder")
    parser.add_argument(
        "--nova-db", type=Path, default=None,
        help="path to LDraw Nova's data/ldraw-info.db (adds part names, part "
        "checks, better step ordering and the floating-piece check)",
    )
    parser.add_argument(
        "--ldraw-dir", type=Path, default=None,
        help="the LDraw part library: a folder with parts/ and p/, or LPub3D's "
        "complete.zip (found automatically in the usual places). With --nova-db, "
        "enables the stud connectivity check.",
    )
    parser.add_argument(
        "--get-ldraw", action="store_true",
        help="download the LDraw part library (about 90 MB, from ldraw.org) into "
        "brickpipe's own folder and exit; later runs find it automatically",
    )
    parser.add_argument(
        "--max-step-parts", type=int, default=8, help="most pieces per build step"
    )
    parser.add_argument(
        "--keep-steps", action="store_true",
        help="leave the model's build steps exactly as they are",
    )
    parser.add_argument(
        "--availability", action="store_true",
        help="check each part and colour against Rebrickable (needs the "
        f"{rebrickable.KEY_ENV} environment variable; about one request per "
        "second on the first run, cached afterwards)",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=None,
        help="where Rebrickable responses are cached "
        "(default: ~/.cache/brickpipe/rebrickable)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit with status 1 if any error is found"
    )
    loop_group = parser.add_argument_group(
        "repair loop",
        "send the problems to the LDraw Nova agent that designed the model and "
        "check what it sends back, repeating until the model is clean",
    )
    loop_group.add_argument(
        "--nova-chat", metavar="CHAT_ID",
        help="the Nova chat that designed the model ('latest' for the most recent one)",
    )
    loop_group.add_argument(
        "--nova-url", default=nova.DEFAULT_URL, help=f"Nova web app (default {nova.DEFAULT_URL})"
    )
    loop_group.add_argument(
        "--rounds", type=int, default=3, help="most repair rounds to run (default 3)"
    )
    loop_group.add_argument(
        "--ask-tools", action="store_true",
        help="leave tool approvals to you in the Nova UI instead of granting them",
    )
    args = parser.parse_args(argv)

    if args.get_ldraw:
        def progress(done, total):
            print(f"\r  {done / 1048576:.0f} of {total / 1048576:.0f} MB" if total else f"\r  {done / 1048576:.0f} MB",
                  end="", flush=True)
        try:
            path = connectivity.download_library(progress=progress)
        except OSError as err:
            print(f"\nbrickpipe: download failed: {err}", file=sys.stderr)
            return 2
        print(f"\nSaved {path}")
        return 0
    if args.model is None:
        parser.error("a model file is required (or --get-ldraw)")

    if args.nova_chat:
        return run_loop(args, client)

    try:
        result = run_pipeline(
            args.model, args.out, nova_db=args.nova_db, max_step_parts=args.max_step_parts,
            keep_steps=args.keep_steps, check_availability=args.availability,
            cache_dir=args.cache_dir, client=client, ldraw_dir=args.ldraw_dir,
        )
    except PipelineError as err:
        print(f"brickpipe: {err}", file=sys.stderr)
        return 2
    print(result.summary())
    print(f"Wrote {result.out}/")
    if result.feedback:
        print("feedback.md lists what the designing agent should repair.")
    return 1 if (args.strict and result.errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
