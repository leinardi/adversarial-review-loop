"""The review pipeline: ``prepare`` a review, ``record`` its answer, and the external reviewer.

``prepare`` snapshots every root, builds the bundle and the prompt, and makes a **ticket** the
run's pending review: review id, run id, stage, phase, phase-list revision and the snapshot tree
of every root. The mod (or :func:`run_external`) runs the reviewer and hands its answer to
``record``, which takes it only while that ticket is still the pending one and every root still
has the tree it was reviewed at. A ticket is retired by ``stop``, ``accept``, a replan, ``init
--new``, a newer ``prepare`` and its own recording, so a late answer can never land on a state it
was not prepared for.

**No failure is an approval** (AGENTS.md rule 1). An answer that breaks the output contract gets
one repair call and is otherwise ``OP_FAILURE``; a repair may never approve, except one that only
accounts for the earlier findings a readable approval skipped (``RESOLVED``); failures count
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
import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from arl import bundle, config, contract, globmatch, guidance, harness, modelcmd, oscillation, report, runner, workspace
from arl.atomic import write_private_file
from arl.bundle import BundleError, BundleTooLarge
from arl.contract import Review, Target
from arl.errors import OcrlError
from arl.state import DISPOSITION_STATUSES, RunError, RunPaths, RunState, locked_state, repositories
from arl.util import decode, encode, log

__all__ = [
    "REPAIR_TAIL_BYTES",
    "abandon",
    "next_turn",
    "prepare",
    "record",
    "reported_model",
    "reported_usage",
    "run_external",
]

#: How much of a malformed transcript the repair call is shown (its tail, where the block is).
REPAIR_TAIL_BYTES: Final = 65536

#: The detail of the one finding ``prompts/reviewer-repair.md`` prescribes when the transcript
#: tail states no findings: a repair carrying only it recovered nothing.
_INCOMPLETE: Final = "| review transcript incomplete"

#: How much of a failed round's error ``failure_history`` keeps; the report holds all of it.
_FAILURE_ERROR_CHARS: Final = 500

#: Failure kinds the mod may report for a subagent that did not answer.
#: How a reviewer that gave no answer failed. ``model``: its model is unknown, not accessible, or
#: was replaced by the session; the run waits for the user at once, with no retry and no budget.
FAILURE_KINDS: Final = ("transient", "operational", "model")


def _require(run: RunState | None) -> RunState:
    if run is None:
        raise RunError("no-run", "there is no run here; start one with /arl-implement")
    return run


def _review_dir(paths: RunPaths, run_id: str, seq: int) -> Path:
    """Named for the run as well as the sequence: an external reviewer of an archived run may
    still be writing by name, and must not land in the next run's review of the same number."""
    return paths.run_dir / "reviews" / f"{run_id}-{seq:03d}"


def _load_config(paths: RunPaths, run: RunState | None = None) -> config.Config:
    """The configuration, with ``run``'s own model choice (``/arl-model``) when one is given."""
    return config.load(str(paths.launch_dir), overrides=run.model_overlay if run is not None else None)


def _pinned(cfg: config.Config) -> dict[str, str]:
    """What a ticket records of the reviewer that runs it: ``cfg`` already through ``for_round``."""
    choice = config.model_choice(cfg)
    return {"harness": cfg.as_str("harness"), "model_id": choice.id, "model_tier": choice.tier, "variant": cfg.as_str("variant")}


def _for_ticket(cfg: config.Config, ticket: Mapping[str, Any]) -> config.Config:
    """``cfg`` as the ticket's round runs it: the reviewer it recorded at ``prepare``.

    A model changed since (``/arl-model``, a config file, ``variant``) is the next review's: the
    one in flight keeps what it was prepared with. A ticket from before the model was recorded
    falls back to resolving it again (:func:`arl.config.for_round`).
    """
    if "model_id" not in ticket:
        return config.for_round(cfg, round_number=int(ticket.get("round") or 1), final=ticket.get("stage") == "final")
    name = str(ticket.get("harness") or "")
    tier = str(ticket.get("model_tier") or "")
    pinned: dict[str, Any] = {"harness": name, "late_model": "", "variant": str(ticket.get("variant") or "")}
    # A tier is pinned by its key, so the report still says which tier ran.
    pinned.update({"model": tier, f"{name}_{tier}_model": str(ticket["model_id"])} if tier else {"model": str(ticket["model_id"])})
    return config.Config({**cfg.values, **pinned})


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------


