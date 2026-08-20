"""``set-phases`` -- freeze the phase list, once, for one activation.

Ports ``cmd_set_phases``. The list is frozen rather than editable because the phase
descriptions are evidence: every review is shown the phase it is reviewing, and a list that
can be rewritten mid-activation lets the work be redescribed to match whatever was built.
"""

from __future__ import annotations

import sys
from typing import Final

from ocrl import commands
from ocrl.atomic import write_private_atomic
from ocrl.config import Config
from ocrl.errors import StateLoadError
from ocrl.paths import state_root
from ocrl.state import State

__all__ = ["MAX_PHASES", "run"]

#: More than this is a plan, not a phase list, and the gate cannot hold a user to it.
MAX_PHASES: Final = 30


def _parse(argv: list[str]) -> tuple[str, list[str]]:
    session = ""
    phases: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--session":
            session = argv[index + 1] if index + 1 < len(argv) else ""
            index += 2
            continue
        if arg == "--phase":
            phases.append(argv[index + 1] if index + 1 < len(argv) else "")
            index += 2
            continue
        index += 1
    return session, phases


def _refuse_unless_armed(state: State, config: Config) -> None:
    """The one permitted transition is ARMED -> ACTIVE, and only once.

    Called with the activation lock held and the document freshly reloaded, so ``ACTIVE``
    here means another call froze the list first -- not that this one is a repeat.
    """
    status = state.effective_status(config)
    if status == "ARMED":
        return
    if status in ("ACTIVE", "RECONCILE"):
        raise commands.Refused(
            f"opencode-review-loop: the phase list is already frozen ({state.phase_count()} phases, "
            f"currently on phase {state.get('phase')}). It cannot be changed for this activation.\n"
        )
    raise commands.Refused(f"opencode-review-loop: cannot set phases while the activation is {status} ({state.get('reason')}).\n")


def _validate(phases: list[str]) -> str:
    """Empty when the list is acceptable, otherwise the message to print."""
    if not phases:
        return 'opencode-review-loop: at least one --phase "…" is required.\n'
    if len(phases) > MAX_PHASES:
        return f"opencode-review-loop: {len(phases)} phases is more than the {MAX_PHASES} this gate accepts; group them.\n"
    # `${p// /}` strips spaces only, so a tab-only description is accepted by the shell too.
    if any(not phase.replace(" ", "") for phase in phases):
        return "opencode-review-loop: empty phase description.\n"
    return ""


def run(argv: list[str]) -> int:
    session, phases = _parse(argv)

    activation = commands.resolve_local_activation(session)
    if activation is None:
        sys.stderr.write("opencode-review-loop: no activation found for this worktree. Run /opencode-review-loop:implement <plan.md> first.\n")
        return 1

    state = activation.state
    # Everything -- the status check, the argument check, the frozen file and the state
    # update -- happens under one hold of the activation lock, against the document the
    # transaction reloads. Checking ARMED beforehand and updating afterwards is what let two
    # concurrent calls both pass the check and then both write: the second silently rewrote a
    # phase list the first had already frozen and started reviewing against, which is exactly
    # the redescribe-the-work-to-fit-the-code failure freezing exists to prevent.
    try:
        with state.transaction():
            _refuse_unless_armed(state, activation.config)
            problem = _validate(phases)
            if problem:
                raise commands.Refused(problem)
            write_private_atomic(activation.act_dir / "phases.frozen", "".join(f"{phase}\n" for phase in phases), root=state_root())
            state.update(phases=phases, phase=1, status="ACTIVE", reason="")
    except StateLoadError:
        # The document went away between resolving the activation and taking the lock.
        sys.stderr.write("opencode-review-loop: no activation found for this worktree. Run /opencode-review-loop:implement <plan.md> first.\n")
        return 1
    except commands.Refused as exc:
        sys.stderr.write(str(exc))
        return 1

    out = [f"opencode-review-loop: {len(phases)} phases frozen. Now on phase 1 of {len(phases)}:\n\n"]
    out += [f"  {index + 1}. {phase}\n" for index, phase in enumerate(phases)]
    out.append("\nImplement phase 1, then commit it. The commit is the review gate.\n")
    sys.stdout.write("".join(out))
    return 0
