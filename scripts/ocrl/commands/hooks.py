"""Plumbing shared by the four hook entrypoints.

What lives here is what more than one of ``pretool``, ``confirm-commit``,
``posttool-failure`` and ``gate-stop`` needs: the read-only tool table, the denial preamble,
resolving the payload's ``cwd`` onto the armed worktree, and **Rule 0** -- recording that
arming never executed.

Imports are kept to what the ``PreToolUse`` hot path already pays for. ``pretool`` runs on
every single tool call, so ``gitsnap``, ``cmdshape``, ``reviewer`` and ``report`` are
imported inside the functions that need them rather than at module scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, NoReturn

from ocrl import commands, paths
from ocrl import config as config_module
from ocrl.config import Config
from ocrl.hookio import Hook
from ocrl.state import State, pointer_write
from ocrl.util import log, now

__all__ = [
    "DENY_PREAMBLE",
    "READONLY_TOOLS",
    "UNSTARTED_ARM_REASON",
    "Activation",
    "activation",
    "deny",
    "describe_move",
    "escalate",
    "record_unstarted_arm",
    "resolve_repo",
    "tool_is_readonly",
]

#: Tools that cannot change the repository. Everything else is treated as a mutation, so an
#: unknown or newly added tool fails closed rather than open.
READONLY_TOOLS: Final = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "LS",
        "NotebookRead",
        "WebFetch",
        "WebSearch",
        "TodoWrite",
        "AskUserQuestion",
        "Skill",
        "SlashCommand",
        "ToolSearch",
        "ExitPlanMode",
        "EnterPlanMode",
        "TaskOutput",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
        "ReadMcpResourceDirTool",
    }
)

DENY_PREAMBLE: Final = "opencode-review-loop is enforcing an external review gate in this worktree.\n\n"

UNSTARTED_ARM_REASON: Final = (
    "arming never executed: the hooks registered, but ocrl.sh arm did not run at "
    "prompt-expansion time. Nothing was frozen and nothing has been reviewed."
)


def tool_is_readonly(tool: str) -> bool:
    return tool in READONLY_TOOLS


# --------------------------------------------------------------------------
# "Is this still the activation I decided about?"
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Activation:
    """Everything that must be unchanged for a decision taken earlier to still apply.

    Both hooks that write a decision -- ``pretool`` approving a commit, ``confirm-commit``
    advancing a phase -- do slow work first: a review that takes minutes, or a handful of git
    processes. Taking the activation lock and *reloading* is not enough on its own, because
    the reloaded document is then written to regardless of what it says. What that allows is
    the one direction Rule 1 forbids: an escalation, a reconcile, an expiry or a user's
    ``stop`` recorded while the slow work ran, and then quietly replaced by an approval.

    So each of those callers captures this before, compares it inside the transaction, and
    refuses on any difference. Deliberately an equality check over everything rather than a
    list of statuses that may not be overwritten -- a deny-list of statuses fails open the
    day one is added, which is how ``RECONCILE`` was missed once already.
    """

    armed_at: str
    baseline_tree: str
    session_id: str
    status: str
    effective_status: str
    phase: int
    last_approved_tree: str
    pending_approved_tree: str
    pending_head: str
    pending_command: str

    @property
    def identity(self) -> tuple[str, str, str]:
        """Which activation this is. ``arm`` writes a fresh document, so a re-arm changes it."""
        return (self.armed_at, self.baseline_tree, self.session_id)

    @property
    def summary(self) -> str:
        return f"{self.effective_status}, phase {self.phase}"


def activation(state: State, config: Config) -> Activation:
    """Capture the loaded document's activation record. Reads only; takes no lock."""
    return Activation(
        armed_at=state.get("armed_at"),
        baseline_tree=state.get("baseline_tree"),
        session_id=state.get("session_id"),
        status=state.get("status"),
        effective_status=state.effective_status(config),
        phase=state.get_int("phase"),
        last_approved_tree=state.get("last_approved_tree"),
        pending_approved_tree=state.get("pending_approved_tree"),
        pending_head=state.get("pending_head"),
        pending_command=state.get("pending_command"),
    )