def _subagent_settings(cfg: config.Config) -> dict[str, Any]:
    """What the mod spawns the reviewer subagent with: the round's model id (``cfg`` already
    through :func:`arl.config.for_round`), and how long it waits for the answer."""
    return {"reviewer_model": config.model_choice(cfg).id, "timeout_sec": cfg.as_int("timeout_sec")}


def _validated_verification(raw: object) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        raise RunError("bad-verification", "verification must be a list of objects (root, command, cwd, status, output)")
    return [dict(entry) for entry in raw]


#: How long one disposition's note may be.
_NOTE_BYTES: Final = 4096


def _validated_dispositions(raw: object, run: RunState) -> tuple[list[dict[str, str]], int]:
    """What the implementer states about the previous round's findings, or a refusal, and how many
    entries were ignored.

    Each entry names a finding of the latest recorded round of the current stage by its id
    (``r<round>.<n>``), says ``fixed`` or ``rejected``, and may carry a note. An id that names no
    such finding, or a second entry for one id, is refused as malformed verification is: a
    statement about a finding the reviewer cannot find would be shown as evidence of nothing.

    A stage with no recorded round yet has nothing for an id to name (typically the previous
    phase's ids carried into the next phase's first review, or a replan): every entry is checked
    for its shape and then ignored, and the count says so, rather than refusing the review.
    """
    if raw is None:
        return [], 0
    shape = "dispositions must be a list of objects (id, status, note)"
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        raise RunError("bad-dispositions", shape)
    if not raw:
        return [], 0
    rounds = run.rounds_of(run.label())
    order = len(rounds)
    stored = rounds[-1].get("findings") if rounds else None
    known = {contract.finding_id(order, number) for number in range(1, (len(stored) if isinstance(stored, list) else 0) + 1)}
    out: list[dict[str, str]] = []
    for entry in raw:
        ident, status, note = entry.get("id"), entry.get("status"), entry.get("note", "")
        if not isinstance(ident, str) or (rounds and ident not in known):
            names = ", ".join(sorted(known)) or "none"
            raise RunError("bad-dispositions", f"disposition id {str(ident)[:40]!r} names no finding of round {order} (its ids: {names})")
        if any(seen["id"] == ident for seen in out):
            raise RunError("bad-dispositions", f"finding {ident} has more than one disposition")
        if status not in DISPOSITION_STATUSES:
            raise RunError("bad-dispositions", f"disposition status must be one of {', '.join(DISPOSITION_STATUSES)}, not {str(status)[:40]!r}")
        if not isinstance(note, str) or len(encode(note)) > _NOTE_BYTES:
            raise RunError("bad-dispositions", f"a disposition note must be text of at most {_NOTE_BYTES} bytes")
        out.append({"id": ident, "status": str(status), "note": note})
    if not rounds:
        return [], len(out)
    return out, 0


def _ignored_note(ignored: int) -> str:
    """What Claude is told about dispositions a stage with no earlier round had nothing to answer."""
    if not ignored:
        return ""
    return f"Note: {ignored} disposition(s) were ignored: this stage had no earlier review for them to answer.\n"


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


def next_turn(run: RunState, cfg: config.Config, verdict: str) -> str:
    """What Claude is told about the rest of its turn after a recorded round, or nothing.

    A run waiting for the user says nothing here: the mod puts the decision to them, and their
    choice is what Claude is told.
    """
    if run.data["waiting"] is not None or run.stage() == "done":
        return ""
    if run.status == "paused":
        # Measured live: a paused run's approval read as "go on", and Claude implemented every
        # remaining phase in the same turn. Pausing is the user's stop.
        first = (
            f"Commit each changed git repository by its own conventions{repositories(run)}, then stop: " if verdict == "APPROVED" else "Stop here: "
        )
        return (
            f"\nThe user paused the run. {first}do not start the next phase or fix anything more until the user resumes "
            "it with /arl-resume. Tell the user where the run stands.\n"
        )
    if verdict == "APPROVED" and cfg.as_bool("auto_compact"):
        # The loop compacts only between turns: a next phase started in this turn would be
        # summarised away mid-work, so the compaction never comes.
        return (
            f"\nCommit each changed git repository by its own conventions{repositories(run)}, then end your turn without starting the "
            "next stage: the loop compacts the conversation and prompts it.\n"
        )
    return ""


