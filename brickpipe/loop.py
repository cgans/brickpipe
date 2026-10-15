"""The repair loop: check a model, send the problems to the Nova agent that
designed it, fetch the repaired model, and repeat until it is clean.

    model.mpd ──check──> feedback.md ──send──> Nova agent ──> model v2 ──check──> ...

Each round's outputs go in their own folder (round-1/, round-2/, ...) under
the output folder, and loop.md there summarises what happened.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import nova
from .cli import PipelineError, Result, run_pipeline

MESSAGE = (
    "The attached feedback.md is a repair request from an automated check of "
    "the model you delivered. Please make the repairs it lists and deliver the "
    "regenerated model."
)


@dataclass
class Round:
    number: int
    model: Path
    result: Result | None = None
    sent: bool = False
    turn: nova.Turn | None = None
    error: str | None = None


@dataclass
class LoopResult:
    rounds: list[Round] = field(default_factory=list)
    final_model: Path | None = None
    clean: bool = False
    stopped_because: str = ""


def run(
    agent: nova.Client,
    chat_id: str,
    model: Path,
    out: Path,
    *,
    max_rounds: int = 3,
    approve_tools: bool = True,
    pipeline: Callable[..., Result] = run_pipeline,
    log: Callable[[str], None] = print,
    **pipeline_options,
) -> LoopResult:
    chat = agent.find_chat(chat_id)
    chat_id = chat["id"]
    log(f"Using Nova chat {chat_id} ({chat.get('title') or 'untitled'})")
    if agent.is_running(chat_id):
        raise nova.NovaError("That chat's agent is still busy. Wait for it to finish.")

    outcome = LoopResult()
    current = Path(model)
    for number in range(1, max_rounds + 2):  # one extra round only checks
        rnd = Round(number, current)
        outcome.rounds.append(rnd)
        round_out = out / f"round-{number}"
        try:
            rnd.result = pipeline(current, round_out, **pipeline_options)
        except PipelineError as err:
            rnd.error = str(err)
            outcome.stopped_because = f"round {number}: {err}"
            break
        log(f"Round {number}: {rnd.result.summary()}")

        if not rnd.result.feedback:
            outcome.clean = True
            outcome.final_model = current
            outcome.stopped_because = "nothing left for the agent to fix"
            break
        if number > max_rounds:
            outcome.final_model = current
            outcome.stopped_because = f"reached the limit of {max_rounds} repair round(s)"
            break

        known = {m.id for m in agent.models(chat_id)}
        sent_at = time.time()
        agent.send(
            chat_id, MESSAGE, documents=[round_out / "feedback.md"],
            options={"mode": "agent", "permissions": "full" if approve_tools else "ask"},
        )
        rnd.sent = True
        log(f"Round {number}: sent feedback.md to the agent, waiting...")
        rnd.turn = agent.wait_for_turn(chat_id, approve_tools=approve_tools)
        if rnd.turn.error:
            outcome.final_model = current
            outcome.stopped_because = f"the agent's turn failed: {rnd.turn.error}"
            break

        fresh = [m for m in agent.models(chat_id)
                 if m.id not in known and m.created_at >= sent_at - 5]
        if not fresh:
            outcome.final_model = current
            outcome.stopped_because = (
                "the agent finished without delivering a new model; read its "
                "reply in the Nova chat"
            )
            break
        newest = fresh[-1]
        current = agent.download(newest, out / "models")
        log(f"Round {number}: agent delivered {newest.file} "
            f"({newest.parts if newest.parts is not None else '?'} parts) "
            f"in {int(rnd.turn.seconds)} s")

    (out / "loop.md").write_text(summary(outcome), encoding="utf-8")
    return outcome


def summary(outcome: LoopResult) -> str:
    lines = ["# Repair loop", ""]
    lines.append("Result: " + ("clean model" if outcome.clean else "stopped") +
                 f" ({outcome.stopped_because})")
    if outcome.final_model:
        lines.append(f"Final model: `{outcome.final_model}`")
    lines += ["", "| Round | Model | Pieces | Errors | Warnings | Sent to agent |",
              "|---|---|---|---|---|---|"]
    for r in outcome.rounds:
        if r.result:
            lines.append(
                f"| {r.number} | {r.model.name} | {r.result.piece_count} | "
                f"{r.result.errors} | {r.result.warnings} | {'yes' if r.sent else 'no'} |"
            )
        else:
            lines.append(f"| {r.number} | {r.model.name} | - | - | - | failed: {r.error} |")
    lines += ["", "Each round's full outputs are in `round-N/`; downloaded models are in `models/`.", ""]
    return "\n".join(lines)
