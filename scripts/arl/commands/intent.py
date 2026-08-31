"""``UserPromptSubmit``: record that a session asked for enforcement, before anything can fail.

The ``implement`` and ``resume`` skills arm the loop from a `` !`…` `` prompt-expansion line.
When that line cannot run at all -- a refused sandbox, an unreadable ``arl.sh``, a missing
interpreter, an unresolved ``${CLAUDE_PLUGIN_ROOT}`` -- ``arm`` persists nothing, and Claude
Code aborts the skill invocation so the skill body's own warning never reaches Claude either
(``tests/STEP0.md``, "A failed arm never reaches Claude"). The next prompt then lands in a
session with no pointer, which Rule 0 would otherwise read as "never asked for enforcement".

This hook runs on the prompt *itself*, before expansion. If the prompt is one of the two
arming commands, a marker is written for the session. ``arm`` and ``resume`` supersede it by
writing the pointer (``state.pointer_write`` clears it); if neither ever does, ``pretool`` and
``gate-stop`` find the marker with no pointer and record ``ARM_FAILED`` themselves.

**Exact-prefix match only.** The prompt must *start* with the command, so prose that mentions
it is not an intent, and a session is never wedged by talking about the plugin. And a marker
that could not be written blocks the prompt: enforcement was requested and could not be
recorded, and the arm that follows is worth less than the record that it was asked for.
"""

from __future__ import annotations

import re
import secrets
from typing import Final

from arl import hookio, paths
from arl.atomic import write_private_atomic
from arl.errors import OcrlError
from arl.hookio import Hook
from arl.util import log

__all__ = ["ARMING_COMMAND", "run"]

#: The two prompts that arm. Anchored at the start, terminated by whitespace or the end, so
#: ``:implementation`` or ``…:implement`` quoted mid-sentence match nothing.
ARMING_COMMAND: Final = re.compile(r"\A\s*/adversarial-review-loop:(?:implement|resume)(?:\s|\Z)")

BLOCKED: Final = (
    "adversarial-review-loop: enforcement was requested but the request could not be recorded ({detail}). "
    "The command was not run. Fix the cause and submit it again."
)


def run(argv: list[str]) -> int:
    """Entrypoint for the ``UserPromptSubmit`` hook."""
    del argv
    hook = Hook()
    # No fail-closed fallback: a crash here on an ordinary prompt must not block it, and on
    # an arming prompt the block is emitted explicitly below.
    hook.arm_failclosed("none")
    return hook.run(lambda: _record(hook))


def _record(hook: Hook) -> None:
    payload = hookio.read_hook_input()
    if not ARMING_COMMAND.match(payload.prompt):
        hook.pass_()
    if not paths.is_safe_component(payload.session_id):
        # No pointer can ever be written for this id either, so no gate can bind it; the
        # arm itself will refuse the id. Nothing to record.
        log(f"intent: unusable session id {payload.session_id!r}")
        hook.pass_()
    # The marker names the *worktree* the arm was asked for, so enforcement can scope it: a
    # failed arm in repository A must not deny (or be consumed by) a call in repository B.
    # Resolved here, once, on an arming prompt only -- never on the per-prompt hot path.
    worktree = paths.repo_root(payload.cwd) or payload.cwd
    # The token is what binds this request to the pointer that answers it: `pointer_write`
    # copies it onto the pointer, and a marker whose token the pointer carries is answered.
    token = secrets.token_hex(8)
    try:
        write_private_atomic(paths.intent_path(payload.session_id), f"{worktree}\nintent={token}\n", root=paths.state_root())
    except (OSError, OcrlError) as exc:
        # OcrlError: the state root or the intents directory is not what it should be
        # (a symlink, a plain file) -- refused by the private-directory guard.
        hook.prompt_block(BLOCKED.format(detail=exc))
    hook.pass_()
