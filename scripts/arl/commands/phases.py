"""``set-phases`` -- freeze the phase list, once, for one activation.

Ports ``cmd_set_phases``. The list is frozen rather than editable because the phase
descriptions are evidence: every review is shown the phase it is reviewing, and a list that
can be rewritten mid-activation lets the work be redescribed to match whatever was built.
"""

from __future__ import annotations

import sys
from typing import Final

from arl import commands, gitsnap
from arl.atomic import write_private_atomic
from arl.config import Config
from arl.errors import StateLoadError
from arl.paths import state_root
from arl.state import State

__all__ = ["MAX_PHASES", "run"]

#: A bound against a runaway list, not a judgment on how finely a plan is decomposed.
MAX_PHASES: Final = 64


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
    """The one permitted transition is ARMED -> ACTIVE, and only once -- unless ``replan_pending``.

    Called with the activation lock held and the document freshly reloaded, so ``ACTIVE``
    here means another call froze the list first -- not that this one is a repeat. A
    ``replan_pending`` activation is handled by the caller before this is reached: it is
    ``ACTIVE`` (already frozen once) but explicitly permitted a second, narrower freeze, so
    this function's "already frozen" refusal must not fire for it.
    """
    status = state.effective_status(config)
    if status == "ARMED":
        return
    if status in ("ACTIVE", "RECONCILE"):
        raise commands.Refused(
            f"adversarial-review-loop: the phase list is already frozen ({state.phase_count()} phases, "
            f"currently on phase {state.get('phase')}). It cannot be changed for this activation.\n"
        )
    raise commands.Refused(f"adversarial-review-loop: cannot set phases while the activation is {status} ({state.get('reason')}).\n")


def _validate(phases: list[str], *, max_total: int) -> str:
    """Empty when the list is acceptable, otherwise the message to print.

    ``max_total`` is the length the *whole* frozen list will have once ``phases`` is applied:
    the caller's own count for an initial freeze, or the immutable prefix plus ``phases`` for
    a replan, so ``MAX_PHASES`` is checked against what will actually be written either way.
    """
    if not phases:
        return 'adversarial-review-loop: at least one --phase "…" is required.\n'
    if max_total > MAX_PHASES:
        return f"adversarial-review-loop: {max_total} phases is more than the {MAX_PHASES} this gate accepts; group them.\n"
    # `${p// /}` strips spaces only, so a tab-only description is accepted by the shell too.
    if any(not phase.replace(" ", "") for phase in phases):
        return "adversarial-review-loop: empty phase description.\n"
    return ""


def _freeze_initial(state: State, config: Config, phases: list[str]) -> str:
    """The ordinary ARMED -> ACTIVE freeze. Raises ``commands.Refused``. Returns a warning, if any."""
    _refuse_unless_armed(state, config)
    problem = _validate(phases, max_total=len(phases))
    if problem:
        raise commands.Refused(problem)
    write_private_atomic(state.act_dir / "phases.frozen", "".join(f"{phase}\n" for phase in phases), root=state_root())
    stop_after_phase = state.get_int("stop_after_phase")
    clamp_warning = ""
    if stop_after_phase > len(phases):
        clamp_warning = (
            f"adversarial-review-loop: the pause target (phase {stop_after_phase}) is beyond the {len(phases)} "
            f"phases just frozen; clamped to {len(phases)}.\n"
        )
        stop_after_phase = len(phases)
    # `replan_pending=False` is belt and braces: a fresh ARMED document already defaults to
    # it, but a token granted by a resume *before* this first freeze must not survive it --
    # see AGENTS.md, "the replan fence". Without this, a second `set-phases` call at any later
    # point, mid-implementation, would take the replan branch and rewrite the phase in flight.
    state.update(phases=phases, phase=1, status="ACTIVE", reason="", stop_after_phase=stop_after_phase, replan_pending=False)
    return clamp_warning


