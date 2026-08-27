"""``resume`` -- continue an armed activation in a new session, or adjust it in this one.

A second arming path (AGENTS.md calls it exactly that), used instead of ``arm`` when a plan
already has a frozen baseline and approvals that must not be lost: ``arm`` always starts a
fresh activation, wiping ``phases``, ``approved_trees`` and the baseline tree, which is right
for a new plan and wrong for coming back to an old one tomorrow.

Two shapes, decided purely by whether ``--session`` names the worktree's most recent
activation:

- **Cross-session** (the ordinary case: a new Claude Code session picks the plan back up).
  The predecessor is retired into a blocking ``RESUMED`` status *before* the successor is
  published, and the successor is materialised from a snapshot taken at that exact moment --
  never by re-reading the predecessor afterwards, which would read back the retirement note
  it just wrote over whatever was there before. See ``_resume_cross_session``.
- **Same-session** (re-running ``resume`` to change ``--until``, the model, or the plan
  without a new session). There is nothing to retire; the live document is mutated in place.
  See ``_resume_same_session``.

Both paths share one property Rule 0 already established for ``arm``: a same-session failure
must leave the live activation untouched (a typo in ``--model`` must not stamp ``ARM_FAILED``
over a perfectly good ``ACTIVE`` document and take the whole run down), while a cross-session
failure always persists *something* under the new session id, because the resume skill has
already registered the hooks there and a missing pointer reads as "arming never ran". See
``_fail``.

**No automatic rollback.** Once the predecessor is retired, a later failure in this same
call does not un-retire it. The predecessor stays ``RESUMED`` (denying), and the successor is
left ``ARM_FAILED`` (also denying) -- both directions deny, which is the fail-closed order,
and the recovery is the same one every other wedge in this gate names:
``/opencode-review-loop:implement <plan.md>``.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from ocrl import commands, gitsnap, paths, planrev
from ocrl import config as config_module
from ocrl.atomic import DIR_MODE, FILE_MODE, ensure_private_dir, write_private_atomic
from ocrl.commands import arm
from ocrl.errors import StateLoadError
from ocrl.state import State, pointer_write
from ocrl.util import now

__all__ = ["run"]

_BOOL_FLAGS: Final = ("--allow-dirty", "--abandon-pending", "--replan")
_VALUE_FLAGS: Final = ("--until", "--plan", "--model", "--variant")

#: The only stored statuses resume may continue from -- deliberately an allow-list, not a
#: deny-list of terminal ones. A deny-list fails open the moment a new status is added and
#: this function is not updated for it, or when ``state.json`` -- which AGENTS.md is explicit
#: is not a trust boundary -- carries a value nothing here ever wrote. ``STALE`` is not
#: listed because it is never a *stored* value; it is derived from ``armed_at`` via the TTL,
#: and every one of these four may legitimately be effectively stale and still resume --
#: ``armed_at`` is refreshed on every resume, which is what un-stales it.
_RESUMABLE: Final = frozenset({"ACTIVE", "ARMED", "RECONCILE", "DISARMED"})

NO_SESSION_MESSAGE: Final = (
    "**opencode-review-loop: RESUME FAILED** -- no session id was supplied, so no state could be recorded. "
    "The review loop is NOT active in this session."
)

SAME_SESSION_FAILURE: Final = """\
**opencode-review-loop: RESUME FAILED -- the live activation was left untouched.**

Reason: {reason}

Nothing was written: this session's activation is exactly as it was before this command ran. \
Fix the cause and run /opencode-review-loop:resume again.
"""

NEW_SESSION_FAILURE: Final = """\
**opencode-review-loop: RESUME FAILED -- the review loop is NOT active in this session.**

Reason: {reason}

Every file mutation and every commit in this worktree is denied until this is resolved.
"""

WEDGED_SUFFIX: Final = """

