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
``/adversarial-review-loop:implement <plan.md>``.
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

from arl import commands, gitsnap, guide, harness, paths, planrev
from arl import config as config_module
from arl.atomic import DIR_MODE, FILE_MODE, ensure_private_dir, write_private_atomic
from arl.commands import arm
from arl.errors import StateLoadError
from arl.state import State, pointer_read, pointer_write
from arl.util import now

__all__ = ["run"]

_BOOL_FLAGS: Final = ("--allow-dirty", "--abandon-pending", "--replan")
_VALUE_FLAGS: Final = ("--until", "--plan", "--harness", "--model", "--variant", "--guide")

#: The only stored statuses resume may continue from -- deliberately an allow-list, not a
#: deny-list of terminal ones. A deny-list fails open the moment a new status is added and
#: this function is not updated for it, or when ``state.json`` -- which AGENTS.md is explicit
#: is not a trust boundary -- carries a value nothing here ever wrote. ``STALE`` is not
#: listed because it is never a *stored* value; it is derived from ``armed_at`` via the TTL,
#: and every one of these four may legitimately be effectively stale and still resume --
#: ``armed_at`` is refreshed on every resume, which is what un-stales it.
_RESUMABLE: Final = frozenset({"ACTIVE", "ARMED", "RECONCILE", "DISARMED"})

NO_SESSION_MESSAGE: Final = (
    "**adversarial-review-loop: RESUME FAILED** -- no session id was supplied, so no state could be recorded. "
    "The review loop is NOT active in this session."
)

SAME_SESSION_FAILURE: Final = """\
**adversarial-review-loop: RESUME FAILED -- the live activation was left untouched.**

Reason: {reason}

Nothing was written: this session's activation is exactly as it was before this command ran. \
Fix the cause and run /adversarial-review-loop:resume again.
"""

NEW_SESSION_FAILURE: Final = """\
**adversarial-review-loop: RESUME FAILED -- the review loop is NOT active in this session.**

Reason: {reason}

Every file mutation and every commit in this worktree is denied until this is resolved.
"""

WEDGED_SUFFIX: Final = """

This worktree's predecessor activation was already retired before this failure, so it is now \
wedged: neither the old session nor this one is a live, working activation. \
Re-arm with /adversarial-review-loop:implement <plan.md>.
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
    #: Raw ``--until`` text, resolved through ``arm.resolve_until``.
    until: str = ""
    #: ``None`` means "not given"; only an explicit ``--plan`` triggers a forced re-read.
    plan: str | None = None
    abandon_pending: bool = False
    #: ``None`` means "not given": an activation keeps the harness it was armed with across a
    #: resume, and only a flag the user actually typed switches it mid-activation.
    harness: str | None = None
    model: str | None = None
    variant: str | None = None
    #: ``None`` means "not given", and that is the *only* way the guide stays as it is: an
    #: activation keeps the guide it was armed with across a resume, and a repo config edited
    #: to name another one mid-activation changes nothing. Only a flag the user actually typed
    #: freezes a new revision -- the same rule ``harness`` follows, for the same reason.
    guide: str | None = None
    #: Permission to redefine the remaining, not-yet-committed phases. See ``_Decision.replan``.
    replan: bool = False


@dataclass(frozen=True)
class _RevisionChange:
    """A plan revision that was decided but not yet written anywhere."""

    content: bytes
    sha256: str
    source_path: str


@dataclass(frozen=True)
class _GuideChange:
    """A review guide revision that was decided but not yet frozen anywhere.

    The same shape as :class:`_RevisionChange`, and deliberately a separate type: the two are
    decided independently, and a single resume can carry one, both or neither.
    """

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
    repo config it partly comes from (``.adversarial-review-loop.json``) lives *inside* the
    repository under review and is explicitly attacker-controlled input (see ``config.py``).
    Reloading it during the republication recheck would let that file be edited during the
    retirement window to flip the policy a plain resume was already refused under.
    """

    #: The overlay this resume validated: the stored one, plus the flags it was given, with
    #: ``harness`` set to what ``_check_reviewer`` actually probed.
    overrides: dict[str, str]
    #: The stored overlay :attr:`overrides` was merged from, kept for the compare-and-swap in
    #: :func:`_refuse_if_the_overlay_moved`. Writing :attr:`overrides` is only sound while the
    #: document still carries this; otherwise the pair that would land is one nothing probed.
    stored_overrides: dict[str, str]
    revision: _RevisionChange | None
    #: The guide path ``--guide`` resolved to, or ``None`` when the flag was not given -- the
    #: only input to the under-lock re-decision, which re-reads that path rather than trusting
    #: :attr:`guide` (decided before the lock) for the write.
    guide_source: str | None
    #: The guide revision this resume decided, or ``None`` for "the guide is unchanged".
    #: Replaced by the under-lock re-decision before it is reported, exactly as
    #: :attr:`revision` is.
    guide: _GuideChange | None
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
        usage="--until, --plan, --guide, --allow-dirty, --abandon-pending, --replan, --harness, --model, --variant",
    )
    return _Flags(
        allow_dirty=arm.flag_bool(raw, "--allow-dirty"),
        until=arm.flag_str(raw, "--until") or "",
        plan=arm.flag_str(raw, "--plan"),
        abandon_pending=arm.flag_bool(raw, "--abandon-pending"),
        replan=arm.flag_bool(raw, "--replan"),
        harness=arm.flag_str(raw, "--harness"),
        model=arm.flag_str(raw, "--model"),
        variant=arm.flag_str(raw, "--variant"),
        guide=arm.flag_str(raw, "--guide"),
    )