def _replan(state: State, phases: list[str]) -> str:
    """Redefine the phases from the current one onward. Raises ``commands.Refused``.

    ``1..phase-1`` are immutable by construction here: the caller only ever supplies the tail,
    so there is no argument through which an already-committed phase's description could be
    touched, and the immutable prefix is carried forward unchanged regardless of what ``phases``
    contains.
    """
    current_phase = state.get_int("phase")
    committed = state.get_array("phases")[: current_phase - 1]
    combined = committed + phases
    problem = _validate(phases, max_total=len(combined))
    if problem:
        raise commands.Refused(problem)
    # The resume that granted `replan_pending` may be several tool calls old by the time this
    # runs -- re-check the worktree is still clean under the lock, rather than trust a promise
    # made earlier in the turn.
    if not gitsnap.worktree_clean(state.worktree):
        raise commands.Refused(
            f"adversarial-review-loop: the worktree is dirty, so the replan cannot be published -- commit or discard the "
            f"change first:\n{gitsnap.dirty_summary(state.worktree)}\n"
        )
    write_private_atomic(state.act_dir / "phases.frozen", "".join(f"{phase}\n" for phase in combined), root=state_root())
    stop_after_phase = state.get_int("stop_after_phase")
    clamp_warning = ""
    if stop_after_phase > len(combined):
        clamp_warning = (
            f"adversarial-review-loop: the pause target (phase {stop_after_phase}) is beyond the {len(combined)} "
            f"phases just redefined; clamped to {len(combined)}.\n"
        )
        stop_after_phase = len(combined)
    # The generation bump invalidates a review that started before the rewrite: `set-phases`
    # is otherwise the one mutation of review scope that leaves no trace in either staleness
    # fingerprint (`hooks.Activation`, `completion.Fingerprint`).
    state.update(
        phases=combined,
        status="ACTIVE",
        reason="",
        stop_after_phase=stop_after_phase,
        replan_pending=False,
        activation_generation=state.get_int("activation_generation") + 1,
    )
    return clamp_warning


def run(argv: list[str]) -> int:
    session, phases = _parse(argv)

    activation = commands.resolve_local_activation(session)
    if activation is None:
        sys.stderr.write("adversarial-review-loop: no activation found for this worktree. Run /adversarial-review-loop:implement <plan.md> first.\n")
        return 1

    state = activation.state
    # Everything -- the status check, the argument check, the frozen file and the state
    # update -- happens under one hold of the activation lock, against the document the
    # transaction reloads. Checking the status beforehand and updating afterwards is what let
    # two concurrent calls both pass the check and then both write: the second silently
    # rewrote a phase list the first had already frozen and started reviewing against, which
    # is exactly the redescribe-the-work-to-fit-the-code failure freezing exists to prevent.
    clamp_warning = ""
    replanning = False
    start_phase = 1
    try:
        with state.transaction():
            replanning = state.effective_status(activation.config) == "ACTIVE" and state.get("replan_pending") == "true"
            start_phase = state.get_int("phase")
            clamp_warning = _replan(state, phases) if replanning else _freeze_initial(state, activation.config, phases)
    except StateLoadError:
        # The document went away between resolving the activation and taking the lock.
        sys.stderr.write("adversarial-review-loop: no activation found for this worktree. Run /adversarial-review-loop:implement <plan.md> first.\n")
        return 1
    except commands.Refused as exc:
        sys.stderr.write(str(exc))
        return 1

    total = state.phase_count()
    if replanning:
        out = [f"adversarial-review-loop: phases {start_phase}..{total} redefined. Still on phase {start_phase} of {total}:\n\n"]
        out += [f"  {index}. {state.phase_desc(index)}\n" for index in range(start_phase, total + 1)]
        out.append(f"\nContinue implementing phase {start_phase}, then commit it. The commit is the review gate.\n")
    else:
        out = [f"adversarial-review-loop: {total} phases frozen. Now on phase 1 of {total}:\n\n"]
        out += [f"  {index + 1}. {phase}\n" for index, phase in enumerate(phases)]
        out.append("\nImplement phase 1, then commit it. The commit is the review gate.\n")
    if clamp_warning:
        out.append(f"\n{clamp_warning}")
    sys.stdout.write("".join(out))
    return 0