This worktree's predecessor activation was already retired before this failure, so it is now \
wedged: neither the old session nor this one is a live, working activation. \
Re-arm with /opencode-review-loop:implement <plan.md>.
"""


class _ResumeFailure(Exception):
    """A resume that must be reported, and persisted the way ``_fail`` decides.

    ``retired`` is set once the predecessor has been retired: from that point on, a further
    failure must not leave the worktree with only a denying predecessor and no explanation on
    the successor's own session id -- see the module docstring, "No automatic rollback".
    """

    def __init__(self, reason: str, *, retired: bool = False) -> None:
        self.retired = retired
        super().__init__(reason)


class _EvidenceCorrupted(_ResumeFailure):
    """A plan revision's recorded evidence could not be verified as itself.

    Distinct from an ordinary ``_ResumeFailure``: every other same-session or pre-retirement
    failure means only "this resume request was rejected", and Rule 0's "a same-session
    failure writes nothing" is safe for those, because nothing about the request being bad
    implies anything is wrong with the activation itself -- a typo in ``--model`` leaves a
    perfectly good ``ACTIVE`` document exactly as it was. This one is the opposite: it means
    the activation's own frozen evidence -- what every review to date, and every review from
    now on, was and will be run against -- has been deleted, replaced, or no longer matches
    what was recorded. Writing nothing here would leave a corrupted activation reporting
    ``ACTIVE`` until the *next* commit's review happens to reach ``reviewer.build_bundle``,
    which verifies the active revision too and would itself escalate to ``NEEDS_HUMAN`` -- but
    only once a phase is actually reviewed, which can be minutes or phases away. So ``run``
    escalates the live activation to ``NEEDS_HUMAN`` immediately, whenever this is raised and
    retirement has not already happened (see ``retired``), rather than leaving the corruption
    to be discovered by whichever review happens to run next.
    """


@dataclass(frozen=True)
class _Flags:
    allow_dirty: bool = False
    #: Raw ``--until`` text, resolved through ``arm._resolve_until``.
    until: str = ""
    #: ``None`` means "not given"; only an explicit ``--plan`` triggers a forced re-read.
    plan: str | None = None
    abandon_pending: bool = False
    model: str | None = None
    variant: str | None = None
    #: Permission to redefine the remaining, not-yet-committed phases. See ``_Decision.replan``.
    replan: bool = False


@dataclass(frozen=True)
class _RevisionChange:
    """A plan revision that was decided but not yet written anywhere."""

    content: bytes
    sha256: str
    source_path: str


@dataclass(frozen=True)
class _Identity:
    """Who this resume is: which repository, which new session, which predecessor."""

    repo: str
    session: str
    prev_session: str
    same_session: bool


@dataclass(frozen=True)
class _Decision:
    """Everything decided in ``_resume`` before either path starts writing anything.

    ``allow_dirty`` is captured here, once, rather than re-derived at publication time: the
    repo config it partly comes from (``.opencode-review-loop.json``) lives *inside* the
    repository under review and is explicitly attacker-controlled input (see ``config.py``).
    Reloading it during the republication recheck would let that file be edited during the
    retirement window to flip the policy a plain resume was already refused under.
    """

    overrides: dict[str, str]
    revision: _RevisionChange | None
    #: The warning from deciding ``revision`` -- kept apart from ``warnings`` because a
    #: same-session (and, for the retirement window, a cross-session) resume re-decides the
    #: revision under the lock and must replace *only* this part of the banner, not every
    #: other warning ``_resume`` already collected (an ``--until`` clamp note, notably).
    revision_warning: str
    until: int
    until_given: bool
    #: Warnings unrelated to the revision decision (currently: an ``--until`` clamp note).
    #: Combined with ``revision_warning`` by ``_banner``.
    warnings: str
    allow_dirty: bool
    #: ``--replan`` was given: permission to redefine phases from the current one onward,
    #: granted to ``phases.py`` via ``replan_pending``. Requires a clean worktree exactly as a
    #: decided plan revision does -- see the module docstring and Phase 4's design notes.
    replan: bool


def _parse(argv: list[str]) -> tuple[str, list[str]]:
    """``(session, flag_tokens)`` from the dispatcher's arguments.

    Unlike ``arm``, there is no positional plan: a revised plan is always named by the
    ``--plan`` flag, so there is no plan/flag boundary to find, and every non-option token is
    itself a flag token. ``--args`` is the shim's single substituted ``$ARGUMENTS`` string,
    split on whitespace exactly as ``arm.split_args`` splits the flag half of its own input --
    which carries the same limitation ``arm`` already has for ``--model``/``--variant``: a
    value containing whitespace cannot survive this channel. See AGENTS.md, "The argument
    channel is not escaped".
    """
    session = ""
    flag_tokens: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--session", "--args"):
            value = argv[index + 1] if index + 1 < len(argv) else ""
            if token == "--session":
                session = value
            else:
                stripped = value.strip(arm._SPACE)
                if stripped:
                    flag_tokens.extend(re.split(rf"[{re.escape(arm._SPACE)}]+", stripped))
            index += 2
            continue
        if token:
            flag_tokens.append(token)
        index += 1
    return session, flag_tokens


def _parse_flags(tokens: list[str]) -> _Flags:
    raw = arm.parse_flag_tokens(
        tokens,
        bool_flags=_BOOL_FLAGS,
        value_flags=_VALUE_FLAGS,
        usage="--until, --plan, --allow-dirty, --abandon-pending, --replan, --model, --variant",
    )
    return _Flags(
        allow_dirty=arm.flag_bool(raw, "--allow-dirty"),
        until=arm.flag_str(raw, "--until") or "",
        plan=arm.flag_str(raw, "--plan"),
        abandon_pending=arm.flag_bool(raw, "--abandon-pending"),
        replan=arm.flag_bool(raw, "--replan"),
        model=arm.flag_str(raw, "--model"),
        variant=arm.flag_str(raw, "--variant"),
    )


# --------------------------------------------------------------------------
# Plan revision: deciding, then writing
# --------------------------------------------------------------------------


def _revisions_with_backfill(act_dir: Path, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``ocrl.planrev.revisions_with_backfill``, translated into this module's failure type.

    Every legacy (pre-resume) document gets a real revision 0 the first time it is loaded,
    through ``State._migrate``. A document ``arm`` wrote *after* that but before ``arm`` itself
    records revision 0 has none either -- ``planrev`` treats both the same way migration does:
    honest that the hash only attests to the file as found right now, not as it was when the
    activation was armed. Once any resume has touched a document, its ``plan_revisions`` is
    never empty again.

    ``planrev.EvidenceCorrupted`` is caught and re-raised as :class:`_EvidenceCorrupted` here,
    which is what makes it a ``_ResumeFailure`` -- carrying the ``retired`` flag ``run`` needs
    to decide what, if anything, may still be written (see the module docstring).
    """
    try:
        return planrev.revisions_with_backfill(act_dir, existing)
    except planrev.EvidenceCorrupted as exc:
        raise _EvidenceCorrupted(str(exc)) from exc


