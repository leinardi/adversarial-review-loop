"""``pretool`` -- the ``PreToolUse`` gate. Ports ``cmd_pretool`` and the two gates it calls.

This is the entrypoint that decides whether a mutation happens, and it runs on **every**
tool call, so both of its invariants are load-bearing:

- **Read-only tools answer before config or state is loaded.** They are permitted in every
  state, so the check is hoisted above ``config.load``. See :func:`_pretool`.
- **One import per job.** ``cmdshape``, ``gitsnap``, ``reviewer`` and ``report`` are imported
  inside the functions that need them: answering a ``Read`` must not pay for the reviewer
  stack.

Every branch ends in an emitter, and every emitter raises. Anything that escapes reaches
``Hook.run``'s fail-closed guard, which denies (**Rule 1**).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, NoReturn

from ocrl import commands, paths
from ocrl import config as config_module
from ocrl.commands import hooks
from ocrl.hookio import Hook, read_hook_input
from ocrl.state import State, pointer_read

if TYPE_CHECKING:  # pragma: no cover - the hot path must not import these to type-check
    from ocrl import reviewer
    from ocrl.config import Config
    from ocrl.gitsnap import Snapshot
    from ocrl.hookio import HookInput

__all__ = ["run"]

NO_SESSION: Final = """\
The hook payload carried no session id, so the gate cannot tell which activation this call belongs to. Denying rather than guessing.

This is an integration fault, not a review finding. Tell the user, and leave the mode with /opencode-review-loop:stop if it persists.
"""

UNSTARTED_ARM: Final = """\
Arming never ran, so nothing was frozen, nothing is being reviewed, and this
mutation is denied.

The hooks registered, which means /opencode-review-loop:implement was invoked,
but the arm command itself never executed. The usual causes are a sandbox that
refused to run it, an unreadable or non-executable scripts/ocrl.sh, or a
missing interpreter.

Tell the user. They can look at the error the slash command printed, fix the
cause and run /opencode-review-loop:implement <plan.md> again, or leave the
mode with /opencode-review-loop:stop.

Do not implement the plan: enforcement was requested and is not running.
"""

MISSING_STATE: Final = """\
The activation state for this session is missing, so the gate cannot tell what has been reviewed. Every mutation and commit is denied.

Run /opencode-review-loop:implement <plan.md> again, or /opencode-review-loop:stop to leave the mode.
"""

ARM_FAILED: Final = """\
Arming failed, so the review loop is NOT active and nothing may be changed.

Reason: {reason}

Run /opencode-review-loop:implement <plan.md> again, or /opencode-review-loop:stop to leave the mode.
"""

NEEDS_HUMAN: Final = """\
The loop escalated to NEEDS_HUMAN and every mutation is denied until the user intervenes.

Reason: {reason}

Stop and tell the user. They can leave the mode with /opencode-review-loop:stop.
"""

STALE: Final = """\
This activation is older than ttl_hours ({ttl_hours}) and is presumed abandoned. It blocks rather than silently disarming.

Continue it with /opencode-review-loop:resume, which keeps the baseline and every approval and re-verifies the worktree before picking back up — that is usually the right recovery. Re-arm with /opencode-review-loop:implement <plan.md> only to start over from scratch, or leave the mode with /opencode-review-loop:stop.
"""

RESUMED: Final = """\
This activation was retired by a resume; {successor} took over. It no longer accepts any mutation, and nothing here can undo that.

Continue in {successor}, or re-arm this worktree from scratch with /opencode-review-loop:implement <plan.md>.
"""

ESCAPE_DENIED: Final = """\
ocrl finish, deactivate, resume and config are user-only commands. You may not run them yourself.

If you believe one of them should run, say so and let the user run /opencode-review-loop:finish, \
/opencode-review-loop:stop, /opencode-review-loop:resume or /opencode-review-loop:config.
"""

PHASES_NOT_FROZEN: Final = """\
The phase list has not been frozen yet, so nothing may be changed.

Read the frozen plan ({act_dir}/{plan_file}), then run exactly:

    {plugin_root}/scripts/ocrl.sh set-phases --phase "…" --phase "…"

one --phase per phase, in order. Reading the repository is allowed; changing it
is not, until that command has run.
"""

REPLAN_PENDING: Final = """\
A resume granted permission to redefine the remaining phases, but that has not happened yet,
so nothing may be changed.