def _stored_overrides(document: object) -> dict[str, str]:
    """The activation overlay a state document carries, normalised.

    Typed ``object`` because it comes straight out of ``state.json``, which is not a trust
    boundary: anything that is not a mapping of strings is read as "no overlay" rather than
    trusted to be one. Normalising here is also what lets a missing ``overrides``, a ``null``
    one and an empty one compare equal in :func:`_refuse_if_the_overlay_moved`.
    """
    if not isinstance(document, dict):
        return {}
    raw = document.get("overrides")
    if not isinstance(raw, dict):
        return {}
    return {key: str(value) for key, value in raw.items() if isinstance(key, str)}


def _merged_overrides(stored: dict[str, str], flags: _Flags) -> dict[str, str]:
    """The activation overlay this resume would leave behind: what is stored, plus what was typed.

    Only the keys actually given: an activation keeps the harness, model and variant it was
    armed with unless this call names another. The harness this returns is therefore the
    *requested* one; ``_resume`` overwrites it with the one ``_check_reviewer`` really probed,
    for the reason ``arm._arm`` documents at length.

    ``review_guide`` is in here so ``guide.resolve`` sees a ``--guide`` the same way ``arm``
    does -- through the ordinary config chain, where ``ARL_REVIEW_GUIDE`` still outranks it --
    and so the overlay keeps naming the guide this activation actually runs under. It has no
    effect on any later round: the guide is read exactly once per ``--guide``, and every
    review afterwards reads only the frozen copy.
    """
    merged = dict(stored)
    for key, value in (("harness", flags.harness), ("model", flags.model), ("variant", flags.variant), ("review_guide", flags.guide)):
        if value is not None:
            merged[key] = value
    return merged


def _refuse_if_the_overlay_moved(current: object, decision: _Decision) -> None:
    """Refuse when the stored overlay changed after this resume probed against it.

    A compare-and-swap, and the only thing that keeps ``_check_reviewer`` meaningful under
    concurrency. Two same-session resumes each probe their *own* pre-lock merge and then both
    write: one switching the harness, one setting a model. Whatever combining rule the writes
    follow, the pair that ends up stored is one neither call ever validated -- a model only the
    old harness reports, now paired with the new one, so every later review fails for an
    operational reason. Composing them under the lock does not fix that; it *is* that.

    So the second writer is refused instead, and says so: the overlay it validated against is
    no longer the one on disk, and the fix is to run the command again, which re-probes the
    combination and either passes or reports exactly why not. Raised as
    ``commands.Refused`` from inside the caller's transaction, so nothing is written and the
    live activation is left exactly as it was -- the same contract every other pre-write
    refusal here has.

    Two identical resumes do not trip this: what is compared is the overlay's *value*, so a
    call that writes back what was already there leaves the next one's base unchanged.
    """
    if _stored_overrides(current) != decision.stored_overrides:
        raise commands.Refused(
            "the activation's model/harness overrides changed while this resume was preparing (a concurrent resume), so the "
            "combination it checked the reviewer against is no longer the one on disk. Nothing was written. Run "
            "/adversarial-review-loop:resume again to check and apply it against the current configuration.\n"
        )