def _active_revision(state: State) -> dict[str, Any]:
    """The last ``plan_revisions`` entry, verified, or a synthesized one when the list is empty."""
    return _revisions_with_backfill(state.act_dir, state.data.get("plan_revisions") or [])[-1]


def _decide_revision(state: State, *, explicit_plan: str | None) -> tuple[_RevisionChange | None, str]:
    """Whether the plan changed, and a warning to surface either way. Never writes anything.

    Resume re-reads the plan when ``--plan`` is given explicitly, or when the recorded
    ``plan_path`` still exists and its bytes differ from the active revision's. An explicit
    ``--plan`` that cannot be read is a refusal (the user asked for a specific file); a
    recorded path that has simply vanished since arming is not -- the active revision stands,
    and this only warns.
    """
    active = _active_revision(state)
    if explicit_plan is not None:
        try:
            candidate_path = arm._resolve_plan(explicit_plan)
        except arm._ArmFailure as exc:
            raise _ResumeFailure(str(exc)) from exc
        try:
            candidate_bytes = Path(candidate_path).read_bytes()
        except OSError as exc:
            raise _ResumeFailure(f'the plan file could not be read: "{candidate_path}" ({exc})') from exc
    else:
        recorded = state.get("plan_path")
        if not recorded or not os.path.isfile(recorded):
            return None, ""
        candidate_path = recorded
        try:
            candidate_bytes = Path(candidate_path).read_bytes()
        except OSError:
            return None, f'\nNote: the recorded plan path ("{recorded}") could no longer be read; the active revision stands.\n'

    digest = hashlib.sha256(candidate_bytes).hexdigest()
    if digest == active.get("sha256"):
        return None, ""
    return _RevisionChange(content=candidate_bytes, sha256=digest, source_path=candidate_path), ""


def _apply_revision(*, act_dir: Path, existing: list[dict[str, Any]], change: _RevisionChange, phase: int) -> list[dict[str, Any]]:
    """Write the new revision file and return the updated ``plan_revisions`` list.

    Immutable and additive: nothing already recorded is renamed or overwritten. Writing the
    file happens before the caller ever saves a document naming it, so a crash in between
    leaves an orphan file next to an activation that is otherwise exactly as it was --
    never a document pointing at a file that was never written.
    """
    revisions = _revisions_with_backfill(act_dir, existing)
    filename = f"plan.rev{len(revisions)}.md"
    write_private_atomic(act_dir / filename, change.content.decode("utf-8", "surrogateescape"), root=paths.state_root(), errors="surrogateescape")
    revisions.append({"at": now(), "phase": phase, "sha256": change.sha256, "file": filename})
    return revisions


# --------------------------------------------------------------------------
# Materialising the successor
# --------------------------------------------------------------------------


def _copy_activation_tree(src: Path, dst: Path) -> None:
    """Copy the predecessor's activation directory into the successor's, byte for byte.

    ``lock`` and ``state.json`` (plus its in-flight temp names) are excluded: the successor
    gets its own lock the first time it takes one, and its document is derived and written
    separately -- never copied, so the reset fields in it are never confused with a stale copy
    of the predecessor's.
    """
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("lock", "state.json", "state.json.tmp.*"))
    os.chmod(dst, DIR_MODE)
    for root, dirs, files in os.walk(dst):
        for name in dirs:
            os.chmod(os.path.join(root, name), DIR_MODE)
        for name in files:
            os.chmod(os.path.join(root, name), FILE_MODE)


#: Fields reset on every cross-session resume. See the module docstring and AGENTS.md,
#: "the inverted carry-forward rule": this is an allow-list of what to *reset*, not of what to
#: keep, so a field added to ``new_state_document`` later is carried forward by default.
def _build_successor_document(
    *, snapshot: dict[str, Any], identity: _Identity, decision: _Decision, revisions: list[dict[str, Any]]
) -> dict[str, Any]:
    data = copy.deepcopy(snapshot)
    if data.get("status") in ("DISARMED", "STALE"):
        data["status"] = "ACTIVE" if data.get("phases") else "ARMED"
    data.update(
        session_id=identity.session,
        worktree=data.get("worktree") or "",
        armed_at=now(),
        stop_blocks=0,
        stop_marker="",
        defer_pending=False,
        pending_approved_tree="",
        pending_head="",
        pending_command="",
        finish_requested=False,
        # Counters, not evidence: a fresh run starts its retry-pacing and clarify budgets
        # from zero. ``round_history`` is deliberately *not* here -- it is carried forward
        # like the reports, per the inverted carry-forward rule in AGENTS.md.
        transient_failures=0,
        retry_not_before=0,
        clarifications=0,
        resumed_from=identity.prev_session,
        resumed_into="",
        resume_count=int(data.get("resume_count") or 0) + 1,
        activation_generation=int(data.get("activation_generation") or 0) + 1,
        overrides=decision.overrides,
        plan_revisions=revisions,
    )
    if decision.until_given:
        data["stop_after_phase"] = decision.until
    if decision.revision is not None:
        data["plan_path"] = decision.revision.source_path
    if decision.replan:
        # Not in the reset table above: it is not a per-resume default, it is a permission
        # explicitly requested by this call. A resume that did *not* pass --replan leaves
        # whatever the predecessor carried untouched, by the same inverted-allow-list rule.
        data["replan_pending"] = True
    return data


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------


