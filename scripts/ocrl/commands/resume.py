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
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from ocrl import commands, gitsnap, paths
from ocrl import config as config_module
from ocrl.atomic import DIR_MODE, FILE_MODE, ensure_private_dir, write_private_atomic
from ocrl.commands import arm
from ocrl.errors import StateLoadError
from ocrl.state import State, pointer_write
from ocrl.util import now

__all__ = ["run"]

#: The name ``arm`` freezes the plan under, and the name revision 0 always carries -- kept in
#: sync with ``ocrl.state._PLAN_FROZEN_NAME`` by convention rather than import, since that name
#: is private to ``state.py`` and this is the one other place that needs to know it.
_PLAN_FROZEN_NAME: Final = "plan.frozen.md"

#: What a real ``hashlib.sha256(...).hexdigest()`` looks like -- lowercase, exactly 64 hex
#: digits. Used to refuse a recorded revision whose ``sha256`` field is missing or malformed
#: before treating it as "nothing to compare", which a missing value would otherwise look like.
_SHA256_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")

_BOOL_FLAGS: Final = ("--allow-dirty", "--abandon-pending")
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
    ``ACTIVE``, and the *ordinary* commit path (``pretool._gate_commit`` -> ``reviewer.
    _plan_excerpt``) never verifies ``plan.frozen.md``'s integrity itself -- it would go on
    consuming whatever is there. So ``run`` escalates the live activation to ``NEEDS_HUMAN``
    whenever this is raised and retirement has not already happened (see ``retired``): a
    blocking status the ordinary commit path *does* already check, closing the gap without
    duplicating this verification into the hot path.
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
    until: int
    until_given: bool
    warnings: str
    allow_dirty: bool


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
        usage="--until, --plan, --allow-dirty, --abandon-pending, --model, --variant",
    )
    return _Flags(
        allow_dirty=arm.flag_bool(raw, "--allow-dirty"),
        until=arm.flag_str(raw, "--until") or "",
        plan=arm.flag_str(raw, "--plan"),
        abandon_pending=arm.flag_bool(raw, "--abandon-pending"),
        model=arm.flag_str(raw, "--model"),
        variant=arm.flag_str(raw, "--variant"),
    )


# --------------------------------------------------------------------------
# Plan revision: deciding, then writing
# --------------------------------------------------------------------------


def _read_verified(act_dir: Path, filename: str, expected_sha256: str | None) -> bytes:
    """The bytes of ``filename`` inside ``act_dir``, checked before they are trusted.

    ``filename`` is untrusted input the moment it comes out of ``plan_revisions`` in
    ``state.json`` -- AGENTS.md is explicit that file is not a trust boundary -- so it is
    validated as one safe path component, then required to be a *literal* regular file:
    ``lstat`` (which does not follow symlinks) must report ``S_ISREG`` for ``act_dir /
    filename`` itself. A ``realpath``-then-containment check, the shape ``pretool`` uses for
    the state root, is not enough here and is deliberately not used: it only rules out a
    symlink that *escapes* the directory, and would happily pass one planted at
    ``plan.frozen.md`` pointing at another file that legitimately lives inside the same
    activation directory (a later revision, say) -- silently substituting its content while
    still resolving "inside the directory". Requiring the component itself to carry no
    indirection at all is the same discipline ``atomic.py``'s ``O_NOFOLLOW``-based directory
    walk already applies to the rest of the state tree. When ``expected_sha256`` is given, the
    content must still match it -- this is what makes a recorded revision actually *frozen*,
    rather than a filename pointing at whatever bytes happen to be there now. ``None`` is
    reserved for synthesizing a brand-new revision 0 from scratch, where there is nothing yet
    to compare against -- every already-recorded entry must supply a real hash, checked by the
    caller before this is reached, since ``None`` reaching here for one would silently skip
    verifying it. Raises ``_EvidenceCorrupted`` on any failure; never returns a placeholder.
    """
    if not paths.is_safe_component(filename):
        raise _EvidenceCorrupted(f'a plan revision names an unsafe file ("{filename}"); nothing was resumed.')
    candidate = act_dir / filename
    try:
        info = candidate.lstat()
    except OSError:
        info = None
    if info is None or not stat.S_ISREG(info.st_mode):
        raise _EvidenceCorrupted(
            f"the plan revision file ({candidate}) is missing, is a symlink, or is not a plain file directly inside "
            "the activation directory; it is evidence a review was run against, and it can no longer be verified as "
            "such. Nothing was resumed."
        )
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise _EvidenceCorrupted(f"the plan revision file ({candidate}) could not be read ({exc}). Nothing was resumed.") from exc
    if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
        raise _EvidenceCorrupted(
            f"the plan revision file ({candidate}) no longer matches the hash recorded when it was written -- its "
            "content may have changed since. Nothing was resumed."
        )
    return content