# --------------------------------------------------------------------------
# Plan revision: deciding, then writing
# --------------------------------------------------------------------------


def _revisions_with_backfill(act_dir: Path, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``arl.planrev.revisions_with_backfill``, translated into this module's failure type.

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
# Review guide revision: deciding, then writing
# --------------------------------------------------------------------------


def _verified_guide_revisions(state: State) -> list[dict[str, Any]]:
    """Every recorded guide revision, re-verified against its hash. May be empty.

    Verified on **every** resume, not only one carrying ``--guide``: the frozen guide is
    evidence in exactly the sense the frozen plan is -- what every review to date was run
    against -- so a resume is the right place to catch it having been replaced, rather than
    leaving it for whichever commit's review reaches ``reviewer.build_bundle`` next.

    An empty list is the ordinary "this activation has no guide" answer and never an error;
    there is no revision-0 backfill (``arl.guide.verified_active``), because backfilling would
    invent a guide for an activation that never ran under one. A failure is re-raised as
    :class:`_EvidenceCorrupted`, so it escalates the live activation the same way corrupted
    plan evidence does.

    The **raw** recorded value is validated, never ``get_array_of_dicts``'s normalised view:
    that one answers ``[]`` for a non-list and drops non-object members, and ``[]`` is exactly
    how "no guide" is encoded -- so a malformed field would resume cleanly, get written back
    normalised (destroying the record), and leave every later review running without the guide
    it is supposed to run under. See :func:`arl.guide.validated_revisions`.
    """
    try:
        revisions = guide.validated_revisions(state.data.get("guide_revisions"))
        guide.verified_active(state.act_dir, revisions)
    except planrev.EvidenceCorrupted as exc:
        raise _EvidenceCorrupted(str(exc)) from exc
    return revisions


def _decide_guide(state: State, *, source: str | None) -> _GuideChange | None:
    """Whether a new guide revision is called for. Never writes anything.

    ``source`` is ``None`` unless ``--guide`` was given, and only that flag can change the
    guide: a repo config edited mid-activation to name another one is exactly what freezing
    the guide at arm exists to defeat.

    Every refusal ``arl.guide`` knows -- unreadable, empty, oversized, carrying a contract
    marker -- applies here too, and fails the resume rather than the next review, because a
    guide the gate will not accept must be reported while the user is watching. That is also
    why a guide cannot be *dropped* mid-activation: there is no value for ``--guide`` that
    means "none", so removing bad guidance means abandoning the activation and re-arming.

    A guide whose bytes *and* path both match the active revision decides nothing: a resume
    that renames its argument at the same content still records a revision, because the path
    is what every disclosure names, but re-running the same command twice does not.
    """
    revisions = _verified_guide_revisions(state)
    if source is None:
        return None
    try:
        raw = guide.read_source(source)
    except guide.GuideRejected as exc:
        raise _ResumeFailure(str(exc)) from exc
    digest = hashlib.sha256(raw).hexdigest()
    if revisions and digest == revisions[-1].get("sha256") and source == state.get("guide_path"):
        return None
    return _GuideChange(content=raw, sha256=digest, source_path=source)


def _apply_guide(*, act_dir: Path, existing: list[dict[str, Any]], change: _GuideChange, phase: int) -> list[dict[str, Any]]:
    """Freeze the new guide revision and return the updated ``guide_revisions`` list.

    Immutable and additive, exactly like ``_apply_revision``: nothing already frozen is
    renamed or overwritten, and the file lands before any document names it. The first
    revision an activation ever records is ``guide.frozen.md`` whether ``arm`` or a later
    ``resume --guide`` wrote it -- ``guide.revision_filename`` decides that from the position,
    so an activation armed without a guide and given one later still reads as revision 0.
    """
    try:
        entry = guide.freeze(change.content, act_dir, guide.revision_filename(len(existing)), phase=phase)
    except guide.GuideRejected as exc:
        raise _ResumeFailure(str(exc)) from exc
    return [*existing, entry]


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
    *,
    snapshot: dict[str, Any],
    identity: _Identity,
    decision: _Decision,
    revisions: list[dict[str, Any]],
    guide_revisions: list[dict[str, Any]],
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
        # like the reports, per the inverted carry-forward rule in AGENTS.md. Nor is
        # ``clarify_seq``: it numbers ``context/`` question files, ``context/`` is copied
        # forward byte for byte, and resetting it would have a post-resume clarify overwrite
        # a carried-forward question file -- the same failure a reset ``report_seq`` causes.
        transient_failures=0,
        retry_not_before=0,
        clarifications=0,
        # A claim from the predecessor's `active_review` is meaningless here regardless: a
        # different session, a different process, and the generation bump below already makes
        # `_claim_active_review`'s own generation check ignore it. Reset explicitly anyway,
        # for the same hygiene the counters above get -- carrying forward evidence of a
        # process that no longer exists is not what "carried forward like the reports" means.
        active_review={},
        review_attempts={},
        resumed_from=identity.prev_session,
        resumed_into="",
        resume_count=int(data.get("resume_count") or 0) + 1,
        activation_generation=int(data.get("activation_generation") or 0) + 1,
        # Sound because `_retire` already refused if the predecessor's overlay had moved since
        # this one was probed, so `snapshot` still carries the base it was merged from.
        overrides=decision.overrides,
        plan_revisions=revisions,
        # Carried forward unchanged when this resume decided no new guide -- the successor
        # reviews under exactly the guide its predecessor did.
        guide_revisions=guide_revisions,
    )
    if decision.until_given:
        data["stop_after_phase"] = decision.until
    if decision.revision is not None:
        data["plan_path"] = decision.revision.source_path
    if decision.guide is not None:
        data["guide_path"] = decision.guide.source_path
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
    _ack_intent(session)

    prev_session = commands.latest_session(repo)
    same_session = bool(prev_session) and session == prev_session

    try:
        flags = _parse_flags(flag_tokens)
    except arm._ArmFailure as exc:
        return _fail(session=session, repo=repo, reason=str(exc), same_session=same_session, retired=False)

    if not prev_session:
        reason = "no activation was ever armed in this worktree. Run /adversarial-review-loop:implement <plan.md> to start one."
        return _fail(session=session, repo=repo, reason=reason, same_session=same_session, retired=False)

    prev_state = State(repo, prev_session)
    if not prev_state.load():
        reason = "the previous activation's state could not be read. Run /adversarial-review-loop:implement <plan.md>."
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