def run(argv: list[str]) -> int:
    session, flag_tokens = _parse(argv)
    repo = commands.current_repo()

    if not session:
        sys.stdout.write(f"{NO_SESSION_MESSAGE}\n")
        return 1

    prev_session = commands.latest_session(repo)
    same_session = bool(prev_session) and session == prev_session

    try:
        flags = _parse_flags(flag_tokens)
    except arm._ArmFailure as exc:
        return _fail(session=session, repo=repo, reason=str(exc), same_session=same_session, retired=False)

    if not prev_session:
        reason = "no activation was ever armed in this worktree. Run /opencode-review-loop:implement <plan.md> to start one."
        return _fail(session=session, repo=repo, reason=reason, same_session=same_session, retired=False)

    prev_state = State(repo, prev_session)
    if not prev_state.load():
        reason = "the previous activation's state could not be read. Run /opencode-review-loop:implement <plan.md>."
        return _fail(session=session, repo=repo, reason=reason, same_session=same_session, retired=False)

    identity = _Identity(repo=repo, session=session, prev_session=prev_session, same_session=same_session)
    try:
        message = _resume(identity=identity, prev_state=prev_state, flags=flags)
    except _ResumeFailure as exc:
        # `_EvidenceCorrupted` additionally escalates the *live* activation -- same-session
        # and pre-retirement cross-session alike -- catching corrupted plan evidence right now
        # rather than leaving it for whichever commit's review happens to run next (`reviewer.
        # build_bundle` verifies the active revision too, but only once a review actually
        # runs). See the exception's own docstring. Once retirement has already happened the
        # predecessor is RESUMED (already blocking), so nothing further is needed there.
        if isinstance(exc, _EvidenceCorrupted) and not exc.retired:
            prev_state.needs_human(str(exc))
        return _fail(session=session, repo=repo, reason=str(exc), same_session=same_session, retired=exc.retired)

    sys.stdout.write(message)
    return 0


def _fail(*, session: str, repo: str, reason: str, same_session: bool, retired: bool) -> int:
    if same_session:
        sys.stdout.write(SAME_SESSION_FAILURE.format(reason=reason))
        return 1
    arm._record_failure(State(repo, session), session=session, repo=repo, reason=reason, publish_latest=retired)
    suffix = WEDGED_SUFFIX if retired else ""
    sys.stdout.write(NEW_SESSION_FAILURE.format(reason=reason) + suffix)
    return 1


def _refuse_unless_resumable(state: State) -> None:
    """Raise ``_ResumeFailure`` unless the stored status is one this build knows may resume.

    Checked against ``_RESUMABLE`` first, deliberately: the four tailored messages below are
    for the statuses that are *expected* to end here, but an allow-list means anything else --
    including a status this build has never heard of -- is refused too, with a generic message
    naming what it actually found.
    """
    stored = state.get("status")
    if stored in _RESUMABLE:
        return
    if stored == "COMPLETE":
        raise _ResumeFailure(
            f"this activation is already COMPLETE ({state.get('reason')}); there is nothing to resume. "
            "Run /opencode-review-loop:implement <plan.md> to start a new one."
        )
    if stored == "ARM_FAILED":
        raise _ResumeFailure(f"arming never completed for this activation ({state.get('reason')}); nothing was frozen to resume.")
    if stored == "NEEDS_HUMAN":
        raise _ResumeFailure(
            f"this activation escalated to NEEDS_HUMAN ({state.get('reason')}); resuming would clear an escalation only a "
            "human may clear. Run /opencode-review-loop:accept to clear it and keep the loop going, or "
            "/opencode-review-loop:stop, then /opencode-review-loop:implement <plan.md> to start over."
        )
    if stored == "RESUMED":
        successor = state.get("resumed_into") or "its successor session"
        raise _ResumeFailure(f"this activation was already retired by a resume; continue in {successor}, or re-arm from scratch.")
    raise _ResumeFailure(
        f'this activation\'s stored status ("{stored}") is not one this build knows how to resume; only ACTIVE, ARMED, '
        "RECONCILE and DISARMED may be resumed. Nothing was written. Re-arm with /opencode-review-loop:implement <plan.md>, "
        "or leave the mode with /opencode-review-loop:stop."
    )


