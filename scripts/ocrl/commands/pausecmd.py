"""``pause`` -- move the pause target without going back through ``resume``.

``stop_after_phase`` is the whole mechanism, and it already has every consumer it needs:
``posttool._advance`` prints ``PAUSE_TARGET_REACHED`` instead of ``NEXT_PHASE`` once the
commit that lands the target has been verified, and ``stop.run`` skips its
``PHASES_OUTSTANDING`` block and ends the turn on ``PAUSED``. Before this command the only
writers were ``arm`` and ``resume``, so the target could only be chosen *before* the work
started -- and moving it mid-flight meant ``resume --until N --allow-dirty``, which reruns
plan-revision detection, bumps the generation and demands a flag whose real meaning is
"fold the uncommitted work into the next phase's review". This writes the one integer.

**It is a soft target and grants nothing.** ``docs/edge-cases.md``, "Pausing is a soft
target, not a fence": the per-commit gate is exactly as strict either side of it, no denial
is added or removed, and no tree becomes approved. That is what makes a command this small
safe.

**It does not bump ``activation_generation``**, unlike ``accept`` and every ``resume``. The
bump exists to invalidate a decision whose *evidence* moved underneath a slow review -- a
plan revision, a model override, a newly approved tree. ``stop_after_phase`` is none of
those: it decides only whether the Stop gate asks for more phases. Bumping it would make an
in-flight final review land as ``completion.SUPERSEDED`` and discard a real verdict for no
safety gain. The one consequence, and it is benign: a Stop gate already past its own
``stop_after_phase`` read has captured the old target, so a pause racing a turn end takes
effect at the *next* turn end.

For the same reason there is no ``hooks.Activation`` compare here -- that pattern exists to
protect a decision taken *before* slow work from being written after it, and there is no
slow work in this command. Every value it reads is read inside the transaction that writes.

This is one of the exits Rule 4 reserves for the user (AGENTS.md). Two independent locks
keep Claude from reaching it: ``skills/pause/SKILL.md`` carries
``disable-model-invocation: true``, and ``pause`` is in ``cmdshape._ESCAPE_RE``, so
``pretool`` denies the Bash route too. Without both, an unbounded ``pause`` would be a
strictly better ``defer`` -- and ``defer`` is bounded by ``max_defers`` precisely because
"stop asking me for more" is also the shape of an agent that has stopped making progress.
"""

from __future__ import annotations

import sys
from typing import Final

from ocrl import commands
from ocrl.commands import arm
from ocrl.config import Config
from ocrl.state import State

__all__ = ["run"]

NOT_ARMED: Final = "opencode-review-loop: not armed in this worktree.\n"

USAGE: Final = (
    "opencode-review-loop: usage: /opencode-review-loop:pause [N | 0 | all]\n"
    "No argument pauses after the phase in flight; N pauses after phase N; 0 or all clears the target.\n"
)

#: Statuses ``pause`` may move the target under. The same live-and-gating set ``accept``
#: uses, and an allow-list for the reason ``accept._refusal`` documents: a deny-list fails
#: open the day a status is added and this is not updated for it.
_PAUSABLE: Final = frozenset({"ACTIVE", "NEEDS_HUMAN", "RECONCILE"})

#: The subset of :data:`_PAUSABLE` with something still outstanding over the activation. A
#: pause succeeds under these in the same words as any other, so the message has to name what
#: it is -- and they are *different* things, only one of which denies work. See
#: :func:`_unresolved_note`.
_UNRESOLVED: Final = frozenset({"NEEDS_HUMAN", "RECONCILE"})

_PHASES_NOT_FROZEN: Final = """\
opencode-review-loop: the phase list is not frozen yet, so there is no phase to pause after. \
Nothing was changed.

Freeze the phases first (the arm banner prints the set-phases command), then pause. To arm \
with a target already set, /opencode-review-loop:implement <plan.md> --until N.
"""

_ALL_PHASES_COMMITTED: Final = """\
opencode-review-loop: every frozen phase is already committed, so there is no phase left to \
pause after. Nothing was changed.

Ending the turn completes the activation; /opencode-review-loop:finish reviews the whole \
activation cumulatively first, and /opencode-review-loop:stop leaves the mode.
"""