def _skip(run: RunState, trees: Mapping[str, str], cfg: config.Config, *, note: str = "") -> dict[str, Any]:
    what = "the final review" if run.stage() == "final" else f"phase {run.current_phase()}"
    run.approve(trees, by="ignored")
    run.save()
    message = f"Every change of {what} matches ignore_globs ({', '.join(cfg.as_list('ignore_globs'))}), so it was approved without a review.\n"
    message += note
    message += next_turn(run, cfg, "APPROVED")
    return {"ok": True, "status": "skipped", "verdict": "APPROVED", "message": message, "run": run.summary()}


def prepare(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Prepare the review of the current stage and make it the pending ticket."""
    verification = _validated_verification(args.get("verification"))
    with locked_state(paths) as loaded:
        run = _require(loaded)
        cfg = _load_config(paths, run)
        # Paused means the loop stops prompting Claude, not that a review Claude asks for is refused.
        run.require_status("active", "paused")
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
        stated, ignored = _validated_dispositions(args.get("dispositions"), run)
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
            return _skip(run, trees, cfg, note=_ignored_note(ignored))
        seq = run.next_review_seq()
        if stated:
            # On the run, not the ticket: a review that fails or is abandoned keeps them for the next.
            run.state_dispositions(run.label(), stated, seq=seq)
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
        # The model this round runs on, from here to its report.
        cfg = config.for_round(cfg, round_number=built.round_number, final=stage == "final")
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
            # Who reviews it, as configured now: what /arl-stats shows the round ran on.
            "model": harness.display_model(cfg),
            # ... and what runs it, from here to its record: a model changed meanwhile is the next
            # review's (`_for_ticket`).
            **_pinned(cfg),
            # For Claude only, with whatever the round answers; never part of the bundle.
            "note": _ignored_note(ignored),
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


def _model_choices(ticket: Mapping[str, Any], which: str, configured: Callable[[str], config.Config]) -> list[str]:
    """The ``/arl-model`` words that would name another model than the refused one, best first.

    ``configured(word)`` is the configuration once ``/arl-model <which> <word>`` ran. ``default``
    alone is offered when it names another model; otherwise each tier that does. ``<model>`` is
    always left to the caller.
    """
    refused = (str(ticket.get("harness") or ""), str(ticket.get("model_id") or ""))
    round_number = 1 if which == "first" else 2
    choices = []
    for word in config.MODEL_WORDS:
        cfg = configured(word)
        choice = config.reviewer_model(cfg, round_number=round_number)
        if choice.id and (cfg.as_str("harness"), choice.id) != refused:
            choices.append(word)
    return ["default"] if "default" in choices else choices


def _model_refusal(
    ticket: Mapping[str, Any], target: Target, error: str, configured: Callable[[str, str], config.Config]
) -> tuple[str, dict[str, str] | None]:
    """Why a model failure stops the run, what the user can do, and the switch offered first.

    The text names the model and the round. ``configured(which, word)`` is the configuration once
    ``/arl-model <which> <word>`` ran; a choice that names the refused model again is not offered.
    The switch is ``{"which", "word"}`` of the first choice offered, ``None`` when none is.
    """
    shown = str(ticket.get("model") or ticket.get("model_id") or "the configured model")
    stage = "the final review" if ticket.get("stage") == "final" else f"round {int(ticket.get('round') or 1)} of {target.label}"
    which = "first" if ticket.get("stage") == "final" or int(ticket.get("round") or 1) < 2 else "late"

    # The default puts every round back on the built-in models, as it did before a tier existed;
    # a tier is offered for the rounds that failed only. Each word is judged at that scope.
    def scope(word: str) -> str:
        return "all" if word == "default" else which

    words = _model_choices(ticket, which, lambda word: configured(scope(word), word))
    switches = [{"which": scope(word), "word": word} for word in words]
    offered = [f"/arl-model {word}" if word == "default" else f"/arl-model {which} {word}" for word in words] + [f"/arl-model {which} <model>"]
    text = f"The reviewer's model {shown} cannot be used for {stage}: {error}. Nothing was retried. Choose another model ({', or '.join(offered)}), then /arl-resume."
    return text, (switches[0] if switches else None)


def _repair_reason(unaccounted: tuple[str, ...]) -> str:
    """``repair-reason.txt``: why an answer whose block the engine read still failed, and what the
    repair call is to do about each id."""
    return (
        "# Why your block failed\n\n"
        "The engine read your findings block. It failed because your review approved while these earlier blocking "
        'findings are still open; each is listed under "Still open" in `prior-rounds.txt`:\n\n'
        + "".join(f"- {ident}\n" for ident in unaccounted)
        + "\nRe-emit your block. For each id above, either add `RESOLVED id=<id> | <what in the code shows it is gone>`, only "
        "if your review above actually checked that the defect is gone, or emit it again as a `FINDING` that is still "
        "open. If nothing in the transcript shows it was checked, it is still open. Do not add a `SUPERSEDES` line your "
        "review did not already have.\n"
    )


def _repair(run: RunState, ticket: dict[str, Any], answer: Path, review_dir: Path, *, unaccounted: tuple[str, ...] = ()) -> dict[str, Any]:
    """Arrange the one repair call a malformed answer gets, and answer what the caller runs.

    ``unaccounted`` names the earlier blocking findings an approval left open (the block itself was
    readable): ``repair-reason.txt`` tells the repair call so, and the ticket remembers it, since
    such a repair may approve once each one is accounted for.
    """
    try:
        raw = answer.read_bytes()
    except OSError:
        raw = b""
    bundle_dir = review_dir / "bundle"
    write_private_file(bundle_dir / "repair.txt", raw[-REPAIR_TAIL_BYTES:] or b"(the reviewer wrote nothing)\n")
    files = ["range.txt", "repair.txt"]
    if unaccounted:
        write_private_file(bundle_dir / "repair-reason.txt", encode(_repair_reason(unaccounted)))
        files += ["repair-reason.txt", "prior-rounds.txt"]
    prompt = review_dir / "repair-prompt.md"
    bundle.compose_fixed("reviewer-repair", bundle_dir, files, prompt)
    ticket["repair"] = str(answer)
    ticket["repair_unaccounted"] = list(unaccounted)
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


def _cited(line: str) -> tuple[str, int] | None:
    match = contract.FINDING_RE.match(line)
    return contract.cited_line(match.group("file")) if match else None


def _line_count(target: Target, file: str, limit: int) -> int | None:
    """How many lines ``<alias>/<path>`` has under its root now, or ``None`` when that cannot be told.

    ``None`` for no such root or file, a path that leaves its root, and a file past ``limit``
    bytes: a citation the engine cannot check is left as the reviewer wrote it.
    """
    alias, _, relative = file.partition("/")
    root = next((root for root in target.roots if root.alias == alias), None)
    if root is None or not relative:
        return None
    base = Path(root.path).resolve()
    path = (base / relative).resolve()
    try:
        if not path.is_relative_to(base) or not path.is_file() or path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _known_ids(rounds: list[dict[str, Any]]) -> frozenset[str]:
    """Every finding id of ``rounds``, by position, as :func:`arl.contract.finding_id` numbers them."""
    known: set[str] = set()
    for order, entry in enumerate(rounds, start=1):
        stored = entry.get("findings")
        known.update(contract.finding_id(order, number) for number in range(1, (len(stored) if isinstance(stored, list) else 0) + 1))
    return frozenset(known)


def _unaccounted_error(missing: list[str]) -> str:
    return (
        f"the review approved while earlier blocking finding(s) {', '.join(missing)} are still open: an approval must mark each "
        "RESOLVED or withdraw it with SUPERSEDES, or else raise it again as a FINDING and ask for changes"
    )


def _missing_line(kept: str, original: str) -> str:
    """The first line of ``original`` that ``kept`` lacks, counting repeats, or ``""``."""
    left = contract.records(kept)
    for line in contract.records(original):
        if line not in left:
            return line
        left.remove(line)
    return ""


def _repair_violation(review: Review, original: Review | None, *, approving: bool) -> str:
    """Why an accounting repair's block breaks faith with the block it re-emits, or ``""``.

    An ordinary repair, of an answer the engine could not read, is held by :func:`_invented_resolved`
    instead: it may copy a ``RESOLVED`` line only from that answer.

    Its ``SUPERSEDES`` lines must be exactly the original's: a withdrawal the review never made,
    recorded, would close findings for every later round, so none may be added whatever the verdict.
    An approving repair must also keep every ``FINDING``, ``SUPERSEDES`` and ``RESOLVED`` line of
    the original; it may add only ``RESOLVED`` and ``FINDING`` lines. ``original`` is ``None`` when
    that answer no longer parses, which nothing can be shown to keep.
    """
    if original is None:
        return "the answer the repair call re-emits can no longer be read"
    added = _missing_line(original.supersedes, review.supersedes)
    if added:
        return f"the repair call added a SUPERSEDES line the review never made, which it may not: {added[: contract.CONTRACT_ECHO_CHARS]}"
    if approving:
        for block, kind in (
            (review.all_findings, original.all_findings),
            (review.supersedes, original.supersedes),
            (review.resolved, original.resolved),
        ):
            dropped = _missing_line(block, kind)
            if dropped:
                return f"the repair call dropped a line of the review it re-emitted, which it may not: {dropped[: contract.CONTRACT_ECHO_CHARS]}"
    return ""


def _invented_resolved(review: Review, original: Path) -> str:
    """The first ``RESOLVED`` line of an ordinary repair that the original answer lacks, or ``""``.

    The original is read as raw lines, split as the parser splits them (:func:`arl.contract.records`),
    since it need not hold a readable block; repeats are counted. An original that cannot be read
    holds no line, so every ``RESOLVED`` of the repair is one it lacks.
    """
    try:
        lines = "\n".join(contract.records(decode(original.read_bytes())))
    except OSError:
        lines = ""
    return _missing_line(lines, review.resolved)


def _accounting_original(ticket: Mapping[str, Any], cfg: config.Config) -> Review | None:
    """The answer an accounting repair re-emits, parsed; ``None`` when it no longer parses."""
    original = contract.parse(Path(str(ticket["repair"])), config=cfg, allow_supersedes=True, allow_resolved=True)
    return original if original.verdict in ("APPROVED", "CHANGES_REQUIRED") else None


def _repaired(review: Review, ticket: Mapping[str, Any], answer: Path, cfg: config.Config, *, accounting_repair: bool) -> Review:
    """What a repair call's parsed answer comes to: the checks on what a repair may emit."""
    if accounting_repair and review.verdict in ("APPROVED", "CHANGES_REQUIRED"):
        # The repair only accounts for the findings an approval left open; it may not rewrite
        # the review it re-emits, and an approval is the original review's own.
        violation = _repair_violation(review, _accounting_original(ticket, cfg), approving=review.verdict == "APPROVED")
        if violation:
            return Review(verdict="OP_FAILURE", kind="contract", raw=str(answer), error=violation)
    if not accounting_repair and review.verdict == "CHANGES_REQUIRED":
        # A recorded RESOLVED closes a finding for every later round, so an ordinary repair may
        # only copy one the unreadable answer already wrote, never state one of its own.
        invented = _invented_resolved(review, Path(str(ticket["repair"])))
        if invented:
            return Review(
                verdict="OP_FAILURE",
                kind="contract",
                raw=str(answer),
                error=f"the repair call wrote a RESOLVED line the original answer does not contain, which it may not: {invented[: contract.CONTRACT_ECHO_CHARS]}",
            )
    if review.verdict == "APPROVED":
        # Only an accounting repair gets here.
        review.repaired = str(ticket["repair"])
        return review
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


def _parsed(run: RunState, ticket: Mapping[str, Any], target: Target, answer: Path, cfg: config.Config) -> Review:
    """Parse ``answer`` into the round's :class:`Review`, with every engine check a parse alone
    cannot make: roots, ``RESOLVED`` ids, the accounting an approval owes, and what a repair may
    emit. An accounting repair may approve but not rewrite the block it re-emits
    (:func:`_repair_violation`); an ordinary repair never approves, recovers at least one blocking
    finding, and writes no ``RESOLVED`` the original answer lacks (:func:`_invented_resolved`)."""
    repairing = bool(ticket.get("repair"))
    # The repair of an approval that skipped an earlier finding: its block was read, so it may
    # re-emit SUPERSEDES lines, and it may approve once every finding is accounted for.
    owed = ticket.get("repair_unaccounted")
    accounting_repair = repairing and isinstance(owed, list) and bool(owed)
    rounds = run.rounds_of(target.label) if target.is_phase else []
    scope = bundle.late_scope(run, target)
    review = contract.parse(
        answer,
        config=cfg,
        allow_supersedes=target.is_phase and (not repairing or accounting_repair),
        allow_resolved=bool(rounds),
        scope=scope,
    )
    review.raw = str(answer)
    retired = frozenset(alias for alias in run.data.get("retired_aliases", []) if isinstance(alias, str))
    answered = review.verdict in ("APPROVED", "CHANGES_REQUIRED")
    stray = contract.unknown_alias(review, target.aliases, retired=retired) if answered else ""
    unknown = contract.unknown_resolved(review, _known_ids(rounds)) if answered and not stray else ""
    if stray:
        review = Review(verdict="OP_FAILURE", kind="contract", raw=str(answer), error=f"file={stray} names no root; every location is <alias>/<path>")
    elif unknown:
        review = Review(
            verdict="OP_FAILURE", kind="contract", raw=str(answer), error=f"RESOLVED id={unknown} names no finding of an earlier round of this review"
        )
    elif answered:
        review.notes += contract.citation_notes(review.all_findings, lambda file: _line_count(target, file, cfg.as_int("max_file_bytes")))
    if repairing and review.verdict == "APPROVED" and not accounting_repair:
        # A tail cannot show that nothing was found, so a repair never approves.
        return Review(
            verdict="OP_FAILURE",
            kind="contract",
            raw=str(answer),
            error="the repair call approved, which it may not; the original answer stays unreadable",
        )
    if review.verdict == "APPROVED" and rounds:
        # A later round may not approve past an earlier blocking finding still open: a fresh
        # reviewer that forgot one has not checked it, and nor has a round that never named it.
        threshold = config.threshold_rank(cfg.as_str("block_severity"))
        missing = contract.unaccounted(review, rounds, threshold=threshold)
        if missing:
            return Review(verdict="OP_FAILURE", kind="contract", raw=str(answer), error=_unaccounted_error(missing), unaccounted=tuple(missing))
    if repairing:
        return _repaired(review, ticket, answer, cfg, accounting_repair=accounting_repair)
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


def _accounting(ticket: Mapping[str, Any], review: Review, answered_by: str) -> dict[str, Any]:
    """What a recorded round or failure cost and who ran it, for ``/arl-stats``; never read for a decision."""
    return {
        "round": int(ticket.get("round") or 0),
        "harness": str(ticket.get("harness") or ""),
        # The model that answered when the harness said so, else the one configured.
        "model": answered_by or str(ticket.get("model") or ""),
        "usage": harness.usage_record(review.usage),
    }


def _apply(  # noqa: PLR0913 - the round, and the one display-only fact about who answered it
    run: RunState, ticket: Mapping[str, Any], target: Target, review: Review, cfg: config.Config, *, answered_by: str = ""
) -> dict[str, Any]:
    """Record ``review`` on the run and answer what Claude is told."""
    cfg = _for_ticket(cfg, ticket)
    seq = int(ticket["seq"])
    review.seq = seq
    review.guide = str(ticket.get("guidance") or "")
    report.store(review, target, seq=f"{seq:03d}", reports_dir=run.paths.run_dir / "reports", config=cfg)
    note = ""
    accounting = _accounting(ticket, review, answered_by)
    if review.verdict in ("APPROVED", "CHANGES_REQUIRED"):
        _keep_answer(Path(review.raw), _review_dir(run.paths, run.run_id, seq) / "bundle")
        run.data["round_history"].append(
            {
                **accounting,
                "blocking": len(contract.records(review.findings)),
                "deferred": len(contract.records(review.deferred)),
                "label": target.label,
                "revision": ticket["revision"],
                "seq": seq,
                "trees": dict(ticket["trees"]),
                "verdict": review.verdict,
                "findings": contract.records(review.all_findings),
                "supersedes": contract.records(review.supersedes),
                # The reviewer's statements that an earlier finding is gone; never read for a verdict.
                "resolved": contract.records(review.resolved),
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
        # What this round's finding ids are numbered from: the next round may answer them.
        review.ordinal = len(rounds)
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
    else:
        # A round that failed still cost what it cost: counted apart, since it decided nothing.
        run.data["failure_history"].append(
            {
                **accounting,
                "label": target.label,
                "revision": ticket["revision"],
                "seq": seq,
                "verdict": review.verdict,
                "kind": review.kind,
                "error": review.error[:_FAILURE_ERROR_CHARS],
            }
        )
        if review.verdict == "OP_FAILURE" and review.kind == "model":
            run.invalidate_ticket("failed")
            detail, switch = _model_refusal(
                ticket,
                target,
                review.error,
                lambda which, word: config.load(str(run.paths.launch_dir), overrides=modelcmd.overlay_for(which, word, run.model_overlay)),
            )
            run.wait_for_user("model", detail, switch=switch)
            note = f"\n{detail}\n"
        elif review.verdict == "OP_FAILURE":
            run.invalidate_ticket("failed")
            note = _count_failure(run, review.kind or "operational", review.error, cfg)
        else:
            run.invalidate_ticket("needs-human")
            run.wait_for_user("needs-human", review.error or "the review needs the user")
    if review.verdict == "APPROVED":
        # A note is about a finding, and an approval shows Claude only the deferred ones.
        shown = {cited[0] for cited in map(_cited, contract.records(review.deferred)) if cited is not None}
        notes = "".join(f"Review note: {note}\n\n" for note in contract.records(review.notes) if note.partition(": ")[0] in shown)
        message = f"{_headline(target, review.verdict)}\n\n{notes}{report.deferred_text(review, what='approval')}Full report: {review.report}\n"
    else:
        message = report.reason(review, _headline(target, review.verdict), config=cfg, waiting=run.data["waiting"] is not None) + note
    stored_note = ticket.get("note")
    message += stored_note if isinstance(stored_note, str) else ""
    message += next_turn(run, cfg, review.verdict)
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
    review_id = str(args["review_id"])
    failed = str(args.get("failed") or "")
    if failed and failed not in FAILURE_KINDS:
        raise RunError("bad-failure", f"--failed takes one of {', '.join(FAILURE_KINDS)}")
    if not failed and not args.get("answer"):
        raise RunError("no-answer", "record takes the reviewer's answer file, or --failed")
    with locked_state(paths) as loaded:
        run = _require(loaded)
        cfg = _load_config(paths, run)
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
    answered_by = reported_model(str(args.get("usage") or ""))
    if failed:
        # Whatever a failed run printed (a CLI's usage text, an auth error) is kept in the report:
        # it is how the person finds out why, and it is never read for a verdict.
        review = Review(
            verdict="OP_FAILURE",
            kind=failed,
            raw=str(args.get("answer") or ""),
            error=str(args.get("error") or f"the reviewer did not answer ({failed})"),
            usage=usage,
        )
    else:
        answer = Path(str(args["answer"])).expanduser()
        if not ticket.get("repair") and not _has_output(answer):
            # A reviewer that said nothing has nothing to repair: it failed, and the budget counts it.
            review = Review(verdict="OP_FAILURE", kind="operational", raw=str(answer), error="the reviewer produced no output", usage=usage)
            return _apply(run, ticket, target, review, cfg, answered_by=answered_by)
        review = _parsed(run, ticket, target, answer, cfg)
        if review.verdict == "OP_FAILURE" and review.kind == "contract" and not ticket.get("repair"):
            return _repair(run, ticket, answer, review_dir, unaccounted=review.unaccounted)
        if usage is not None:
            review.usage = usage
    return _apply(run, ticket, target, review, cfg, answered_by=answered_by)


#: The figures ``record --usage`` reads, each a :class:`arl.harness.Usage` field.
_USAGE_FIELDS: Final = ("turns", "input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens", "duration_ms")


def _usage_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        log(f"record: --usage is not JSON and is ignored: {raw[:200]!r}")
        return None
    return value if isinstance(value, dict) else None


def reported_model(raw: str) -> str:
    """The model ``record --usage`` says answered, or ``""``; display only, like the usage."""
    value = _usage_object(raw)
    model = value.get("model") if value is not None else None
    return model[:_MODEL_CHARS] if isinstance(model, str) else ""


#: How long a reported model id may be; anything longer is cut.
_MODEL_CHARS: Final = 120


def reported_usage(raw: str) -> harness.Usage | None:
    """What the mod says a subagent round cost (``record --usage``), for the report only.

    Observability, never a decision: anything unreadable is dropped, and a figure that is not a
    whole non-negative number is left missing rather than guessed.
    """
    value = _usage_object(raw)
    if value is None:
        return None
    figures: dict[str, Any] = {
        key: value[key] for key in _USAGE_FIELDS if isinstance(value.get(key), int) and not isinstance(value[key], bool) and value[key] >= 0
    }
    cost = value.get("cost_usd")
    # JSON's 1e309 reads as infinity, which no strict JSON reader takes back: finite only.
    if isinstance(cost, int | float) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
        figures["cost_usd"] = float(cost)
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


def _usage_arg(spent: harness.Usage | None) -> str:
    """``spent`` as ``record --usage`` reads it, ``""`` for nothing reported."""
    return json.dumps(harness.usage_record(spent)) if spent is not None else ""


def run_external(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Run the configured external reviewer for the pending review, then record its answer.

    The state lock is not held while the reviewer runs: ``/arl-stop`` or a replan meanwhile retires
    the ticket, and the late answer is then refused by :func:`record`.
    """
    review_id = str(args["review_id"])
    with locked_state(paths) as loaded:
        run = _require(loaded)
        try:
            ticket = _pending(run, review_id)
        except RunError:
            run.save()
            raise
        target = _target(run, ticket)
        # The reviewer the ticket was prepared for, not whatever is configured now.
        cfg = _for_ticket(_load_config(paths, run), ticket)
    _external_harness(cfg)
    review_dir = _review_dir(paths, str(ticket["run_id"]), int(ticket["seq"]))
    prompt, answer = review_dir / "prompt.md", review_dir / "answer.md"
    try:
        # Before anything runs: a model the reviewer does not list fails here, once, and asks the user.
        runner.check_model(cfg)
    except runner.ReviewerFailed as exc:
        return record(paths, {"review_id": review_id, "failed": runner.classify_op_failure(exc, answer), "error": str(exc)})
    spent: harness.Usage | None = None
    for attempt in range(2):
        invocation = runner.Invocation(bundle_dir=review_dir / "bundle", prompt_file=prompt, title=f"arl {review_id} {attempt}", out_path=answer)
        try:
            spent = harness.add_usage(spent, runner.invoke(target, invocation, config=cfg))
        except runner.LaunchError as exc:
            # A repair that could not start still leaves the review before it paid for.
            return record(paths, {"review_id": review_id, "failed": "operational", "error": str(exc), "usage": _usage_arg(spent)})
        except runner.ReviewerFailed as exc:
            failure = runner.classify_op_failure(exc, answer)
            return record(paths, {"review_id": review_id, "failed": failure, "error": str(exc), "answer": str(answer), "usage": _usage_arg(spent)})
        # The round's cost so far, the repair call's included, as the mod reports a subagent's.
        outcome = record(paths, {"review_id": review_id, "answer": str(answer), "usage": _usage_arg(spent)})
        if outcome.get("status") != "repair":
            return outcome
        prompt, answer = Path(outcome["prompt_file"]), Path(outcome["answer_file"])
    return outcome