def _resume(*, identity: _Identity, prev_state: State, flags: _Flags) -> str:
    repo = identity.repo
    _refuse_unless_resumable(prev_state)

    overrides = dict(prev_state.data.get("overrides") or {})
    for key, value in (("model", flags.model), ("variant", flags.variant)):
        if value is not None:
            overrides[key] = value
    probe_config = config_module.load(repo, overrides=overrides)
    try:
        arm._check_reviewer(probe_config)
    except arm._ArmFailure as exc:
        raise _ResumeFailure(str(exc)) from exc

    activation_commit = prev_state.get("activation_commit")
    if activation_commit and not gitsnap.is_ancestor(repo, activation_commit, "HEAD"):
        raise _ResumeFailure(
            f"the activation commit ({activation_commit}) is no longer an ancestor of HEAD; history was rewritten and the "
            "frozen baseline no longer describes this repository. Re-arm with /opencode-review-loop:implement <plan.md>."
        )

    # Checked before the dirty-worktree gate below, deliberately: an approval is pending
    # precisely because its commit has not landed yet, so the content pretool already
    # snapshotted and approved is still sitting uncommitted in the worktree. Reporting "the
    # worktree is dirty" first would name a symptom of the real, more specific and more
    # actionable blocker.
    pending_tree = prev_state.get("pending_approved_tree")
    if pending_tree and not flags.abandon_pending:
        raise _ResumeFailure(
            f"a commit review is still pending (approved tree {pending_tree}, on top of {prev_state.get('pending_head') or '<none>'}). "
            "Let it land or fail first -- confirm-commit or posttool-failure clears it -- or pass --abandon-pending if that session is gone."
        )

    revision, revision_warning = _decide_revision(prev_state, explicit_plan=flags.plan)

    # `--replan` grants permission to redefine the phases from the current one onward; there
    # is nothing to redefine until a first `set-phases` has run, and granting the token on an
    # unfrozen (`ARMED`) activation would leave it lying around for whatever `set-phases`
    # freezes the list -- see `phases.py` and AGENTS.md, "the replan fence".
    if flags.replan and not prev_state.get_array("phases"):
        raise _ResumeFailure("the phase list is not frozen yet, so there is nothing to replan. Run set-phases normally instead of --replan.")

    # Any decided revision, and --replan, require a clean worktree -- and neither is waived by
    # --allow-dirty or allow_dirty in config. The condition is "a revision was decided, or
    # --replan was passed", not "--plan or --replan was typed": an automatic revision (the
    # recorded plan_path changed on disk, no --plan given) is exactly as much a "clean
    # boundary" promise as an explicit one.
    allow_dirty = flags.allow_dirty or probe_config.as_bool("allow_dirty")
    if revision is not None or flags.replan:
        if not gitsnap.worktree_clean(repo):
            raise _ResumeFailure(
                "a plan revision was decided, or --replan was passed, both of which require a clean worktree regardless "
                f"of --allow-dirty or allow_dirty in config -- commit the current phase first:\n{gitsnap.dirty_summary(repo)}"
            )
    elif not allow_dirty and not gitsnap.worktree_clean(repo):
        raise _ResumeFailure(
            f"the worktree is dirty. Either commit or stash the existing changes, or re-run with --allow-dirty to fold "
            f"them into the next phase's review:\n{gitsnap.dirty_summary(repo)}"
        )

    until = prev_state.get_int("stop_after_phase")
    until_given = bool(flags.until)
    warnings = ""
    if until_given:
        until = arm._resolve_until(flags.until)
        total = prev_state.phase_count()
        if total and until > total:
            warnings += f"\nNote: --until {until} is beyond the {total} frozen phases; clamped to {total}.\n"
            until = total

    # The unapproved-HEAD warning is deliberately *not* computed here: for a cross-session
    # resume this runs before retirement, and the predecessor stays live through the whole
    # materialisation window that follows -- an already-authorised tool call or a background
    # writer can still move HEAD in that window. `_banner` computes it fresh, immediately
    # before it is shown, against whatever finally got published.
    decision = _Decision(
        overrides=overrides,
        revision=revision,
        revision_warning=revision_warning,
        until=until,
        until_given=until_given,
        warnings=warnings,
        allow_dirty=allow_dirty,
        replan=flags.replan,
    )
    if identity.same_session:
        return _resume_same_session(state=prev_state, identity=identity, flags=flags, decision=decision)
    return _resume_cross_session(prev_state=prev_state, identity=identity, flags=flags, decision=decision)


def _apply_revision_and_replan(state: State, *, repo: str, flags: _Flags, decision: _Decision) -> tuple[_RevisionChange | None, str]:
    """Decide and publish a plan revision, and/or a granted replan token, same-session.

    **The revision is decided again here**, against the document this transaction just
    reloaded -- ``decision.revision``, decided in ``_resume`` before the lock was taken, is
    deliberately not trusted for the write. Two concurrent same-session resumes can both call
    ``_decide_revision`` outside the lock, both see the same predecessor (say revision 0) and
    both decide "changed": if the first one's write is trusted, the second -- now holding the
    lock and reloading a document that already carries the first's revision 1 -- would append
    a second, duplicate revision recording the identical change again, inflating
    ``plan_revisions``, the bundle attachments and the reviewer's disclosure with a change
    that never happened a second time. Recomputing here, against the reloaded document,
    answers "changed" correctly for whichever of the two calls runs second: by the time it
    looks, the active revision already reflects the first call's write.

    Raises ``commands.Refused``, or lets ``_decide_revision``'s own ``_ResumeFailure`` (or its
    ``_EvidenceCorrupted`` subclass) propagate directly -- both are still raised from *inside*
    the caller's transaction, so a refusal aborts the whole resume and nothing is written, and
    ``run``'s existing handling of ``_EvidenceCorrupted`` still applies unchanged. Split out
    only to keep ``_resume_same_session``'s branch count readable.

    Returns the (re-)decided revision and its warning, so the caller reports what actually
    happened -- which may differ from what ``_resume`` decided before the lock -- rather than
    a decision this call may have just superseded.
    """
    revision, revision_warning = _decide_revision(state, explicit_plan=flags.plan)
    if (revision is not None or decision.replan) and not gitsnap.worktree_clean(repo):
        what = "the revision" if revision is not None else "--replan"
        raise commands.Refused(
            f"the worktree became dirty before {what} could be published; commit the current phase "
            f"first and resume again:\n{gitsnap.dirty_summary(repo)}\n"
        )
    if revision is not None:
        revisions = _apply_revision(
            act_dir=state.act_dir, existing=state.data.get("plan_revisions") or [], change=revision, phase=state.get_int("phase")
        )
        state.update(plan_revisions=revisions, plan_path=revision.source_path)
    else:
        state.update(plan_revisions=_revisions_with_backfill(state.act_dir, state.data.get("plan_revisions") or []))
    if decision.replan:
        # Re-checked against the reloaded document: the phase list could have been cleared (a
        # re-arm cannot happen mid-transaction, but belt and braces costs nothing here) since
        # `_resume`'s own pre-check.
        if not state.get_array("phases"):
            raise commands.Refused("the phase list is not frozen yet, so there is nothing to replan. Run set-phases normally instead.\n")
        state.update(replan_pending=True)
    return revision, revision_warning