def _ack_intent(session: str) -> None:
    """Answer the session's intent marker: the arming command it asked for is now *running*.

    Rule 0's marker guards exactly one thing -- an expansion that never started. Once this
    command is executing it can observe and record its own failures, so the marker's job is
    done, and leaving it unanswered is not conservative: a *successful* same-session resume
    writes no new pointer, the marker would outlive it, and the very next mutation would
    overwrite the live activation with ``ARM_FAILED`` (measured against a real 44-phase run,
    2026-08-30 -- see ``tests/STEP0.md``). The ack is the pointer republished with the
    marker's token (``pointer_write`` reads it itself), which is durable, atomic, and exactly
    the ack every other arming path already produces.

    Only when this session already *has* a pointer: a first arm keeps its marker until its
    own success or failure record writes one, so a hard crash mid-arm still reads as "arming
    never ran" rather than as an unarmed worktree.
    """
    existing = pointer_read(session)
    if existing:
        pointer_write(session, existing)


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
            "Run /adversarial-review-loop:implement <plan.md> to start a new one."
        )
    if stored == "ARM_FAILED":
        raise _ResumeFailure(f"arming never completed for this activation ({state.get('reason')}); nothing was frozen to resume.")
    if stored == "NEEDS_HUMAN":
        raise _ResumeFailure(
            f"this activation escalated to NEEDS_HUMAN ({state.get('reason')}); resuming would clear an escalation only a "
            "human may clear. Run /adversarial-review-loop:accept to clear it and keep the loop going, or "
            "/adversarial-review-loop:stop, then /adversarial-review-loop:implement <plan.md> to start over."
        )
    if stored == "RESUMED":
        successor = state.get("resumed_into") or "its successor session"
        raise _ResumeFailure(f"this activation was already retired by a resume; continue in {successor}, or re-arm from scratch.")
    raise _ResumeFailure(
        f'this activation\'s stored status ("{stored}") is not one this build knows how to resume; only ACTIVE, ARMED, '
        "RECONCILE and DISARMED may be resumed. Nothing was written. Re-arm with /adversarial-review-loop:implement <plan.md>, "
        "or leave the mode with /adversarial-review-loop:stop."
    )


