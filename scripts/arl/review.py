"""The review pipeline: ``prepare`` a review, ``record`` its answer, and the external reviewer.

``prepare`` snapshots every root, builds the bundle and the prompt, and makes a **ticket** the
run's pending review: review id, run id, stage, phase, phase-list revision and the snapshot tree
of every root. The mod (or :func:`run_external`) runs the reviewer and hands its answer to
``record``, which takes it only while that ticket is still the pending one and every root still
has the tree it was reviewed at. A ticket is retired by ``stop``, ``accept``, a replan, ``init
--new``, a newer ``prepare`` and its own recording, so a late answer can never land on a state it
was not prepared for.

**No failure is an approval** (AGENTS.md rule 1). An answer that breaks the output contract gets
one repair call and is otherwise ``OP_FAILURE``; a repair may never approve; failures count
against ``max_failures`` (``max_transient_failures`` for timeouts and rate limits) and, past the
budget, the run waits for the user.
"""

#  This file is part of adversarial-review-loop.
#
#  Copyright (c) 2026 Roberto Leinardi
#
#  adversarial-review-loop is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  adversarial-review-loop is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from arl import bundle, config, contract, globmatch, guidance, harness, oscillation, report, runner, workspace
from arl.atomic import write_private_file
from arl.bundle import BundleError, BundleTooLarge
from arl.contract import Review, Target
from arl.errors import OcrlError
from arl.state import RunError, RunPaths, RunState, locked_state
from arl.util import decode, encode, log

__all__ = [
    "REPAIR_TAIL_BYTES",
    "abandon",
    "clarify_prepare",
    "clarify_record",
    "clarify_run_external",
    "prepare",
    "record",
    "reported_usage",
    "run_external",
]

#: How much of a malformed transcript the repair call is shown (its tail, where the block is).
REPAIR_TAIL_BYTES: Final = 65536

#: The detail of the one finding ``prompts/reviewer-repair.md`` prescribes when the transcript
#: tail states no findings: a repair carrying only it recovered nothing.
_INCOMPLETE: Final = "| review transcript incomplete"

#: Failure kinds the mod may report for a subagent that did not answer.
FAILURE_KINDS: Final = ("transient", "operational")


def _require(run: RunState | None) -> RunState:
    if run is None:
        raise RunError("no-run", "there is no run here; start one with /arl-implement")
    return run


def _review_dir(paths: RunPaths, run_id: str, seq: int) -> Path:
    """Named for the run as well as the sequence: an external reviewer of an archived run may
    still be writing by name, and must not land in the next run's review of the same number."""
    return paths.run_dir / "reviews" / f"{run_id}-{seq:03d}"


def _load_config(paths: RunPaths) -> config.Config:
    return config.load(str(paths.launch_dir))


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------


def _subagent_settings(cfg: config.Config) -> dict[str, Any]:
    """What the mod spawns the reviewer subagent with: the configured model, empty for the
    session's own subagent default, and how long it waits for the answer."""
    return {"reviewer_model": cfg.as_str("model"), "timeout_sec": cfg.as_int("timeout_sec")}


def _validated_verification(raw: object) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        raise RunError("bad-verification", "verification must be a list of objects (root, command, cwd, status, output)")
    return [dict(entry) for entry in raw]


def _snapshot_or_wait(run: RunState) -> dict[str, str]:
    """Every root's tree; a file past ``max_file_bytes`` makes the run wait for the user."""
    try:
        return workspace.snapshot_all(run)
    except workspace.Oversized as exc:
        run.wait_for_user("oversized", str(exc))
        run.save()
        raise RunError("oversized", f"{exc}\nRemove the file(s), or ignore them in the root's .gitignore, then review again.") from exc