def _resume_same_session(*, state: State, identity: _Identity, flags: _Flags, decision: _Decision) -> str:
    repo = identity.repo
    head_warning = ""
    revision: _RevisionChange | None = None
    revision_warning = ""
    try:
        with state.transaction():
            # Re-checked against the reloaded document -- a concurrent escalation or `deactivate`
            # may have moved it since `_resume`'s own check, before this took the lock.
            _refuse_unless_resumable(state)
            # `armed_at` is refreshed on every resume, cross-session included, and that alone
            # is what un-stales a TTL-expired activation: `effective_status` derives STALE from
            # it, so nothing else needs to change for a stale activation to resume. `DISARMED`
            # is the one stored status that needs an explicit transform, exactly as the
            # cross-session path's `_build_successor_document` already does -- without it, a
            # same-session `deactivate` then `resume` prints a RESUMED banner while the stored
            # status stays DISARMED, and pretool passes every mutation through unenforced.
            if state.get("status") == "DISARMED":
                state.update(status="ACTIVE" if state.get_array("phases") else "ARMED")
            state.update(armed_at=now())
            pending = state.get("pending_approved_tree")
            if pending:
                if not flags.abandon_pending:
                    raise commands.Refused(
                        f"a commit review is still pending (approved tree {pending}); let it land or fail first, or pass --abandon-pending.\n"
                    )
                state.update(
                    abandoned_pending_tree=pending,
                    abandoned_pending_head=state.get("pending_head"),
                    pending_approved_tree="",
                    pending_head="",
                    pending_command="",
                )
            revision, revision_warning = _apply_revision_and_replan(state, repo=repo, flags=flags, decision=decision)
            if decision.until_given:
                state.update(stop_after_phase=decision.until)
            state.update(overrides=decision.overrides, activation_generation=state.get_int("activation_generation") + 1)
            # Convergence counters are per-run, exactly as in `_build_successor_document`: a
            # resume is a fresh start, so an inherited retry backoff (`retry_not_before` is a
            # future timestamp) or an exhausted clarification budget must not carry over.
            # `round_history` is evidence and is left untouched.
            state.update(transient_failures=0, retry_not_before=0, clarifications=0)

            # Verified with the *checked* read, inside the transaction, so a genuine git
            # failure aborts the whole resume rather than silently skipping the warning: the
            # `with` block only saves if it completes normally, so raising here leaves nothing
            # written -- consistent with "a same-session failure writes nothing at all".
            try:
                head_tree = gitsnap.head_tree_checked(repo)
            except gitsnap.GitUnavailable as exc:
                raise commands.Refused(f"the repository could not be read to verify HEAD after resuming ({exc}). Nothing was written.\n") from exc
            if head_tree and not state.tree_approved(head_tree):
                head_warning = (
                    f"\nWARNING: HEAD's tree ({head_tree}) was never approved by a review. Those commits are folded "
                    "into the next phase's review, or -- if no phase is left -- into the unreviewed-work sweep at "
                    "the next turn's end. last_approved_tree is left untouched, which is what keeps them in scope "
                    "for either.\n"
                )
    except commands.Refused as exc:
        raise _ResumeFailure(str(exc)) from exc
    except StateLoadError as exc:
        raise _ResumeFailure(f"the live activation could not be re-read ({exc}).") from exc

    # The revision reported here is the one just (re-)decided inside the lock, not the one
    # `_resume` decided before it -- see `_apply_revision_and_replan`'s docstring for why the
    # two can legitimately differ under a concurrent same-session resume. `decision.warnings`
    # (the --until clamp note, if any) is carried through untouched -- only the revision part
    # is replaced, and `head_warning` is appended to the "other" bucket, same as cross-session.
    fresh = replace(decision, revision=revision, revision_warning=revision_warning, warnings=decision.warnings + head_warning)
    return _banner(state=state, identity=identity, decision=fresh)