def _resume(*, identity: _Identity, prev_state: State, flags: _Flags) -> str:
    repo = identity.repo
    _refuse_unless_resumable(prev_state)

    # Probed against the overlay this resume *would* write, so the binary checked and the model
    # list probed are the harness's being switched to rather than the one being left. The base
    # it was merged from is carried on the decision: by the time the lock is taken a concurrent
    # resume may have moved it, and `_refuse_if_the_overlay_moved` refuses rather than storing
    # a combination this probe never covered.
    stored_overrides = _stored_overrides(prev_state.data)
    overrides = _merged_overrides(stored_overrides, flags)
    probe_config = config_module.load(repo, overrides=overrides)
    try:
        arm._check_reviewer(probe_config)
    except arm._ArmFailure as exc:
        raise _ResumeFailure(str(exc)) from exc
    # The harness that was *probed*, not the one that was asked for -- `ARL_HARNESS` outranks
    # this overlay, so with the two disagreeing `--harness` never reached the check above.
    # Recording the flag anyway would pin a harness nothing verified, and the activation would
    # start running it the moment the variable left the environment. See `arm._arm`.
    overrides["harness"] = probe_config.as_str("harness")

    activation_commit = prev_state.get("activation_commit")
    if activation_commit and not gitsnap.is_ancestor(repo, activation_commit, "HEAD"):
        raise _ResumeFailure(
            f"the activation commit ({activation_commit}) is no longer an ancestor of HEAD; history was rewritten and the "
            "frozen baseline no longer describes this repository. Re-arm with /adversarial-review-loop:implement <plan.md>."
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

    # Resolved through the same fully-merged config `arm` resolves it through, so a `--guide`
    # behaves identically on both arming paths -- `ARL_REVIEW_GUIDE` included, which outranks
    # the overlay there and here alike. Decided (and therefore read and validated) before
    # anything is written, so a guide the gate will not accept costs nothing: the activation is
    # left exactly as it was found, and the refusal names the file while the user is watching.
    guide_source = guide.resolve(probe_config, repo) if flags.guide is not None else None
    guide_change = _decide_guide(prev_state, source=guide_source)

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
        until = arm.resolve_until(flags.until)
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
        stored_overrides=stored_overrides,
        revision=revision,
        guide_source=guide_source,
        guide=guide_change,
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


def _apply_guide_revision(state: State, *, decision: _Decision) -> _GuideChange | None:
    """Decide and freeze a guide revision against the document the caller just reloaded.

    Decided again here rather than trusting ``decision.guide``, for exactly the reason
    :func:`_apply_revision_and_replan` documents for the plan: two concurrent resumes can both
    decide "changed" outside the lock, and the second one -- reloading a document that already
    carries the first one's revision -- must see it and record nothing, instead of appending a
    duplicate that inflates every disclosure with a change that happened once.

    Also what re-verifies the existing revisions inside the transaction, so a guide that was
    tampered with after ``_resume``'s own check cannot be appended to.

    Called on every same-session resume, guide or no guide: with no ``--guide`` it verifies and
    returns ``None``. What is written back is what ``_decide_guide`` just *validated* -- never
    a normalised view of a malformed field, which is the one way a resume could quietly erase
    the record of a guide (see :func:`_verified_guide_revisions`) -- and never a backfill, so
    "no guide" stays "no guide".
    """
    change = _decide_guide(state, source=decision.guide_source)
    revisions = _verified_guide_revisions(state)
    if change is not None:
        revisions = _apply_guide(act_dir=state.act_dir, existing=revisions, change=change, phase=state.get_int("phase"))
        state.update(guide_path=change.source_path)
    state.update(guide_revisions=revisions)
    return change


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
    guide_change: _GuideChange | None = None
    try:
        with state.transaction():
            # Re-checked against the reloaded document -- a concurrent escalation or `deactivate`
            # may have moved it since `_resume`'s own check, before this took the lock.
            _refuse_unless_resumable(state)
            # Checked against the *reloaded* document, and **before anything is frozen to
            # disk**: a concurrent same-session resume may have switched the harness since this
            # call probed, and storing this overlay on top of that would leave a harness/model
            # pair nothing ever validated. Refusing here rather than after the writes below is
            # what makes "the live activation was left untouched" literally true -- a refusal
            # taken later still leaves the plan and guide revisions this call decided sitting
            # in the activation directory, unreferenced by any document. The cross-session path
            # already refuses in this position, inside `_retire`, for the same reason.
            _refuse_if_the_overlay_moved(state.data, decision)
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
            guide_change = _apply_guide_revision(state, decision=decision)
            if decision.until_given:
                state.update(stop_after_phase=decision.until)
            state.update(overrides=decision.overrides, activation_generation=state.get_int("activation_generation") + 1)
            # Convergence counters are per-run, exactly as in `_build_successor_document`: a
            # resume is a fresh start, so an inherited retry backoff (`retry_not_before` is a
            # future timestamp) or an exhausted clarification budget must not carry over.
            # `round_history` is evidence and is left untouched. `active_review` is reset for
            # the same hygiene, even though the generation bump above already makes
            # `_claim_active_review`'s own generation check ignore any stale claim on its own.
            state.update(transient_failures=0, retry_not_before=0, clarifications=0, active_review={}, review_attempts={})

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
    fresh = replace(decision, revision=revision, guide=guide_change, revision_warning=revision_warning, warnings=decision.warnings + head_warning)
    return _banner(state=state, identity=identity, decision=fresh)


def _resume_cross_session(*, prev_state: State, identity: _Identity, flags: _Flags, decision: _Decision) -> str:
    repo, session = identity.repo, identity.session
    snapshot: dict[str, Any] = {}
    #: Recomputed inside `_retire`'s transaction, replacing `decision.revision` for
    #: everything downstream -- see `_retire`'s own comment for why.
    fresh_revision: _RevisionChange | None = None
    fresh_revision_warning = ""
    #: Recomputed inside `_retire`'s transaction too, and for the same reason.
    fresh_guide: _GuideChange | None = None

    def _retire() -> None:
        nonlocal snapshot, fresh_revision, fresh_revision_warning, fresh_guide
        _refuse_unless_resumable(prev_state)
        # Before the retirement write below, so a refusal here leaves the predecessor live and
        # `retired=False` correct: a same-session resume against this *same* predecessor can
        # move the overlay while this call is queued for the lock, and the successor must not
        # be published with a harness/model pair neither call probed.
        _refuse_if_the_overlay_moved(prev_state.data, decision)
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
        # Decided again here for the same reason, and before the retirement write below, so a
        # refused guide leaves the predecessor live and `retired=False` correct. The frozen
        # copy is written later, into the successor's own directory -- nothing about the
        # predecessor's activation directory changes on this path.
        fresh_guide = _decide_guide(prev_state, source=decision.guide_source)
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
        raise _ResumeFailure(
            f"the previous activation's state could not be re-read ({exc}). Run /adversarial-review-loop:implement <plan.md>."
        ) from exc
    # `_decide_revision`'s own `_ResumeFailure` (or `_EvidenceCorrupted`) propagates through
    # here uncaught, deliberately: it is raised before the retirement write above, so
    # `retired=False` (its default) is already correct, and `run`'s existing handling of
    # `_EvidenceCorrupted` applies to the still-live predecessor unchanged.

    # `decision.revision`, decided before the lock, is not used again from here on -- only the
    # fresh one `_retire` just decided against the document it actually reloaded.
    decision = replace(decision, revision=fresh_revision, guide=fresh_guide, revision_warning=fresh_revision_warning)

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

    # Into the successor's directory, which already carries the predecessor's frozen guides
    # byte for byte (`_copy_activation_tree`) -- so the revision index the new file is named
    # from counts the copies that are actually there. No backfill: an activation with no guide
    # keeps an empty list, and one given its first guide here records it as revision 0.
    #
    # Validated rather than filtered: dropping a malformed member here would hand the successor
    # a shorter history than the predecessor was reviewed under. `_retire` already validated
    # this exact document under the lock, so this cannot fire -- but the alternative if it ever
    # did is silent evidence loss, which is the one outcome this field must never have.
    try:
        guide_revisions = guide.validated_revisions(snapshot.get("guide_revisions"))
    except planrev.EvidenceCorrupted as exc:
        raise _ResumeFailure(str(exc)) from exc
    if decision.guide is not None:
        guide_revisions = _apply_guide(act_dir=new_dir, existing=guide_revisions, change=decision.guide, phase=int(snapshot.get("phase") or 1))

    new_data = _build_successor_document(
        snapshot=snapshot, identity=identity, decision=decision, revisions=revisions, guide_revisions=guide_revisions
    )

    # Re-run the git-facing checks immediately before publication. The predecessor was live
    # for the whole window since they were first checked above: a tool call it had already
    # authorised, or a background writer, can still touch the worktree in between. This
    # applies the *same* effective dirty policy as the earlier check (`decision.allow_dirty`,
    # captured once in `_resume` -- not re-read from the repo's own
    # `.adversarial-review-loop.json` here, which is attacker-controlled input and reloading it
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
            "longer an ancestor of HEAD). Re-arm with /adversarial-review-loop:implement <plan.md>."
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
    # `arm._check_reviewer` has already accepted the harness on every path that reaches this
    # banner, so `harness.model` cannot raise here.
    reviewer = f"{config.as_str('harness')} {harness.model(config)}{f' (variant {variant})' if variant else ''}"
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

    # Named here for the reason `arm._armed_message` names it: the guide is the one input the
    # repository supplies that becomes *instruction* to the reviewer, a bad one makes reviews
    # worse, and that is disclosed rather than prevented. A resume needs it doubly -- `--guide`
    # can have just replaced it, and a resume is where a human finds out that the phases from
    # here on are reviewed under different guidance than the ones behind them. The path goes
    # through `guide.display_path` because `review_guide` is repository-controlled and this
    # banner is both printed to a terminal and read by the model as what to do next.
    guide_revisions = state.get_array_of_dicts("guide_revisions")
    if not guide_revisions:
        guide_line = "none"
    else:
        guide_index = len(guide_revisions) - 1
        guide_digest = str(guide_revisions[-1].get("sha256") or "")
        guide_source = guide.display_path(state.get("guide_path")) if state.get("guide_path") else "<unrecorded>"
        guide_line = (
            f"{guide_source} (frozen copy: {state.act_dir}/{guide.revision_filename(guide_index)}, "
            f"sha256 {guide_digest[:12] or '<unrecorded>'}, revision {guide_index}"
            f"{', changed just now' if decision.guide is not None else ''})"
        )

    # The unapproved-HEAD warning is not computed here: both callers already fold it into
    # `decision.warnings` themselves, using `gitsnap.head_tree_checked` at the point each
    # actually publishes -- a lenient re-read here, after the fact, could silently drop it if
    # git happened to be unreadable at render time (see `_resume_same_session` and
    # `_resume_cross_session`).

    if state.get("status") == "ARMED":
        next_steps = (
            "Phases are not frozen yet. Read the frozen plan named above and run:\n\n"
            f'    {commands.plugin_root()}/scripts/arl.sh set-phases --phase "…" --phase "…"\n'
        )
    elif state.get("replan_pending") == "true":
        next_steps = (
            f"--replan was granted: redefine phases {phase}..{total or '?'} only (phase {phase - 1} and earlier are "
            "immutable and stay as they are). Read the frozen plan named above and run:\n\n"
            f'    {commands.plugin_root()}/scripts/arl.sh set-phases --phase "…" --phase "…"\n\n'
            f"one --phase per phase, from {phase} onward. Every other mutation is denied until that command has run."
        )
    else:
        next_steps = f"Continue with phase {phase} of {total or '?'}:\n\n    {state.phase_desc(phase)}\n"

    resumed_from_line = "" if same_session else f"- resumed from session: {prev_session}\n"
    return f"""\
**adversarial-review-loop is RESUMED for this worktree.**

- repository: {repo}
- session: {session}
{resumed_from_line}- plan revision: {revision_count} ({"changed just now" if revision is not None else "unchanged"})
{revision_note}- frozen plan: {state.act_dir}/{active_plan_file}
- baseline tree: {state.get("baseline_tree")}
- activation commit: {state.get("activation_commit") or "<empty repository>"}
- phase: {phase} of {total or "?"}
- pause target: {pause_target}
- reviewer: {reviewer}
- review guide: {guide_line}
{warnings}
{next_steps}"""