def _only_ignored(run: RunState, trees: Mapping[str, str], cfg: config.Config) -> bool:
    """Did the stage change something, and does every changed path match ``ignore_globs``?

    False whenever that is not certain -- no globs, no change, a root whose changes cannot be
    listed -- because True approves without a review. A glob is matched against the path inside
    its root, as a single-repository project would write it. A change to the configuration file
    itself is never ignored: the edit that sets ``ignore_globs`` would otherwise approve itself.
    """
    globs = cfg.as_list("ignore_globs")
    if not globs:
        return False
    target = bundle.target_for(run, trees, final=run.stage() == "final")
    changed: list[str] = []
    for root in target.roots:
        if root.base == root.head:
            continue
        paths = bundle.changed_paths(run, root, root.base)
        if paths is None:
            return False
        changed.extend(path.removeprefix(f"{root.alias}/") for path in paths)
    if any(path.rsplit("/", 1)[-1] == config.REPO_CONFIG_NAME for path in changed):
        return False
    return bool(changed) and all(any(globmatch.matches(path, glob) for glob in globs) for path in changed)


def _skip(run: RunState, trees: Mapping[str, str], cfg: config.Config) -> dict[str, Any]:
    what = "the final review" if run.stage() == "final" else f"phase {run.current_phase()}"
    run.approve(trees, by="ignored")
    run.save()
    message = f"Every change of {what} matches ignore_globs ({', '.join(cfg.as_list('ignore_globs'))}), so it was approved without a review.\n"
    return {"ok": True, "status": "skipped", "verdict": "APPROVED", "message": message, "run": run.summary()}