def _resume_cross_session(*, prev_state: State, identity: _Identity, flags: _Flags, decision: _Decision) -> str:
    repo, session = identity.repo, identity.session
    snapshot: dict[str, Any] = {}
    #: Recomputed inside `_retire`'s transaction, replacing `decision.revision` for
    #: everything downstream -- see `_retire`'s own comment for why.
    fresh_revision: _RevisionChange | None = None
    fresh_revision_warning = ""

    def _retire() -> None:
        nonlocal snapshot, fresh_revision, fresh_revision_warning
        _refuse_unless_resumable(prev_state)
        if decision.replan and not prev_state.get_array("phases"):
            raise commands.Refused("the phase list is not frozen yet, so there is nothing to replan. Run set-phases normally instead of --replan.\n")
        # Decided again here, against the document this transaction just reloaded -- exactly
        # the same reasoning `_apply_revision_and_replan` documents for the same-session path,
        # and it applies here too: a same-session resume against this *same* predecessor can
        # publish a revision while this call is still queued for the lock (retirement does not
        # start until this function runs), and `decision.revision`, decided before the lock,
        # would then be stale. Trusting it would have `_publish_successor` append a second,
        # duplicate revision for a change the reloaded document already carries.
        fresh_revision, fresh_revision_warning = _decide_revision(prev_state, explicit_plan=flags.plan)
        # Enforced here, before retirement, not only in `_publish_successor`'s later recheck:
        # the pre-lock check in `_resume` ran against the *old* decision (no revision, say,
        # with `allow_dirty` in play), so it can pass while this fresh one needs a clean
        # worktree. Refusing now -- nothing retired yet -- is strictly better than retiring
        # anyway and finding out only in `_publish_successor`, which would wedge the
        # predecessor as RESUMED and the successor as ARM_FAILED over a check this call could
        # have made before committing to either.
        if (fresh_revision is not None or decision.replan) and not gitsnap.worktree_clean(repo):
            what = "the revision" if fresh_revision is not None else "--replan"
            raise commands.Refused(
                f"the worktree became dirty before {what} could be published; commit the current phase "
                f"first and resume again:\n{gitsnap.dirty_summary(repo)}\n"
            )
        pending = prev_state.get("pending_approved_tree")
        if pending:
            if not flags.abandon_pending:
                raise commands.Refused(
                    f"a commit review is still pending (approved tree {pending}); let it land or fail first, or pass --abandon-pending.\n"
                )
            prev_state.update(
                abandoned_pending_tree=pending,
                abandoned_pending_head=prev_state.get("pending_head"),
                pending_approved_tree="",
                pending_head="",
                pending_command="",
            )
        # The pre-retirement snapshot: everything the successor is built from. Taken *after*
        # the abandon-pending mutation above, so the marker travels with it, and *before* the
        # retirement note below, so the successor never inherits RESUMED or its reason.
        snapshot = copy.deepcopy(prev_state.data)
        prev_state.update(status="RESUMED", resumed_into=session, reason=f"retired by resume into session {session}")

    try:
        with prev_state.transaction():
            _retire()
    except commands.Refused as exc:
        raise _ResumeFailure(str(exc), retired=False) from exc
    except StateLoadError as exc:
        raise _ResumeFailure(f"the previous activation's state could not be re-read ({exc}). Run /opencode-review-loop:implement <plan.md>.") from exc
    # `_decide_revision`'s own `_ResumeFailure` (or `_EvidenceCorrupted`) propagates through
    # here uncaught, deliberately: it is raised before the retirement write above, so
    # `retired=False` (its default) is already correct, and `run`'s existing handling of
    # `_EvidenceCorrupted` applies to the still-live predecessor unchanged.

    # `decision.revision`, decided before the lock, is not used again from here on -- only the
    # fresh one `_retire` just decided against the document it actually reloaded.
    decision = replace(decision, revision=fresh_revision, revision_warning=fresh_revision_warning)

    # The predecessor is retired from here on: any further failure records ARM_FAILED on the
    # successor rather than trying to undo the retirement (module docstring, "No automatic
    # rollback").
    try:
        successor_state, head_warning = _publish_successor(prev_state=prev_state, snapshot=snapshot, identity=identity, decision=decision)
    except _ResumeFailure as exc:
        # Every failure past this point happens after the predecessor was already retired
        # (module docstring, "No automatic rollback"): force `retired=True` uniformly here
        # rather than trust each raise site above to set it, so a future one that forgets
        # cannot silently leave `latest` pointing at a predecessor that no longer accepts
        # anything.
        raise _ResumeFailure(str(exc), retired=True) from exc
    except OSError as exc:
        raise _ResumeFailure(f"the successor activation could not be materialised ({exc}).", retired=True) from exc

    pointer_write(session, repo)
    commands.write_latest(repo, session)

    return _banner(state=successor_state, identity=identity, decision=replace(decision, warnings=decision.warnings + head_warning))


def _publish_successor(*, prev_state: State, snapshot: dict[str, Any], identity: _Identity, decision: _Decision) -> tuple[State, str]:
    """Materialise the successor's directory and document, then publish it. Never rolls back.

    Returns the saved ``State`` and the unapproved-HEAD warning, if any. Every failure here
    is reported by the caller as happening after retirement -- see its own comment.
    """
    repo, session, revision = identity.repo, identity.session, decision.revision

    new_dir = paths.activation_dir(repo, session)
    ensure_private_dir(new_dir, root=paths.state_root())
    _copy_activation_tree(prev_state.act_dir, new_dir)

    revisions = list(snapshot.get("plan_revisions") or [])
    if revision is not None:
        revisions = _apply_revision(act_dir=new_dir, existing=revisions, change=revision, phase=int(snapshot.get("phase") or 1))
    else:
        revisions = _revisions_with_backfill(new_dir, revisions)

    new_data = _build_successor_document(snapshot=snapshot, identity=identity, decision=decision, revisions=revisions)

    # Re-run the git-facing checks immediately before publication. The predecessor was live
    # for the whole window since they were first checked above: a tool call it had already
    # authorised, or a background writer, can still touch the worktree in between. This
    # applies the *same* effective dirty policy as the earlier check (`decision.allow_dirty`,
    # captured once in `_resume` -- not re-read from the repo's own
    # `.opencode-review-loop.json` here, which is attacker-controlled input and reloading it
    # would let this exact window flip the policy a plain resume was already refused under),
    # not only the unconditional one a decided revision imposes: an ordinary resume with no
    # --allow-dirty and no plan change is just as much a "clean boundary" promise, and a dirty
    # worktree slipping in during this exact window must not be published over.
    if not gitsnap.worktree_clean(repo):
        if revision is not None or decision.replan:
            what = "the revised plan" if revision is not None else "--replan"
            raise _ResumeFailure(
                f"the worktree became dirty while this resume was publishing {what}; commit the current "
                f"phase first and resume again:\n{gitsnap.dirty_summary(repo)}"
            )
        if not decision.allow_dirty:
            raise _ResumeFailure(
                "the worktree became dirty while this resume was publishing; without --allow-dirty it must stay "
                f"clean through publication. Commit the current phase first and resume again:\n{gitsnap.dirty_summary(repo)}"
            )
    activation_commit = str(snapshot.get("activation_commit") or "")
    if activation_commit and not gitsnap.is_ancestor(repo, activation_commit, "HEAD"):
        raise _ResumeFailure(
            f"history was rewritten while this resume was publishing (the activation commit {activation_commit} is no "
            "longer an ancestor of HEAD). Re-arm with /opencode-review-loop:implement <plan.md>."
        )

    # The checked read, not the lenient one: a repository that becomes unreadable exactly here
    # must not be indistinguishable from "empty repository, nothing to warn about" -- that
    # would publish a successor claiming everything is fine when whether HEAD was even
    # reviewed could not be verified.
    try:
        head_tree = gitsnap.head_tree_checked(repo)
    except gitsnap.GitUnavailable as exc:
        raise _ResumeFailure(f"the repository could not be read to verify HEAD before publication ({exc}).") from exc
    head_warning = ""
    if head_tree and head_tree not in (new_data.get("approved_trees") or []):
        head_warning = (
            f"\nWARNING: HEAD's tree ({head_tree}) was never approved by a review. Those commits are folded into "
            "the next phase's review, or -- if no phase is left -- into the unreviewed-work sweep at the next "
            "turn's end. last_approved_tree is left untouched, which is what keeps them in scope for either.\n"
        )

    successor_state = State(repo, session)
    successor_state.data = new_data
    successor_state.save()
    return successor_state, head_warning