def _revisions_with_backfill(act_dir: Path, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``existing`` (verified), or a synthesized revision 0 when it is empty.

    Every legacy (pre-resume) document gets a real revision 0 the first time it is loaded,
    through ``State._migrate``. A document ``arm`` wrote *after* that but before ``arm`` itself
    records revision 0 (a later phase) has none either -- this makes that case behave the same
    way migration does: honest that the hash only attests to the file as found right now, not
    as it was when the activation was armed. Once any resume has touched a document, its
    ``plan_revisions`` is never empty again.

    Whether ``existing`` is empty or not, this never trusts content it has not itself just
    read: an empty list gets its revision 0 synthesized from ``plan.frozen.md`` (raising
    ``_ResumeFailure`` if that cannot be read at all -- a synthesized revision 0 with an
    empty-content hash would be worse than a refusal, since it is the evidence every review to
    date was run against). A non-empty list has **every** entry re-verified against the hash it
    recorded, on **every** call -- not only revision 0's backfill, and not only the *active*
    (last) entry. Verifying only the active one is not enough while the reviewer still always
    reads ``plan.frozen.md`` regardless of which revision is active (disclosure is not wired
    until a later phase, see the banner's own note): revision 0's file could be deleted or
    replaced while a later revision is active, resume would see only the active entry checked
    out fine, and the gate would review against evidence nothing here ever looked at again.
    """
    if not existing:
        frozen_bytes = _read_verified(act_dir, _PLAN_FROZEN_NAME, expected_sha256=None)
        return [{"at": now(), "phase": 1, "sha256": hashlib.sha256(frozen_bytes).hexdigest(), "file": _PLAN_FROZEN_NAME}]
    revisions = [dict(entry) for entry in existing]
    for entry in revisions:
        # `expected_sha256=None` is reserved for the synthesis above, where there is
        # legitimately nothing yet to compare against -- passing it here for an *already
        # recorded* entry would silently skip verifying it. A missing or malformed sha256 on
        # an existing entry is not "nothing to compare"; it is exactly the untrusted-state.json
        # case this whole function exists to refuse, so it is checked before the file is even
        # opened.
        recorded_hash = entry.get("sha256")
        if not isinstance(recorded_hash, str) or not _SHA256_HEX_RE.fullmatch(recorded_hash):
            raise _EvidenceCorrupted(
                f'the plan revision recorded for "{entry.get("file")}" has no valid sha256 recorded; its integrity '
                "cannot be verified. Nothing was resumed."
            )
        _read_verified(act_dir, str(entry.get("file")), expected_sha256=recorded_hash)
    return revisions


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
        # and pre-retirement cross-session alike -- so the ordinary commit path, which never
        # verifies plan.frozen.md's integrity itself, is blocked by the NEEDS_HUMAN it already
        # checks for, rather than silently going on consuming corrupted evidence. See the
        # exception's own docstring. Once retirement has already happened the predecessor is
        # RESUMED (already blocking), so nothing further is needed there.
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
            "human may clear. Run /opencode-review-loop:stop, then /opencode-review-loop:implement <plan.md> to start over."
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

    allow_dirty = flags.allow_dirty or probe_config.as_bool("allow_dirty")
    if revision is not None:
        if not gitsnap.worktree_clean(repo):
            raise _ResumeFailure(
                "a plan revision was decided (the plan file changed), which requires a clean worktree regardless of "
                f"--allow-dirty or allow_dirty in config -- commit the current phase first:\n{gitsnap.dirty_summary(repo)}"
            )
    elif not allow_dirty and not gitsnap.worktree_clean(repo):
        raise _ResumeFailure(
            f"the worktree is dirty. Either commit or stash the existing changes, or re-run with --allow-dirty to fold "
            f"them into the next phase's review:\n{gitsnap.dirty_summary(repo)}"
        )

    until = prev_state.get_int("stop_after_phase")
    until_given = bool(flags.until)
    warnings = revision_warning
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
    decision = _Decision(overrides=overrides, revision=revision, until=until, until_given=until_given, warnings=warnings, allow_dirty=allow_dirty)
    if identity.same_session:
        return _resume_same_session(state=prev_state, identity=identity, flags=flags, decision=decision)
    return _resume_cross_session(prev_state=prev_state, identity=identity, flags=flags, decision=decision)


def _resume_same_session(*, state: State, identity: _Identity, flags: _Flags, decision: _Decision) -> str:
    repo, revision = identity.repo, decision.revision
    head_warning = ""
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
            if revision is not None:
                if not gitsnap.worktree_clean(repo):
                    raise commands.Refused(
                        f"the worktree became dirty before the revision could be published; commit the current phase "
                        f"first and resume again:\n{gitsnap.dirty_summary(repo)}\n"
                    )
                revisions = _apply_revision(
                    act_dir=state.act_dir, existing=state.data.get("plan_revisions") or [], change=revision, phase=state.get_int("phase")
                )
                state.update(plan_revisions=revisions, plan_path=revision.source_path)
            else:
                state.update(plan_revisions=_revisions_with_backfill(state.act_dir, state.data.get("plan_revisions") or []))
            if decision.until_given:
                state.update(stop_after_phase=decision.until)
            state.update(overrides=decision.overrides, activation_generation=state.get_int("activation_generation") + 1)

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
                    "into the next phase's review and the final cumulative review. last_approved_tree is left untouched.\n"
                )
    except commands.Refused as exc:
        raise _ResumeFailure(str(exc)) from exc
    except StateLoadError as exc:
        raise _ResumeFailure(f"the live activation could not be re-read ({exc}).") from exc

    return _banner(state=state, identity=identity, decision=replace(decision, warnings=decision.warnings + head_warning))


def _resume_cross_session(*, prev_state: State, identity: _Identity, flags: _Flags, decision: _Decision) -> str:
    repo, session = identity.repo, identity.session
    snapshot: dict[str, Any] = {}

    def _retire() -> None:
        nonlocal snapshot
        _refuse_unless_resumable(prev_state)
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
        if revision is not None:
            raise _ResumeFailure(
                "the worktree became dirty while this resume was publishing the revised plan; commit the current "
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
            "the next phase's review and the final cumulative review. last_approved_tree is left untouched.\n"
        )

    successor_state = State(repo, session)
    successor_state.data = new_data
    successor_state.save()
    return successor_state, head_warning


def _banner(*, state: State, identity: _Identity, decision: _Decision) -> str:
    repo, session, prev_session, same_session = identity.repo, identity.session, identity.prev_session, identity.same_session
    revision, warnings = decision.revision, decision.warnings
    config = config_module.load(repo, overrides=state.data.get("overrides"))
    variant = config.as_str("variant")
    reviewer = f"{config.as_str('model')}{f' (variant {variant})' if variant else ''}"
    total = state.phase_count()
    phase = state.get_int("phase")
    target = state.get_int("stop_after_phase")
    pause_target = f"{target} of {total}" if target else "none"
    plan_revisions_list = state.data.get("plan_revisions") or []
    revision_count = len(plan_revisions_list) or 1
    # The reviewer (`reviewer._plan_excerpt`) reads a hardcoded plan.frozen.md and does not
    # yet know a revision exists -- that disclosure is a later phase of this feature. Saying
    # so here is what stops a revision from being implemented against as if it were already
    # governing the review it is not shown to.
    revision_note = (
        "\nNote: a plan revision is recorded, but the reviewer does not yet read revised plan content -- it always "
        "evaluates against plan.frozen.md (the original). Implement against plan.frozen.md until this is wired up.\n"
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
            f"Phases are not frozen yet. Read the frozen plan ({state.act_dir}/plan.frozen.md) and run:\n\n"
            f'    {commands.plugin_root()}/scripts/ocrl.sh set-phases --phase "…" --phase "…"\n'
        )
    else:
        next_steps = f"Continue with phase {phase} of {total or '?'}:\n\n    {state.phase_desc(phase)}\n"

    resumed_from_line = "" if same_session else f"- resumed from session: {prev_session}\n"
    return f"""\
**opencode-review-loop is RESUMED for this worktree.**

- repository: {repo}
- session: {session}
{resumed_from_line}- plan revision: {revision_count} ({"changed just now" if revision is not None else "unchanged"})
{revision_note}- baseline tree: {state.get("baseline_tree")}
- activation commit: {state.get("activation_commit") or "<empty repository>"}
- phase: {phase} of {total or "?"}
- pause target: {pause_target}
- reviewer: {reviewer}
{warnings}
{next_steps}"""