Read the current frozen plan ({act_dir}/{plan_file}), then run exactly:

    {plugin_root}/scripts/ocrl.sh set-phases --phase "…" --phase "…"

replacing only the phases from the current one onward -- phases already committed are
immutable and are kept automatically. Reading the repository is allowed; changing it is not,
until that command has run.
"""

SET_PHASES_ALLOWED: Final = "opencode-review-loop: set-phases is the one command allowed before the phase list is frozen."

SET_PHASES_ALLOWED_REPLAN: Final = "opencode-review-loop: set-phases is the one command allowed while a replan is pending."

BAD_COMMIT_SHAPE: Final = """\
This commit command was not accepted: {error}

Only these shapes are accepted, because nothing in them can change working-tree
content after the gate has snapshotted it:

  git commit -m "…"
  git add -A && git commit -m "…"
  git add -A && git status && git commit -m "…"

Run builds, tests, formatters and `git rm` as their own separate Bash calls
first; the next snapshot picks their result up. Then commit with one of the
shapes above.
"""

OVERSIZED: Final = """\
These files are above max_file_bytes ({limit}) and would be committed without ever being snapshotted for review:

{files}

Gitignore them, commit them deliberately outside this mode, or raise max_file_bytes. Nothing is silently dropped from the snapshot.
"""

SNAPSHOT_FAILED: Final = """\
The working state could not be snapshotted: {error}

The commit is denied because there is nothing to review against.
"""

DIFF_FAILED: Final = """\
The diff between the last approved tree ({base}) and the working state ({head}) could not be computed, so the gate cannot tell what changed.

The commit is denied: an unreadable diff is not "no changes".
"""

STATE_ROOT_DENIED: Final = """\
This tool would write inside the review loop's own state directory:

  {path}

That directory ({root}) holds the frozen plan, the record of what has been reviewed, and the
status this gate enforces. Nothing in the repository under review has any reason to edit it,
and editing it is how the mode gets switched off without anyone deciding to.

If you believe the loop should end, say so and let the user run /opencode-review-loop:stop.
"""

EXPANSION_DENIED: Final = """\
This command contains {expansion}, so the gate cannot tell what it will run.

That is not a guess it may make: a command name produced by an expansion can be `git`, and a
commit created that way would never reach the review gate at all. Substitutions and variable
expansions are therefore refused while this worktree is under review.

Write the command out literally, or split it into separate Bash calls and use the results
yourself. A `$` inside single quotes is literal to bash and is not affected.
"""

ACTIVATION_MOVED: Final = """\
{change} while this commit was being gated, so the decision that was reached no longer applies to it. Nothing was approved and the commit is denied.

The activation is now {now}. Commit again: the gate reviews the current working state against the current activation.
"""

ESCALATED: Final = """\
This escalated to NEEDS_HUMAN and is not an approval.

{error}

Full report: {report}

Stop here and tell the user. The gate keeps denying until they run /opencode-review-loop:stop.
"""

FAILURES_EXHAUSTED: Final = """\
The reviewer failed {failures} times in a row (limit {limit}), so this escalated to NEEDS_HUMAN. A failed review is never an approval.

Last failure: {error}

Stop here and tell the user.
"""

REVIEW_FAILED: Final = """\
The review could not be completed ({failures} of {limit} consecutive failures), so the commit is denied. A failed review is never an approval.

{error}

Raw output: {raw}

Commit again to retry.
"""

RESET_REJECTED: Final = """\
The recovery reset was rejected: {error}

During reconcile the only permitted command is:

  git reset --soft {parent}
"""

RESET_UNRESOLVED: Final = 'The reset target "{target}" does not resolve to a commit. The only permitted target is {parent}.\n'

RESET_WRONG_TARGET: Final = (
    "The reset target resolves to {resolved}, but the only permitted target during this reconcile is the diverging commit's own parent, {parent}.\n"
)

RESET_PREDATES_ACTIVATION: Final = (
    "The reset target {resolved} is strictly before the activation commit {activation}. The loop may not rewind history that predates it.\n"
)

RESET_ALLOWED: Final = "opencode-review-loop: bounded recovery reset to {resolved} permitted. Rebuild the intended complete tree, then commit again."

RESET_UNREADABLE: Final = """\
This reset was not accepted: {error}