def _banner(*, state: State, identity: _Identity, decision: _Decision) -> str:
    repo, session, prev_session, same_session = identity.repo, identity.session, identity.prev_session, identity.same_session
    revision = decision.revision
    warnings = decision.revision_warning + decision.warnings
    config = config_module.load(repo, overrides=state.data.get("overrides"))
    variant = config.as_str("variant")
    reviewer = f"{config.as_str('model')}{f' (variant {variant})' if variant else ''}"
    total = state.phase_count()
    phase = state.get_int("phase")
    target = state.get_int("stop_after_phase")
    pause_target = f"{target} of {total}" if target else "none"
    plan_revisions_list = state.data.get("plan_revisions") or []
    revision_count = len(plan_revisions_list) or 1
    # By the time this banner is printed, whichever path got here has already verified the
    # active revision in full (`_decide_revision` / `_apply_revision_and_replan` /
    # `_publish_successor`, all through `planrev.verified_revisions`), so this should never
    # raise in practice -- but the banner is display only, and a resume that already succeeded
    # and wrote state must not crash reporting so over a defensive check.
    try:
        active_plan_file = planrev.active_filename(plan_revisions_list)
    except planrev.EvidenceCorrupted as exc:
        active_plan_file = f"<corrupted: {exc}>"
    # The reviewer reads whichever revision is active (`reviewer._plan_excerpt`, via
    # `planrev.verified_revisions`) and discloses every earlier one to it too -- so this is
    # simply which file on disk is now the one to implement against, not a caveat about what
    # is or is not enforced.
    revision_note = (
        f"\nNote: revision {revision_count - 1} ({active_plan_file}) is the plan the reviewer evaluates against from here on.\n"
        if len(plan_revisions_list) > 1
        else ""
    )

    # The unapproved-HEAD warning is not computed here: both callers already fold it into
    # `decision.warnings` themselves, using `gitsnap.head_tree_checked` at the point each
    # actually publishes -- a lenient re-read here, after the fact, could silently drop it if
    # git happened to be unreadable at render time (see `_resume_same_session` and
    # `_resume_cross_session`).

    if state.get("status") == "ARMED":
        next_steps = (
            "Phases are not frozen yet. Read the frozen plan named above and run:\n\n"
            f'    {commands.plugin_root()}/scripts/ocrl.sh set-phases --phase "…" --phase "…"\n'
        )
    elif state.get("replan_pending") == "true":
        next_steps = (
            f"--replan was granted: redefine phases {phase}..{total or '?'} only (phase {phase - 1} and earlier are "
            "immutable and stay as they are). Read the frozen plan named above and run:\n\n"
            f'    {commands.plugin_root()}/scripts/ocrl.sh set-phases --phase "…" --phase "…"\n\n'
            f"one --phase per phase, from {phase} onward. Every other mutation is denied until that command has run."
        )
    else:
        next_steps = f"Continue with phase {phase} of {total or '?'}:\n\n    {state.phase_desc(phase)}\n"

    resumed_from_line = "" if same_session else f"- resumed from session: {prev_session}\n"
    return f"""\
**opencode-review-loop is RESUMED for this worktree.**

- repository: {repo}
- session: {session}
{resumed_from_line}- plan revision: {revision_count} ({"changed just now" if revision is not None else "unchanged"})
{revision_note}- frozen plan: {state.act_dir}/{active_plan_file}
- baseline tree: {state.get("baseline_tree")}
- activation commit: {state.get("activation_commit") or "<empty repository>"}
- phase: {phase} of {total or "?"}
- pause target: {pause_target}
- reviewer: {reviewer}
{warnings}
{next_steps}"""