def _refusal(state: State, config: Config) -> str | None:  # noqa: PLR0911 - one return per status this command must name, matched exactly
    """``None`` when this activation's target may move; the refusal text otherwise."""
    status = state.effective_status(config)
    if status in _PAUSABLE:
        if state.phase_count() == 0:
            return _PHASES_NOT_FROZEN
        # `phase` past the last frozen one is the window between the final phase's commit and
        # the Stop gate's own completion machinery. There is no later phase for a pause to
        # stop before, and clamping into it would silently do nothing.
        phase = state.get_int("phase")
        if phase < 1 or phase > state.phase_count():
            return _ALL_PHASES_COMMITTED
        return None
    if status == "ARMED":
        return _PHASES_NOT_FROZEN
    if status in ("COMPLETE", "DISARMED"):
        return f"opencode-review-loop: nothing is gated in this worktree ({status}), so there is nothing to pause.\n"
    if status == "STALE":
        return (
            f"opencode-review-loop: this activation is past ttl_hours ({config.as_int('ttl_hours')}), so it cannot be "
            "continued as it stands and a pause target would decide nothing. Run /opencode-review-loop:resume --until N, "
            "which refreshes the activation and sets the target in one step.\n"
        )
    if status in ("ARM_FAILED", "RESUMED"):
        return f"opencode-review-loop: this activation is {status}; there is no live activation here to pause.\n"
    return f"opencode-review-loop: cannot pause while the activation is {status}.\n"


def _parse_args(argv: list[str]) -> tuple[list[str], str]:
    """``(positionals, session)``.

    ``--args`` is the slash command's single substituted string, whitespace-split; a bare
    token on argv (how the tests and a shell caller spell it) is a positional too. Unlike
    ``arm.split_args`` nothing here is a path, so the split is plain and every token is kept
    -- including the extra ones, so :func:`run` can refuse rather than silently read the
    first and drop the rest.
    """
    positionals: list[str] = []
    session = ""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--args", "--session"):
            value = argv[index + 1] if index + 1 < len(argv) else ""
            if token == "--session":
                session = value
            else:
                positionals.extend(value.split())
            index += 2
            continue
        if token:
            positionals.append(token)
        index += 1
    return positionals, session


def _apply(state: State, *, config: Config, raw: str) -> str:
    """Move the target under one transaction. Raises ``commands.Refused`` on any refusal."""
    with state.transaction():
        problem = _refusal(state, config)
        if problem is not None:
            raise commands.Refused(problem)

        phase = state.get_int("phase")
        total = state.phase_count()

        # No argument means "after the phase in flight", which is the reason this command
        # exists -- resolved here, inside the transaction, against the phase the reloaded
        # document reports rather than one read before queueing for the lock. `0` and `all`
        # come back as 0 from the shared resolver and mean "no target at all".
        target = phase if raw == "" else arm.resolve_until(raw, flag="pause")

        if target and target < phase:
            raise commands.Refused(
                f"opencode-review-loop: phase {target} is already committed (the loop is on phase {phase} of {total}), "
                "so pausing after it would stop nothing. Nothing was changed. Pass no argument to pause after the phase "
                f"in flight, or a number from {phase} to {total}.\n"
            )

        # Clamped rather than refused, matching `phases.run`'s handling of an `--until` that
        # outran the phase list: the intent ("pause at the end") is unambiguous and the last
        # phase is what it means.
        note = ""
        if target > total:
            note = f"\nThe plan has only {total} phases, so the target was clamped to {total}.\n"
            target = total

        # The stored status, not the effective one: `_refusal` already turned every status
        # this command will not write under into a refusal, so by here the two agree. Read
        # because a pause under `NEEDS_HUMAN` or `RECONCILE` succeeds and says so in exactly
        # the same words as an ordinary one, while something is still outstanding over the
        # activation -- a different something in each case, and only `NEEDS_HUMAN` denies
        # work. Nothing downstream can tell any of that from the target alone, so the message
        # has to. See `_unresolved_note`.
        status = state.get("status")
        previous = state.get_int("stop_after_phase")
        state.update(stop_after_phase=target)

    if target == 0:
        was = f"was phase {previous} of {total}" if previous else "there was none set"
        # Carries the blocked note too. Clearing is still a write under a status where
        # `pretool` is denying work, and it is the *more* misleading of the two outcomes to
        # leave unqualified: there is no target left, so nothing downstream -- no
        # `PAUSE_TARGET_REACHED`, no `PAUSED` -- will ever mention the activation again.
        return (
            f"opencode-review-loop: pause target cleared ({was}). The loop will run to the end of the plan; it is on "
            f"phase {phase} of {total}. There is no pause target left, so no turn will end paused.\n"
            f"{_unresolved_note(status)}"
        )
    return (
        f"opencode-review-loop: pausing after phase {target} of {total}:\n\n    {state.phase_desc(target)}\n{note}\n"
        f"Nothing is denied by this and nothing was approved by it -- the review gate on every commit is unchanged.\n"
        # The blocked note comes *first*: it is a constraint on everything below it, and a
        # reader given "commit it as usual" before being told what is still outstanding has
        # already been told the wrong thing once.
        f"{_unresolved_note(status)}"
        f"{_what_happens_next(phase=phase, target=target, total=total, unresolved=status in _UNRESOLVED)}"
    )