Outside a reconcile the only reset this gate can reason about is `git reset --soft <target>`,
and it must not move HEAD off a commit that was reviewed. A reset it cannot read is denied
rather than passed through: every commit in this history is a tree that was actually
reviewed, and rewinding one breaks that guarantee.

If you meant to unstage something, there is no need — the review snapshot is taken through a
throwaway index and never reads the real one.
"""

RECONCILE_FROM_ABANDONED: Final = """\
The commit resume --abandon-pending gave up on ({bad}) landed after all, on top of the parent \
it was expected to build on. Nothing here reviewed it.

Recover explicitly: `git reset --soft {parent}`, rebuild the intended complete tree, and \
commit again through the normal review gate.
"""

ABANDONED_MARKER_UNVERIFIABLE: Final = """\
Whether a commit resume --abandon-pending gave up on already landed could not be checked: {error}

The commit is denied: an unreadable history is not the same as nothing having landed.
"""

RESET_OFF_REVIEWED_COMMIT: Final = (
    "Moving HEAD off a reviewed commit is only permitted during a reconcile. Every commit in this history is a tree that was "
    "actually reviewed, and rewinding one breaks that guarantee.\n"
)


def _guard_state_root(hook: Hook, payload: HookInput) -> None:
    """Refuse an editing tool aimed at the gate's own state.

    Everything the gate knows lives in one directory, and the ordinary tool route to it is
    wide open: ``pretool`` passes every non-Bash tool, so a ``Write`` of
    ``{"status": "DISARMED"}`` over ``state.json`` ends the mode without any of the checks
    that exist to stop exactly that (Rule 4).

    This closes the *tool* route and only that. A Bash command can still write the same file,
    and no check inside this process can prevent it -- the gate runs as the same user, and by
    the time it is asked about a command that command is about to run. See AGENTS.md, "What
    Rule 4 does and does not guarantee". Worth having anyway: it is one string comparison, and
    it removes the route that needs no shell at all.
    """
    if not payload.path:
        return
    # `realpath`, not `abspath`: containment is decided after every symlink is resolved, on
    # both sides. A lexical comparison is bypassed by a link in the repository pointing at the
    # state root -- `link/…/state.json` shares no prefix with it -- and by a relative
    # `OCRL_STATE_DIR`, which never matches an absolute target at all. `realpath` handles a
    # path that does not exist yet: it resolves the part that does and keeps the rest.
    root = os.path.realpath(paths.state_root())
    target = os.path.realpath(os.path.join(payload.cwd or os.getcwd(), payload.path))
    if target == root or target.startswith(f"{root}{os.sep}"):
        hooks.deny(hook, STATE_ROOT_DENIED.format(path=payload.path, root=root))


def _entrypoint() -> str:
    """The exact path ``arm`` told the model to run, and the only one ``set-phases`` accepts."""
    return f"{commands.plugin_root()}/scripts/ocrl.sh"


def run(argv: list[str]) -> int:
    """Entrypoint for the ``PreToolUse`` hook."""
    del argv
    hook = Hook()
    # Armed before stdin is even read: a crash anywhere between here and the decision must
    # still produce a denial rather than a silent non-zero exit, which Claude Code treats as
    # a non-blocking error and proceeds through.
    hook.arm_failclosed("pretool")
    return hook.run(lambda: _pretool(hook))


def _pretool(hook: Hook) -> None:
    payload = read_hook_input()
    cwd = payload.cwd or os.getcwd()
    tool = payload.tool_name

    # No pointer for this session -> arming never executed. Fail closed (Rule 0).
    worktree = pointer_read(payload.session_id)
    if not worktree:
        _no_pointer(hook, session=payload.session_id, cwd=cwd, tool=tool)

    repo = hooks.resolve_repo(cwd, worktree) or cwd
    if repo != worktree:
        # Work outside the armed worktree in the same session is untouched.
        hook.pass_()

    # A read-only tool is permitted in *every* state -- each branch below would end up
    # passing for one. Answering here avoids loading the config and the state to reach a
    # foregone conclusion, and Read/Grep/Glob are the bulk of all tool traffic.
    #
    # The shell repeated this check inside each denying branch; those repeats were
    # unreachable and are not reproduced. **If a future state ever needs to deny a read-only
    # tool, this hoist has to come out first** -- and the per-branch checks have to come back
    # with it, or the deny is unreachable in the other direction.
    if hooks.tool_is_readonly(tool):
        hook.pass_()

    state = State(worktree, payload.session_id)
    if not state.load():
        # The entrypoint ran, so the skill was invoked, but no state exists. That is the
        # fail-open case, and it fails closed instead.
        hooks.deny(hook, MISSING_STATE)
    config = config_module.load(repo, overrides=state.data.get("overrides"))

    _gate(hook, payload, state=state, config=config, repo=repo)


def _no_pointer(hook: Hook, *, session: str, cwd: str, tool: str) -> NoReturn:
    """No session pointer: either the payload is unusable, or arming never ran."""
    if not session:
        # The gate cannot identify what it is protecting, so it denies rather than guessing.
        hooks.deny(hook, NO_SESSION)
    # This one is *not* the hoist below -- it is reached before it, and it is what keeps a
    # session whose arming never started from denying every Read as well.
    if hooks.tool_is_readonly(tool):
        hook.pass_()
    hooks.record_unstarted_arm(session, cwd)
    hooks.deny(hook, UNSTARTED_ARM)


def _gate_terminal_status(hook: Hook, *, state: State, config: Config, status: str) -> None:
    """The statuses that end the decision unconditionally, before anything is classified.

    Split out of :func:`_gate` to keep it under the branch-count linter's ceiling -- these
    four are independent of ``tool``/``command`` and of each other, so factoring them out
    changes nothing about the decision itself.
    """
    if status in ("COMPLETE", "DISARMED"):
        hook.pass_()
    if status == "ARM_FAILED":
        hooks.deny(hook, ARM_FAILED.format(reason=state.get("reason")))
    if status == "NEEDS_HUMAN":
        hooks.deny(hook, NEEDS_HUMAN.format(reason=state.get("reason")))
    if status == "STALE":
        hooks.deny(hook, STALE.format(ttl_hours=config.as_int("ttl_hours")))
    if status == "RESUMED":
        # Terminal, like the three above: retirement must never be something a mutation in
        # the old session can talk its way past.
        successor = state.get("resumed_into") or "the successor session"
        hooks.deny(hook, RESUMED.format(successor=successor))


def _verified_plan_file(hook: Hook, *, state: State, config: Config) -> str:
    """The active plan revision's file name, fully verified, or escalate and deny.

    **Verifies before ``set-phases`` is ever considered for an allow**, not only when this is
    reached to build a denial message: both ``ARMED`` and ``replan_pending`` are about to let
    the model redefine phase scope from whatever it read, and if the activation's own record
    of what that evidence *is* cannot be trusted, that must block the freeze itself, not just
    the wording of an unrelated denial. So this reads and hash-verifies the active revision in
    full (:func:`planrev.verified_revisions`, the same check ``reviewer.build_bundle`` runs
    before a review), not merely its name -- a corrupted or tampered entry must not let
    ``set-phases`` through on the strength of "the filename looked safe".

    Escalation is **guarded**, exactly as ``pretool._escalate``/``stop._escalate`` already are
    for every other path that writes ``NEEDS_HUMAN``: ``expected`` is captured before the
    verification runs, and ``hooks.escalate`` only writes if the activation still matches it
    when the write actually happens. An unconditional ``state.needs_human(...)`` here could
    otherwise land after a concurrent ``/opencode-review-loop:stop`` and re-enable the gate the
    user just turned off (Rule 4) -- the same failure the reviewer-escalation path already
    guards against, reached this time from a check with no slow operation in between, but the
    same race in miniature: nothing here holds the lock continuously between the read and the
    write.
    """
    from ocrl import planrev  # noqa: PLC0415 - not on the read-only hot path

    expected = hooks.activation(state, config)
    try:
        revisions = planrev.verified_revisions(state.act_dir, state.data.get("plan_revisions") or [])
    except planrev.EvidenceCorrupted as exc:
        if hooks.escalate(state, config, expected, str(exc)):
            hooks.deny(hook, NEEDS_HUMAN.format(reason=str(exc)))
        with state.transaction():
            current = hooks.activation(state, config)
        hooks.deny(hook, ACTIVATION_MOVED.format(change=hooks.describe_move(expected, current), now=current.summary))
    entry, _content = revisions[-1]
    return str(entry.get("file"))


def _gate(hook: Hook, payload: HookInput, *, state: State, config: Config, repo: str) -> None:
    """Everything from the effective status down. Ends in an emitter on every path."""
    from ocrl import cmdshape  # noqa: PLC0415 - not on the read-only hot path

    tool, command = payload.tool_name, payload.command
    status = state.effective_status(config)

    _gate_terminal_status(hook, state=state, config=config, status=status)

    # The escapes are user-only; Claude's route is Bash, and it is denied (Rule 4).
    if tool == "Bash" and cmdshape.is_escape(command):
        hooks.deny(hook, ESCAPE_DENIED)

    if status == "ARMED":
        # Verified *before* `set-phases` is even considered for an allow -- see
        # `_verified_plan_file`'s own docstring for why the order matters here. Named, not
        # hardcoded to plan.frozen.md: a resume can decide a revision before phases are ever
        # frozen, and pointing at the wrong file here would have the model split and freeze
        # phases against evidence a review will not actually be run against.
        plan_file = _verified_plan_file(hook, state=state, config=config)
        # The whole command must *be* the exception, not merely contain it, and it must be
        # this gate's own script by exact path -- a basename test trusts any executable named
        # `ocrl` that the repository under review happens to ship. See `cmdshape.is_set_phases`.
        if tool == "Bash" and cmdshape.is_set_phases(command, _entrypoint()):
            hook.allow(SET_PHASES_ALLOWED)
        hooks.deny(hook, PHASES_NOT_FROZEN.format(act_dir=state.act_dir, plugin_root=commands.plugin_root(), plan_file=plan_file))

    if status == "ACTIVE" and state.get("replan_pending") == "true":
        # Same fence as ARMED, and for the same reason: a phase list that can be rewritten
        # while other tool calls are permitted lets work happen, or even be reviewed, before
        # the description it is meant to match exists. See AGENTS.md, "the replan fence".
        # Verified before the allow-check, same as the ARMED branch above.
        plan_file = _verified_plan_file(hook, state=state, config=config)
        if tool == "Bash" and cmdshape.is_set_phases(command, _entrypoint()):
            hook.allow(SET_PHASES_ALLOWED_REPLAN)
        hooks.deny(hook, REPLAN_PENDING.format(act_dir=state.act_dir, plugin_root=commands.plugin_root(), plan_file=plan_file))

    if tool != "Bash":
        _guard_state_root(hook, payload)
        hook.pass_()

    # Words this gate cannot read are refused before anything is classified. `$(printf git)`
    # runs `git commit`, and no textual pass -- nor bashlex, which now reads the shape -- can
    # resolve it to a command name. See `cmdshape.unresolved_expansion`.
    expansion = cmdshape.unresolved_expansion(command)
    if expansion:
        hooks.deny(hook, EXPANSION_DENIED.format(expansion=expansion))

    if status == "RECONCILE" and cmdshape.mentions_reset(command):
        _gate_reset(hook, state=state, repo=repo, command=command)

    if cmdshape.mentions_commit(command):
        _gate_commit(hook, state=state, config=config, repo=repo, command=command)

    if cmdshape.mentions_reset(command):
        _guard_reset(hook, repo=repo, command=command)

    hook.pass_()


# --------------------------------------------------------------------------
# The commit gate
# --------------------------------------------------------------------------


def _prepare(hook: Hook, *, state: State, config: Config, repo: str, command: str) -> Snapshot:
    """Everything that must hold before the working state is worth reviewing at all.

    Order is the shell's, and it matters: the shape check runs first, so a command the gate
    cannot read as a safe commit sequence is refused before anything is cleared or snapshotted.
    """
    from ocrl import cmdshape, gitsnap  # noqa: PLC0415 - reached only by a commit

    try:
        cmdshape.validate_commit(command)
    except cmdshape.CommandShapeError as exc:
        hooks.deny(hook, BAD_COMMIT_SHAPE.format(error=exc))

    # A pending approval from an earlier attempt is stale by definition here.
    if state.get("pending_approved_tree"):
        with state.transaction():
            state.update(pending_approved_tree="", pending_head="", pending_command="")

    limit = config.as_int("max_file_bytes")
    too_big = gitsnap.oversized(repo, limit)
    if too_big:
        hooks.deny(hook, OVERSIZED.format(limit=limit, files=gitsnap.format_oversized(too_big).rstrip("\n")))

    try:
        return gitsnap.snapshot(repo)
    except gitsnap.SnapshotError as exc:
        hooks.deny(hook, SNAPSHOT_FAILED.format(error=exc))


def _gate_commit(hook: Hook, *, state: State, config: Config, repo: str, command: str) -> None:
    """Decide on the Bash call that would create a commit, running the review if needed."""
    from ocrl import gitsnap, report, reviewer  # noqa: PLC0415 - reached only by a commit

    # A commit resume --abandon-pending gave up on may still have landed, in the retired
    # session, after the marker was recorded. Nothing else in this activation ever sees that
    # commit's own PostToolUse, so this is checked before anything here approves another one.
    try:
        bad = hooks.resolve_abandoned_marker(state, repo=repo)
    except gitsnap.GitUnavailable as exc:
        hooks.deny(hook, ABANDONED_MARKER_UNVERIFIABLE.format(error=exc))
    if bad:
        hooks.deny(hook, RECONCILE_FROM_ABANDONED.format(bad=bad, parent=state.get("bad_commit_parent")))

    snap = _prepare(hook, state=state, config=config, repo=repo, command=command)
    tree = snap.tree
    phase = state.get_int("phase")
    base = state.get("last_approved_tree")
    head = gitsnap.head_commit(repo)
    # Captured after any stale approval is cleared and before anything slow runs. A review
    # takes minutes, and an approval that lands on an activation which moved during them is
    # an approval of something nobody asked for. See `hooks.Activation`.
    expected = hooks.activation(state, config)

    def approve(message: str) -> NoReturn:
        """Record the pending approval and allow the commit.

        ``pending_command`` is stored alongside the tree because ``confirm-commit`` will only
        let the very command that was approved consume the approval.

        The activation is re-checked **inside** the transaction, against the document it
        reloads. Reloading alone is not enough: the write would still land, so an activation
        that escalated to ``NEEDS_HUMAN``, expired, entered ``RECONCILE``, was stopped or was
        re-armed while the reviewer ran would have this approval written over the top of it
        -- and the tool call allowed. That is the failure-into-approval direction Rule 1
        forbids, so a mismatch denies instead.
        """
        try:
            with state.transaction():
                current = hooks.activation(state, config)
                if current != expected:
                    raise commands.Refused(ACTIVATION_MOVED.format(change=hooks.describe_move(expected, current), now=current.summary))
                state.mark_tree_approved(tree)
                state.update(pending_approved_tree=tree, pending_head=head, pending_command=command, failures=0)
        except commands.Refused as exc:
            hooks.deny(hook, str(exc))
        hook.allow(message)

    if tree == base:
        approve("opencode-review-loop: the working state is byte-identical to the last approved tree, so no review was needed. Commit allowed.")
    if state.tree_approved(tree):
        approve(f"opencode-review-loop: this exact tree ({tree}) was already approved by a previous review. Commit allowed.")

    # The shell tested `[ -z "$(git diff --name-only …)" ]`, which cannot tell "no changes"
    # from "git failed" -- and read a failure as an approval. Rule 1 says it must not, so the
    # exit status is checked here and a failed diff denies.
    diff = gitsnap.git_run(repo, ["diff", "--name-only", base, tree])
    if diff.returncode != 0:
        hooks.deny(hook, DIFF_FAILED.format(base=base, head=tree))
    if not diff.stdout:
        approve("opencode-review-loop: no content difference from the last approved tree. Commit allowed.")

    if gitsnap.all_paths_ignored(repo, base, tree, config):
        approve("opencode-review-loop: every changed path matches ignore_globs, so the review was skipped. Commit allowed.")

    target = reviewer.Target(repo=repo, base=base, head=tree, scope="phase", phase=phase)
    review = reviewer.execute(target, state=state, config=config, warnings=snap.warnings)

    if review.verdict == "APPROVED":
        extra = f"\nNon-blocking findings (recorded, not required):\n{review.all_findings}" if review.all_findings else ""
        approve(f"opencode-review-loop: OpenCode approved phase {phase}.\n{extra}\nFull report: {review.report}".rstrip("\n"))
    if review.verdict == "CHANGES_REQUIRED":
        # No preamble: the report's own headline already says what this is, as it did in the
        # shell -- `ocrl_deny "$(ocrl_report_reason …)"`, with no `ocrl_deny_preamble`.
        headline = f"opencode-review-loop: OpenCode requires changes before phase {phase} can be committed."
        hook.deny(report.reason(review, headline, config=config).rstrip("\n"))
    if review.verdict == "NEEDS_HUMAN":
        _escalate(hook, state=state, config=config, expected=expected, reason=review.error)
        hooks.deny(hook, ESCALATED.format(error=review.error, report=review.report or "<none>"))
    _review_failed(hook, state=state, config=config, expected=expected, review=review)


def _escalate(hook: Hook, *, state: State, config: Config, expected: hooks.Activation, reason: str) -> None:
    """Escalate, or deny saying the activation moved instead.

    The escalation is guarded for the same reason the approval is: the user can run
    ``/opencode-review-loop:stop`` while a review runs, and an escalation landing after that
    turns their ``DISARMED`` back into a state that denies everything (Rule 4).
    """
    if hooks.escalate(state, config, expected, reason):
        return
    with state.transaction():
        current = hooks.activation(state, config)
    hooks.deny(hook, ACTIVATION_MOVED.format(change=hooks.describe_move(expected, current), now=current.summary))


def _review_failed(hook: Hook, *, state: State, config: Config, expected: hooks.Activation, review: reviewer.Review) -> NoReturn:
    """An operational failure. Counted, and escalated once the run of them is long enough.

    Counted **inside** the transaction, against the document it reloads: two overlapping
    commits that each read the same starting count would otherwise both write the same value,
    so a run of failures would never reach the limit.
    """
    with state.transaction():
        failures = state.get_int("failures") + 1
        state.update(failures=failures)
    limit = config.as_int("max_failures")

    if failures > limit:
        _escalate(hook, state=state, config=config, expected=expected, reason=f"the reviewer failed {failures} times in a row: {review.error}")
        hooks.deny(hook, FAILURES_EXHAUSTED.format(failures=failures, limit=limit, error=review.error))
    hooks.deny(hook, REVIEW_FAILED.format(failures=failures, limit=limit, error=review.error, raw=review.raw or "<none>"))


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------


def _gate_reset(hook: Hook, *, state: State, repo: str, command: str) -> NoReturn:
    """The one bounded ``git reset --soft`` permitted while a reconcile is unfinished."""
    from ocrl import cmdshape, gitsnap  # noqa: PLC0415 - reached only during a reconcile

    parent = state.get("bad_commit_parent")
    try:
        target = cmdshape.reset_target(command)
    except cmdshape.CommandShapeError as exc:
        hooks.deny(hook, RESET_REJECTED.format(error=exc, parent=parent))

    resolved = gitsnap.rev_parse(repo, f"{target}^{{commit}}")
    activation = state.get("activation_commit")

    if not resolved:
        hooks.deny(hook, RESET_UNRESOLVED.format(target=target, parent=parent))
    if resolved != parent:
        hooks.deny(hook, RESET_WRONG_TARGET.format(resolved=resolved, parent=parent))
    if activation and resolved != activation and gitsnap.is_ancestor(repo, resolved, activation):
        hooks.deny(hook, RESET_PREDATES_ACTIVATION.format(resolved=resolved, activation=activation))
    hook.allow(RESET_ALLOWED.format(resolved=resolved))


def _guard_reset(hook: Hook, *, repo: str, command: str) -> None:
    """Outside a reconcile, a reset may not move HEAD off a commit that was reviewed.

    Fails closed on a reset it cannot read. The shell only denied when ``reset_target``
    *succeeded*, so every shape that made it raise was passed through instead -- including
    ``git reset --hard HEAD~1`` and a bare ``git reset HEAD~1``, both of which move HEAD off a
    reviewed commit. "The gate could not parse it" is not a reason to allow it (Rule 1).

    The cost is that ``git reset`` and ``git reset -- <path>``, which only unstage, are denied
    here too. Nothing in this loop needs them: the snapshot is taken through a throwaway index
    and never reads the real one, so what is staged has no bearing on what is reviewed.
    """
    from ocrl import cmdshape, gitsnap  # noqa: PLC0415 - reached only by a reset

    try:
        target = cmdshape.reset_target(command)
    except cmdshape.CommandShapeError as exc:
        hooks.deny(hook, RESET_UNREADABLE.format(error=exc))
    resolved = gitsnap.rev_parse(repo, f"{target}^{{commit}}")
    if resolved and resolved != gitsnap.head_commit(repo):
        hooks.deny(hook, RESET_OFF_REVIEWED_COMMIT)