def prepare(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Prepare the review of the current stage and make it the pending ticket."""
    cfg = _load_config(paths)
    verification = _validated_verification(args.get("verification"))
    with locked_state(paths) as loaded:
        run = _require(loaded)
        run.require_status("active")
        decision = run.pending_decision()
        if decision is not None:
            # Another review would end it by approving: only the user ends a stall or a call for them.
            raise RunError(
                "waiting", f"the run waits for the user ({decision['kind']}): {decision.get('detail', '')}: /arl-resume or /arl-accept first"
            )
        if cfg.as_str("harness") != harness.SUBAGENT:
            # Before any ticket: a review no reviewer can run would hold the loop on it.
            _external_harness(cfg)
        stage = run.stage()
        if stage not in ("phase", "final"):
            raise RunError("nothing-to-review", "set the phases first" if stage == "plan" else "the run is complete")
        if not run.roots:
            # An empty bundle would show the reviewer nothing, and its approval would cover changes
            # no one saw.
            raise RunError("no-roots", "the run has no workspace root left: call add_root for the folder the phase changed")
        # Work on the next stage has begun in the turn that got the approval: compacting now would
        # summarise away what this review is about to say.
        run.data["compact_due"] = False
        # A repository created since the last look becomes a root before anything is snapshotted;
        # its unknown baseline then refuses below, naming it.
        if workspace.discover(run, late=True):
            run.save()
        trees = _snapshot_or_wait(run)
        # Every root snapshotted, so nothing is oversized any more.
        run.clear_wait("oversized")
        if _only_ignored(run, trees, cfg):
            return _skip(run, trees, cfg)
        seq = run.next_review_seq()
        review_dir = _review_dir(paths, run.run_id, seq)
        target = bundle.target_for(run, trees, final=stage == "final")
        changed = {root.alias for root in target.roots if root.base != root.head}
        sources, warnings = guidance.collect([root for root in run.roots if root["alias"] in changed], cfg, paths.launch_dir)
        try:
            built = bundle.build(run, target, review_dir / "bundle", config=cfg, verification=verification, sources=sources, warnings=warnings)
            bundle.compose_prompt(target, built, review_dir / "prompt.md")
        except BundleTooLarge as exc:
            run.wait_for_user("oversized", str(exc))
            run.save()
            raise RunError("oversized", str(exc)) from exc
        except BundleError as exc:
            outcome = _count_failure(run, "bundle", str(exc), cfg)
            run.save()
            raise RunError("bundle", f"{exc}{outcome}") from exc
        ticket = {
            "review_id": f"{run.run_id}-{seq}",
            "run_id": run.run_id,
            "seq": seq,
            "stage": stage,
            "label": target.label,
            "phase": target.phase,
            "revision": run.data["phases_revision"],
            "trees": trees,
            "session": str(args.get("session") or ""),
            "round": built.round_number,
            "repair": "",
            # What a stored report names as the guidance this round ran under.
            "guidance": guidance.disclosure(sources),
        }
        run.issue_ticket(ticket)
        run.save()
        return {
            "ok": True,
            "review_id": ticket["review_id"],
            "label": target.label,
            "round": built.round_number,
            "harness": cfg.as_str("harness"),
            "model": harness.display_model(cfg),
            **_subagent_settings(cfg),
            "prompt_file": str(review_dir / "prompt.md"),
            "bundle_dir": str(built.dir),
            # Every directory the reviewer reads besides the bundle: a root need not sit under
            # the launch directory, and a reviewer that cannot read one has not reviewed it.
            "roots": [root.path for root in target.roots],
            "answer_file": str(review_dir / "answer.md"),
        }


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------


def _pending(run: RunState, review_id: str) -> dict[str, Any]:
    ticket = run.data["pending_ticket"]
    if ticket is None or ticket.get("review_id") != review_id:
        last = run.data["last_ticket"] or {}
        why = f" (it was {last.get('status')})" if last.get("review_id") == review_id else ""
        raise RunError("stale-review", f"review {review_id} is not the pending review{why}; its answer changes nothing")
    trees = ticket.get("trees")
    covered = isinstance(trees, dict) and set(trees) == {root["alias"] for root in run.roots}
    # A root registered since the review was prepared is a change it never saw.
    if (
        not covered
        or ticket.get("run_id") != run.run_id
        or ticket.get("revision") != run.data["phases_revision"]
        or ticket.get("label") != run.label()
    ):
        run.invalidate_ticket("stale")
        raise RunError("stale-review", f"review {review_id} was prepared for a state that has moved on; its answer changes nothing")
    return dict(ticket)


def _target(run: RunState, ticket: Mapping[str, Any]) -> Target:
    return bundle.target_for(run, ticket["trees"], final=ticket["stage"] == "final")


def _count_failure(run: RunState, kind: str, error: str, cfg: config.Config) -> str:
    """Spend one failure of ``kind`` and wait for the user past the budget; answers a note."""
    key, budget = ("transient_failures", "max_transient_failures") if kind == "transient" else ("failures", "max_failures")
    run.data[key] += 1
    if run.data[key] > cfg.as_int(budget):
        run.wait_for_user("failures", f"{run.data[key]} {kind} failures in a row; the last: {error}")
        return f" The run now waits for the user ({budget} exceeded)."
    return ""


def _repair(run: RunState, ticket: dict[str, Any], answer: Path, review_dir: Path) -> dict[str, Any]:
    """Arrange the one repair call a malformed answer gets, and answer what the caller runs."""
    try:
        raw = answer.read_bytes()
    except OSError:
        raw = b""
    write_private_file(review_dir / "bundle" / "repair.txt", raw[-REPAIR_TAIL_BYTES:] or b"(the reviewer wrote nothing)\n")
    prompt = review_dir / "repair-prompt.md"
    bundle.compose_fixed("reviewer-repair", review_dir / "bundle", ["range.txt", "repair.txt"], prompt)
    ticket["repair"] = str(answer)
    run.data["pending_ticket"] = ticket
    run.save()
    return {
        "ok": True,
        "status": "repair",
        "review_id": ticket["review_id"],
        "prompt_file": str(prompt),
        "bundle_dir": str(review_dir / "bundle"),
        "answer_file": str(review_dir / "repair-answer.md"),
    }


def _parsed(run: RunState, ticket: Mapping[str, Any], target: Target, answer: Path, cfg: config.Config) -> Review:
    repairing = bool(ticket.get("repair"))
    scope = bundle.late_scope(run, target)
    review = contract.parse(answer, config=cfg, allow_supersedes=target.is_phase and not repairing, scope=scope)
    review.raw = str(answer)
    retired = frozenset(alias for alias in run.data.get("retired_aliases", []) if isinstance(alias, str))
    stray = contract.unknown_alias(review, target.aliases, retired=retired) if review.verdict in ("APPROVED", "CHANGES_REQUIRED") else ""
    if stray:
        review = Review(verdict="OP_FAILURE", kind="contract", raw=str(answer), error=f"file={stray} names no root; every location is <alias>/<path>")
    if repairing:
        if review.verdict == "APPROVED":
            # A tail cannot show that nothing was found, so a repair never approves.
            return Review(
                verdict="OP_FAILURE",
                kind="contract",
                raw=str(answer),
                error="the repair call approved, which it may not; the original answer stays unreadable",
            )
        if review.verdict == "CHANGES_REQUIRED":
            recovered = [line for line in contract.records(review.findings) if line and not line.endswith(_INCOMPLETE)]
            if not recovered:
                # Nothing blocking was recovered (or only the "transcript incomplete" sentinel the
                # repair prompt prescribes): no review stands behind this round, so it is the
                # original failure, counted against the budget -- not a round Claude must "fix".
                return Review(
                    verdict="OP_FAILURE",
                    kind="contract",
                    raw=str(answer),
                    error="the repair call recovered no finding from the unreadable answer",
                )
            review.repaired = str(ticket["repair"])
    return review


def _has_output(answer: Path) -> bool:
    try:
        with answer.open("rb") as handle:
            return bool(handle.read(65536).strip())
    except OSError:
        return False


def _headline(target: Target, verdict: str) -> str:
    what = "The final review" if target.scope == "final" else f"The review of phase {target.phase}"
    return {
        "APPROVED": f"{what} approved the change.",
        "CHANGES_REQUIRED": f"{what} asks for changes.",
        "NEEDS_HUMAN": f"{what} needs the user.",
    }.get(verdict, f"{what} failed and approved nothing.")


def _original_prose(review: Review) -> str:
    """The round's reasoning: its own prose, or for a repaired round the prose of the answer the
    repair re-emitted the block of (the repair call itself writes only the block)."""
    if not review.repaired:
        return review.prose
    try:
        text = decode(Path(review.repaired).read_bytes())
    except OSError:
        return ""
    lines = text.split("\n")
    marker = next((index for index, line in enumerate(lines) if line.strip() == contract.FINDINGS_MARKER), len(lines))
    return "\n".join(lines[:marker])


def _keep_answer(answer: Path, bundle_dir: Path) -> None:
    """Copy the parsed answer into the bundle as ``review.md``: a later question about this round
    goes to a fresh reviewer, which knows the review only from this copy."""
    try:
        text = answer.read_bytes()
    except OSError as exc:
        text = encode(f"(the review's answer at {answer} could not be read: {exc.strerror or exc})\n")
    write_private_file(bundle_dir / "review.md", text)


def _apply(run: RunState, ticket: Mapping[str, Any], target: Target, review: Review, cfg: config.Config) -> dict[str, Any]:
    """Record ``review`` on the run and answer what Claude is told."""
    seq = int(ticket["seq"])
    review.seq = seq
    review.guide = str(ticket.get("guidance") or "")
    report.store(review, target, seq=f"{seq:03d}", reports_dir=run.paths.run_dir / "reports", config=cfg)
    note = ""
    if review.verdict in ("APPROVED", "CHANGES_REQUIRED"):
        _keep_answer(Path(review.raw), _review_dir(run.paths, run.run_id, seq) / "bundle")
        run.data["round_history"].append(
            {
                "label": target.label,
                "revision": ticket["revision"],
                "seq": seq,
                "trees": dict(ticket["trees"]),
                "verdict": review.verdict,
                "findings": contract.records(review.all_findings),
                "supersedes": contract.records(review.supersedes),
                "repaired": bool(review.repaired),
                # Why the round concluded what it did, for the next fresh reviewer: bounded here so
                # the run document stays small whatever the reviewer wrote.
                "reasoning": bundle.bounded_reasoning(_original_prose(review)),
            }
        )
        run.data["failures"] = run.data["transient_failures"] = 0
        # A review that answered ends a run of failures, and the wait the budget started with it.
        run.clear_wait("failures")
        rounds = run.rounds_of(target.label)
        points = oscillation.reversals(rounds, target.label, block_severity=cfg.as_str("block_severity"))
        review.oscillating = oscillation.render(points) if points else ""
        if review.verdict == "APPROVED":
            run.approve(ticket["trees"], by="review")
        else:
            run.record_changes_required()
            run.invalidate_ticket("recorded")
            since = [entry for entry in rounds if int(entry.get("seq", 0)) > run.data["stall_floor"]]
            stalled = oscillation.persisting(since, target.label, cfg.as_int("stall_rounds"), block_severity=cfg.as_str("block_severity"))
            if stalled:
                run.wait_for_user("stall", oscillation.render_persisting(stalled))
                note = f"\nThe same findings have persisted for {cfg.as_int('stall_rounds')} rounds; the run now waits for the user.\n"
    elif review.verdict == "OP_FAILURE":
        run.invalidate_ticket("failed")
        note = _count_failure(run, review.kind or "operational", review.error, cfg)
    else:
        run.invalidate_ticket("needs-human")
        run.wait_for_user("needs-human", review.error or "the review needs the user")
    if review.verdict == "APPROVED":
        message = f"{_headline(target, review.verdict)}\n\n{report.deferred_text(review, what='approval')}Full report: {review.report}\n"
    else:
        message = report.reason(review, _headline(target, review.verdict), config=cfg) + note
    run.save()
    return {
        "ok": True,
        "status": "recorded",
        "verdict": review.verdict,
        "label": target.label,
        "report": review.report,
        "message": message,
        "run": run.summary(),
    }


def record(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Record the answer to the pending review ``args["review_id"]``.

    ``answer`` is the reviewer's output file; ``failed`` instead reports a reviewer that did not
    answer at all (``transient`` or ``operational``). The answer lands only while its ticket is
    pending and every root still has the tree it was reviewed at; a root that changed during the
    review makes the answer stale -- it judged content that is no longer there.
    """
    cfg = _load_config(paths)
    review_id = str(args["review_id"])
    failed = str(args.get("failed") or "")
    if failed and failed not in FAILURE_KINDS:
        raise RunError("bad-failure", f"--failed takes one of {', '.join(FAILURE_KINDS)}")
    if not failed and not args.get("answer"):
        raise RunError("no-answer", "record takes the reviewer's answer file, or --failed")
    with locked_state(paths) as loaded:
        run = _require(loaded)
        try:
            ticket = _pending(run, review_id)
        except RunError:
            run.save()
            raise
        try:
            target = _target(run, ticket)
            # A reviewer that did not answer judged nothing, so a root that moved meanwhile does not
            # make its failure stale: it is counted like any other.
            current = ticket["trees"] if failed else _snapshot_or_wait(run)
            if current != ticket["trees"]:
                raise RunError("stale-review", "a root changed while the review ran, so its answer judged content that is gone; call review again")
            return _record_answer(run, ticket, target, args, cfg, failed=failed)
        except RunError:
            # Stale, oversized or refused: the answer judged content that cannot be recorded.
            run.invalidate_ticket("stale")
            run.save()
            raise
        except OcrlError as exc:
            # The engine itself could not finish recording (a root that cannot be snapshotted, a
            # bundle the late scope cannot read): the ticket is retired and the failure counted
            # rather than left pending with nothing to finish it.
            run.invalidate_ticket("failed")
            note = _count_failure(run, "operational", str(exc), cfg)
            run.save()
            raise RunError("record-failed", f"the answer could not be recorded: {exc}{note}") from exc


def _record_answer(  # noqa: PLR0913 - the pieces `record` already validated
    run: RunState, ticket: dict[str, Any], target: Target, args: Mapping[str, Any], cfg: config.Config, *, failed: str
) -> dict[str, Any]:
    review_dir = _review_dir(run.paths, run.run_id, int(ticket["seq"]))
    usage = reported_usage(str(args.get("usage") or ""))
    if failed:
        review = Review(verdict="OP_FAILURE", kind=failed, error=str(args.get("error") or f"the reviewer did not answer ({failed})"), usage=usage)
    else:
        answer = Path(str(args["answer"])).expanduser()
        if not ticket.get("repair") and not _has_output(answer):
            # A reviewer that said nothing has nothing to repair: it failed, and the budget counts it.
            review = Review(verdict="OP_FAILURE", kind="operational", raw=str(answer), error="the reviewer produced no output", usage=usage)
            return _apply(run, ticket, target, review, cfg)
        review = _parsed(run, ticket, target, answer, cfg)
        if review.verdict == "OP_FAILURE" and review.kind == "contract" and not ticket.get("repair"):
            return _repair(run, ticket, answer, review_dir)
        if usage is not None:
            review.usage = usage
    return _apply(run, ticket, target, review, cfg)


#: The figures ``record --usage`` reads, each a :class:`arl.harness.Usage` field.
_USAGE_FIELDS: Final = ("turns", "input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens", "duration_ms")


def reported_usage(raw: str) -> harness.Usage | None:
    """What the mod says a subagent round cost (``record --usage``), for the report only.

    Observability, never a decision: anything unreadable is dropped, and a figure that is not a
    whole non-negative number is left missing rather than guessed.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        log(f"record: --usage is not JSON and is ignored: {raw[:200]!r}")
        return None
    if not isinstance(value, dict):
        return None
    figures = {key: value[key] for key in _USAGE_FIELDS if isinstance(value.get(key), int) and not isinstance(value[key], bool) and value[key] >= 0}
    return harness.Usage(**figures) if figures else None


def abandon(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Retire the pending review ``args["review_id"]`` that the person interrupted.

    Nothing judged anything, so no failure is counted and nothing is recorded: the ticket only
    stops being pending, so the loop is not held on a review no reviewer is running. A review that
    is not the pending one is left alone and said to be so.
    """
    review_id = str(args["review_id"])
    with locked_state(paths) as loaded:
        run = _require(loaded)
        ticket = run.data["pending_ticket"]
        if ticket is None or ticket.get("review_id") != review_id:
            return {"ok": True, "abandoned": False, "run": run.summary()}
        run.invalidate_ticket("abandoned")
        run.save()
        return {"ok": True, "abandoned": True, "run": run.summary()}


def _external_harness(cfg: config.Config) -> None:
    """Refuse unless ``harness`` names an external reviewer this build implements."""
    if cfg.as_str("harness") == harness.SUBAGENT:
        raise RunError("not-external", "the configured reviewer is the subagent, which the mod runs")
    try:
        harness.selected(cfg)
    except harness.UnknownHarness as exc:
        raise RunError("unknown-harness", str(exc)) from exc


def run_external(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Run the configured external reviewer for the pending review, then record its answer.

    The state lock is not held while the reviewer runs: ``/arl-stop`` or a replan meanwhile retires
    the ticket, and the late answer is then refused by :func:`record`.
    """
    cfg = _load_config(paths)
    _external_harness(cfg)
    review_id = str(args["review_id"])
    with locked_state(paths) as loaded:
        run = _require(loaded)
        try:
            ticket = _pending(run, review_id)
        except RunError:
            run.save()
            raise
        target = _target(run, ticket)
    review_dir = _review_dir(paths, str(ticket["run_id"]), int(ticket["seq"]))
    prompt, answer = review_dir / "prompt.md", review_dir / "answer.md"
    for attempt in range(2):
        invocation = runner.Invocation(bundle_dir=review_dir / "bundle", prompt_file=prompt, title=f"arl {review_id} {attempt}", out_path=answer)
        try:
            runner.invoke(target, invocation, config=cfg)
        except runner.LaunchError as exc:
            return record(paths, {"review_id": review_id, "failed": "operational", "error": str(exc)})
        except runner.ReviewerFailed as exc:
            return record(paths, {"review_id": review_id, "failed": runner.classify_op_failure(exc, answer), "error": str(exc)})
        outcome = record(paths, {"review_id": review_id, "answer": str(answer)})
        if outcome.get("status") != "repair":
            return outcome
        prompt, answer = Path(outcome["prompt_file"]), Path(outcome["answer_file"])
    return outcome


# --------------------------------------------------------------------------
# clarify
# --------------------------------------------------------------------------


def _clarified_round(run: RunState) -> dict[str, Any]:
    """The latest recorded round of the current stage: the only review a question can be about."""
    rounds = run.rounds_of(run.label())
    if not rounds:
        raise RunError("nothing-to-clarify", "no review of the current stage has been recorded yet")
    return rounds[-1]


def clarify_prepare(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Prepare one question about the latest recorded round, within ``max_clarifications``."""
    cfg = _load_config(paths)
    question = str(args.get("question") or "").strip()
    if not question:
        raise RunError("no-question", "clarify reads the question on stdin")
    with locked_state(paths) as loaded:
        run = _require(loaded)
        entry = _clarified_round(run)
        seq = int(entry["seq"])
        review_dir = _review_dir(paths, run.run_id, seq)
        bundle_dir = review_dir / "bundle"
        # Counted as asked, not as answered: a question whose reply never came back still spent
        # the budget, or a failing reviewer could be asked without end.
        asked = len(list(bundle_dir.glob("question-*.txt")))
        if asked >= cfg.as_int("max_clarifications"):
            raise RunError("clarify-budget", f"this review was already asked {asked} question(s), the max_clarifications budget")
        number = asked + 1
        write_private_file(bundle_dir / f"question-{number}.txt", encode(question + "\n"))
        files = ["range.txt", *sorted(path.name for path in bundle_dir.glob("changes.*.diff")), "review.md"]
        if entry.get("repaired"):
            files.append("repair.txt")
        if (bundle_dir / "prior-rounds.txt").is_file():
            files.append("prior-rounds.txt")
        files.append(f"question-{number}.txt")
        prompt = review_dir / f"clarify-{number}-prompt.md"
        bundle.compose_fixed("reviewer-clarify", bundle_dir, files, prompt)
        return {
            "ok": True,
            "run_id": run.run_id,
            "round_seq": seq,
            "question": number,
            **_subagent_settings(cfg),
            "prompt_file": str(prompt),
            "bundle_dir": str(bundle_dir),
            "roots": [str(root["path"]) for root in run.roots],
            "answer_file": str(review_dir / f"clarify-{number}-answer.md"),
        }


def clarify_record(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Record the reply to question ``args["question"]`` about round ``args["round_seq"]``.

    The prose goes back to Claude. A retraction block is kept only for the lines that each retract
    exactly one finding of that round (:func:`arl.contract.valid_retractions`); it changes no
    verdict, and is shown under that round in the next round's ``prior-rounds.txt``.
    """
    answer = Path(str(args["answer"])).expanduser()
    try:
        text = decode(answer.read_bytes())
    except OSError as exc:
        raise RunError("no-answer", f"the clarify answer at {answer} could not be read: {exc}") from exc
    reply = contract.parse_clarify(text)
    with locked_state(paths) as loaded:
        run = _require(loaded)
        entry = _clarified_round(run)
        seq = int(entry["seq"])
        if str(args.get("round_seq")) != str(seq) or (args.get("run_id") and args["run_id"] != run.run_id):
            raise RunError("stale-clarify", "a newer round has been recorded since this question was asked")
        rounds = run.rounds_of(entry["label"])
        ordinal = oscillation.ordinal_of(rounds, entry["label"], seq)
        # A final reviewer may not retract (its contract has no SUPERSEDES), so nor may its clarify.
        final = entry["label"] == "final"
        kept = contract.valid_retractions(entry, list(reply.supersedes), ordinal=ordinal) if ordinal is not None and not final else []
        run.data["clarify_history"].append({"label": entry["label"], "round_seq": seq, "question": int(args["question"]), "supersedes": kept})
        run.save()
    dropped = len(reply.supersedes) - len(kept)
    note = reply.problem or (f"{dropped} retraction line(s) did not name exactly one finding of that round and were not recorded" if dropped else "")
    return {"ok": True, "reply": reply.prose, "retracted": kept, "problem": note}


def clarify_run_external(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Ask the external reviewer one question, then record its reply."""
    cfg = _load_config(paths)
    _external_harness(cfg)
    prepared = clarify_prepare(paths, args)
    try:
        runner.run_clarify(
            str(paths.launch_dir),
            Path(prepared["bundle_dir"]),
            prompt_file=Path(prepared["prompt_file"]),
            title=f"arl clarify {prepared['round_seq']}-{prepared['question']}",
            out_path=Path(prepared["answer_file"]),
            config=cfg,
            roots=tuple(prepared["roots"]),
        )
    except runner.ReviewerFailed as exc:
        raise RunError("clarify-failed", f"the reviewer could not answer: {exc}") from exc
    return clarify_record(paths, {**prepared, "answer": prepared["answer_file"]})