def escalate(state: State, config: Config, expected: Activation, reason: str) -> bool:
    """Record ``NEEDS_HUMAN``, unless something else already moved the activation.

    ``State.needs_human`` writes unconditionally, which is right for arming but wrong at the
    end of a review: a review takes minutes, and the user can run ``/opencode-review-loop:stop``
    during them. An escalation landing afterwards turns their ``DISARMED`` back into a state
    that denies every mutation -- the gate re-enabling itself after the user left the mode,
    which Rule 4 says only they may decide.

    Answers whether the escalation stuck. ``False`` means the caller's whole decision is void
    and it must say so rather than report an escalation that was not recorded.
    """
    try:
        with state.transaction(create=True):
            if activation(state, config) != expected:
                raise commands.Refused(describe_move(expected, activation(state, config)))
            state.update(status="NEEDS_HUMAN", reason=reason)
    except commands.Refused:
        log(f"not escalating: the activation moved first ({reason})")
        return False
    return True


def describe_move(before: Activation, now: Activation) -> str:
    """Name what changed, most significant first, for the message the caller prints."""
    if before.identity != now.identity:
        return "this activation was re-armed"
    if before.effective_status != now.effective_status:
        return f"the activation moved from {before.effective_status} to {now.effective_status}"
    if before.phase != now.phase:
        return f"the phase moved from {before.phase} to {now.phase}"
    if before.last_approved_tree != now.last_approved_tree:
        return "another call approved a different tree"
    return "the pending approval changed"


def deny(hook: Hook, body: str) -> NoReturn:
    """Deny with the standard preamble in front of ``body``.

    The trailing newlines go because the shell built every one of these reasons inside
    ``$( … )``, which strips them, and the messages are asserted on character for character
    by ``tests/selftest.sh``.
    """
    hook.deny(f"{DENY_PREAMBLE}{body}".rstrip("\n"))


def resolve_repo(cwd: str, worktree: str) -> str:
    """Which repository the payload's ``cwd`` belongs to.

    The overwhelmingly common case is ``cwd`` already being the armed worktree root, and that
    needs no git process to establish -- which matters, because this is on the path of every
    tool call. Empty when ``cwd`` is not inside a repository at all; callers decide what that
    means for them.
    """
    if cwd == worktree:
        return worktree
    return paths.repo_root(cwd)


def record_unstarted_arm(session: str, cwd: str) -> tuple[State, Config] | None:
    """Persist ``ARM_FAILED`` for an activation whose ``arm`` never ran (**Rule 0**).

    A hook entrypoint only runs because the ``implement`` skill was invoked in this session
    -- that is what registers the hooks. So reaching one with no session pointer means
    ``ocrl arm`` never executed at all: a denied sandbox, a missing interpreter, an unreadable
    script, a ``${CLAUDE_PLUGIN_ROOT}`` that did not resolve. ``arm`` persists ``ARM_FAILED``
    for every failure it can observe, but it cannot persist a failure to start.

    Recording it here means the very next call takes the ordinary ``ARM_FAILED`` path, with
    all of its counting and messaging. Enforcement was requested and is not running; passing
    would be the one outcome the design exists to prevent.

    ``None`` when nothing could be recorded, which the caller reports as an integration
    fault. That happens when the session id is not usable as a key: an empty one lands the
    state in the worktree directory itself and makes ``pointer_write`` target a directory,
    so the next call repeats this branch forever, and an unsafe one is refused outright by
    ``ocrl.paths`` rather than composing a path out of the state root.
    """
    if not paths.is_safe_component(session):
        log(f"cannot record an unstarted arm for an unusable session id: {session!r}")
        return None

    repo = paths.repo_root(cwd) or cwd
    config = config_module.load(repo)
    state = State(repo, session)
    # `create=True`: there is nothing to load, and the document being written can only deny.
    with state.transaction(create=True):
        state.update(
            status="ARM_FAILED",
            worktree=repo,
            session_id=session,
            reason=UNSTARTED_ARM_REASON,
            armed_at=now(),
        )
    pointer_write(session, repo)
    commands.write_latest(repo, session)
    return state, config