def _what_happens_next(*, phase: int, target: int, total: int, unresolved: bool) -> str:
    """What the loop will actually do, for this exact target.

    ``unresolved`` re-tenses it: under ``NEEDS_HUMAN`` or ``RECONCILE`` this describes what
    happens once the outstanding thing is dealt with, rather than reading as an instruction to
    commit right now. :func:`_unresolved_note` has already said, immediately above, what that
    thing is and who deals with it -- which differs sharply between the two.

    Three different things, and getting them confused is worse than saying nothing. A target
    on the last phase changes no behaviour at all: ``posttool._advance`` tests
    ``next_phase > total`` *before* it tests the target, so that commit is answered with
    "all phases done" and never with "pause target reached", and the Stop gate's own pause
    branch is guarded by ``phase <= total``, which is false by then -- it completes instead.
    Promising a paused turn and a later resume there would be a plain lie. A target ahead of
    the phase in flight is the other trap: it does not stop the turn *this* phase ends on.
    """
    if target >= total:
        return (
            f"Phase {total} is the last one, so this changes nothing on its own -- the loop was already going to stop "
            f"there. It is on phase {phase} of {total}. Ending the turn once every phase is committed completes the "
            "activation as usual; there is no pause to resume from. To stop earlier, pause on an earlier phase.\n"
        )
    lead = "Once that is resolved, the loop" if unresolved else "The loop"
    resume = "\nContinue later with /opencode-review-loop:resume --until 0 (or --until M for a further target).\n"
    if target == phase:
        return (
            f"{lead} finishes the phase it is on (phase {phase} of {total}), commits it as usual, and then ends the "
            f"turn instead of continuing into phase {phase + 1}.\n{resume}"
        )
    return (
        f"{lead} is on phase {phase} of {total} and keeps going through phase {target}, then ends the turn instead of "
        f"continuing into phase {target + 1}.\n{resume}"
    )


def _unresolved_note(status: str) -> str:
    """The paragraph a pause under an unresolved activation must carry, or "".

    ``_PAUSABLE`` admits ``NEEDS_HUMAN`` and ``RECONCILE`` deliberately -- the target is
    still worth setting for whenever the activation is moving again, and refusing would make
    this command useless in exactly the situation a user is most likely to reach for it. But
    a pause succeeds there in the same words as an ordinary one, and the target alone
    distinguishes nothing, so what is still outstanding is named here.

    **The two are not the same fence, and must not be worded as though they were.**
    ``NEEDS_HUMAN`` is in ``pretool._gate_terminal_status``, so it genuinely denies every
    mutation and only the user can clear it. ``RECONCILE`` is not: an ``Edit`` still reaches
    ``hook.pass_()``, a non-commit ``Bash`` call still passes, the reset is bounded to the one
    prescribed target, and a commit still goes through the *ordinary* review gate. Its
    recovery is a three-step procedure Claude is meant to carry out
    (``posttool.RECONCILE_CONTEXT``), so telling it to stop there strands the phase until the
    user intervenes again -- the opposite of what the status is for.
    """
    if status == "NEEDS_HUMAN":
        return (
            "\nNote: this activation is NEEDS_HUMAN and every mutation is still denied. The pause changes nothing "
            "about that -- do not carry on implementing or try to commit. The escalation has to be resolved first "
            "(/opencode-review-loop:accept, or /opencode-review-loop:stop to leave the mode).\n"
        )
    if status == "RECONCILE":
        return (
            "\nNote: this activation is RECONCILE -- a commit landed that does not match what was reviewed, and that "
            "is still unresolved. The pause changes nothing about it either way. Carry out the recovery the gate "
            "already prescribed (the bounded reset, rebuild the phase's intended tree, commit again through the "
            "normal gate); the pause then applies to that commit like any other. Until the divergence is resolved "
            "the Stop gate will not complete this activation.\n"
        )
    return ""


def run(argv: list[str]) -> int:
    positionals, session = _parse_args(argv)
    if len(positionals) > 1:
        sys.stderr.write(USAGE)
        return 2

    activation = commands.resolve_local_activation(session)
    if activation is None:
        sys.stdout.write(NOT_ARMED)
        return 0

    try:
        message = _apply(activation.state, config=activation.config, raw=positionals[0] if positionals else "")
    except arm._ArmFailure as exc:
        sys.stderr.write(f"opencode-review-loop: {exc}\n")
        return 2
    except commands.Refused as exc:
        sys.stdout.write(str(exc))
        return 1

    sys.stdout.write(message)
    return 0
