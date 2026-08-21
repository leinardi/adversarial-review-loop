"""``arm`` -- freeze a plan and put this worktree under the gate.

Ports ``cmd_arm``, ``ocrl_arm_die`` and ``ocrl_split_args`` from ``scripts/ocrl.sh``.

Two properties carry over unchanged, and both are load-bearing:

**Every failure this command can observe is persisted as ``ARM_FAILED``** (Rule 0). Arming
that fails must leave state saying so, because the alternative -- no state at all -- is
indistinguishable from a session that was never armed, and the hooks would then be denying
without being able to say why. The message printed here is what the user sees; the state is
what the next tool call reads.

**The character-set check on the plan path protects the loop's state, not the machine.**
``skills/implement/SKILL.md`` interpolates ``$ARGUMENTS`` into a shell body that is then
``eval``-ed, so anything injectable has already run by the time this code sees the string.
The check is still worth keeping -- it stops a nonsense path from being frozen into an
activation -- but it must not be mistaken for a sandbox. See AGENTS.md, "The argument
channel is not escaped".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ocrl import commands, gitsnap, paths
from ocrl import config as config_module
from ocrl.atomic import ensure_private_dir, write_private_atomic
from ocrl.config import Config
from ocrl.state import State, pointer_write
from ocrl.util import now

__all__ = ["run", "split_args"]

#: Whitespace the shell's ``[[:space:]]`` matches in the C locale. ``str.strip()`` would
#: also strip Unicode spaces, which the shell would have left in the path.
_SPACE: Final = " \t\n\v\f\r"

#: Characters a plan path may contain. Anything else is refused rather than quoted.
_PLAN_RE: Final = re.compile(r"[A-Za-z0-9._/@:+,~-][A-Za-z0-9._/@:+,~ -]*")

#: How long ``opencode models`` is given to answer during the reachability probe.
MODELS_PROBE_TIMEOUT: Final = 60

ARM_FAILED_MESSAGE: Final = """\
**opencode-review-loop: ARMING FAILED — the review loop is NOT active.**

Reason: {reason}

Every file mutation and every commit in this worktree is denied until this is
resolved. Do not attempt to implement the plan.

Ways out:
  - fix the cause and run `/opencode-review-loop:implement <plan.md>` again
  - abandon the mode with `/opencode-review-loop:stop`
"""

NO_SESSION_MESSAGE: Final = (
    "**opencode-review-loop: ARMING FAILED** — no session id was supplied, so no state could be recorded. "
    "The review loop is NOT active; do not implement anything."
)


class _ArmFailure(Exception):
    """An arming failure that must be persisted before the command returns."""


@dataclass(frozen=True)
class _Request:
    """What arming was asked to do, before any of it has been validated."""

    session: str
    repo: str
    plan: str
    flag: str
    config: Config


@dataclass(frozen=True)
class _Frozen:
    """What arming settled on, once every check has passed. This is the activation."""

    plan: str
    act_dir: Path
    baseline: str
    head_commit: str
    allow_dirty: bool


def _rstrip_space(text: str) -> str:
    return text.rstrip(_SPACE)


def split_args(raw: str) -> tuple[str, str]:
    """Split the slash command's single argument string into ``(plan, flag)``.

    The whole string arrives as one argument because Claude Code's positional substitution
    is 0-based -- ``$1`` is the *second* argument, and an out-of-range ``$N`` is left in the
    body literally, where the expansion shell turns it into the empty string. ``$ARGUMENTS``
    is substituted unconditionally and keeps paths containing spaces intact, so the split
    happens here.

    Faithful to the retired shell's ``ocrl_split_args`` including its oddity: the third case
    matches on *any* whitespace followed by a dash, but then takes the last whitespace-separated
    word as the flag, whatever it starts with. ``"a -x b"`` therefore yields ``("a -x", "b")``,
    pinned in ``tests/unit/test_commands_arm.py``. It is harmless because the caller rejects
    every flag that is not ``--allow-dirty``.
    """
    raw = raw.strip(_SPACE)
    if not raw:
        return "", ""

    if re.search(rf"[{re.escape(_SPACE)}]--allow-dirty\Z", raw):
        return _rstrip_space(raw[: -len("--allow-dirty")]), "--allow-dirty"
    if raw == "--allow-dirty":
        return "", "--allow-dirty"
    if re.search(rf"[{re.escape(_SPACE)}]-", raw):
        index = max(raw.rfind(char) for char in _SPACE)
        flag = raw[index + 1 :]
        return _rstrip_space(raw[: index + 1]), flag
    return raw, ""


def _parse(argv: list[str]) -> tuple[str, str, str]:
    """``(session, plan, flag)`` from the dispatcher's arguments.

    An option whose value is missing consumes what is there and stops, rather than the
    shell's ``shift 2`` -- which fails on a one-element list, leaves the arguments untouched
    and spins forever. That is a bug fix, not a behaviour change: no reachable caller can
    tell the difference between "spun forever" and "was rejected".
    """
    session = plan = flag = ""
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--session", "--args", "--plan"):
            value = argv[index + 1] if index + 1 < len(argv) else ""
            if arg == "--session":
                session = value
            elif arg == "--plan":
                plan = value
            else:
                plan, parsed_flag = split_args(value)
                if parsed_flag:
                    flag = parsed_flag
            index += 2
            continue
        if arg == "--allow-dirty":
            flag = "--allow-dirty"
        elif arg:
            # An unrecognised positional is reported by name rather than ignored: it is
            # nearly always a mistyped flag, and silently arming would hide that.
            flag = arg
        index += 1
    return session, plan, flag


def _resolve_plan(plan: str) -> str:
    """Validate the plan path and expand a leading ``~/``. Raises ``_ArmFailure``."""
    if not plan:
        raise _ArmFailure("no plan path was supplied (usage: /opencode-review-loop:implement <path-to-plan.md>)")
    if not _PLAN_RE.fullmatch(plan):
        raise _ArmFailure(f'the plan path contains characters that are not safe to pass through shell expansion: "{plan}"')
    if plan.startswith("~/"):
        plan = f"{os.environ.get('HOME', '')}/{plan[2:]}"
    if not os.path.isfile(plan):
        raise _ArmFailure(f'the plan path does not resolve to an existing regular file: "{plan}"')
    if not os.access(plan, os.R_OK):
        raise _ArmFailure(f'the plan file is not readable: "{plan}"')
    return plan


def _check_reviewer(config: Config) -> None:
    """Refuse to arm when the reviewer cannot be reached. Raises ``_ArmFailure``.

    Arming with an unreachable reviewer would produce an activation whose every commit fails
    the review for an operational reason -- denials that look like findings. Better to say so
    now, while the user is watching the slash command's output.
    """
    if os.environ.get("OCRL_REVIEWER_CMD"):
        # The test seam stands in for OpenCode entirely; probing the real binary would make
        # the suite depend on it being installed.
        return
    if not paths.have("opencode"):
        raise _ArmFailure("the `opencode` binary is not on PATH, so the review gate cannot run")
    try:
        proc = subprocess.run(["opencode", "models"], capture_output=True, text=True, check=False, timeout=MODELS_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        # Whatever it managed to print before the deadline, as `timeout 60 … || true` kept.
        probe = exc.stdout.decode("utf-8", "surrogateescape") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    except OSError:
        probe = ""
    else:
        probe = proc.stdout
    probe = probe.rstrip("\n")

    if not probe:
        raise _ArmFailure("could not list OpenCode models (`opencode models` returned nothing); the reviewer is unreachable")
    model = config.as_str("model")
    if model not in probe.split("\n"):
        raise _ArmFailure(
            f'the configured model "{model}" is not among the models OpenCode reports. '
            "Set `model` in .opencode-review-loop.json or OCRL_MODEL to one that is."
        )


def _baseline_tree(repo: str, head_commit: str) -> str:
    """The tree arming freezes as "reviewed so far": HEAD's, or the empty tree.

    ``git hash-object -t tree /dev/null`` rather than the well-known SHA-1 constant, because
    a repository may use SHA-256 and then the constant is simply the wrong id.
    """
    if head_commit:
        return gitsnap.head_tree(repo)
    return gitsnap.git_run(repo, ["hash-object", "-t", "tree", "/dev/null"]).stdout.decode("utf-8", "surrogateescape").rstrip("\n")


def _freeze_plan(plan: str, act_dir: Path) -> None:
    """Copy the plan into the activation directory. Raises ``_ArmFailure``.

    The frozen copy is what every review is shown, so a plan edited afterwards cannot change
    what the gate believes was agreed. A copy that fails refuses to arm, where the shell's
    unchecked ``cp`` would have armed with no plan at all.
    """
    try:
        raw = Path(plan).read_bytes()
    except OSError as exc:
        raise _ArmFailure(f'the plan file could not be read: "{plan}" ({exc})') from exc
    try:
        write_private_atomic(
            act_dir / "plan.frozen.md",
            raw.decode("utf-8", "surrogateescape"),
            root=paths.state_root(),
            errors="surrogateescape",
        )
    except OSError as exc:
        raise _ArmFailure(f"the plan could not be frozen into the activation directory ({exc})") from exc


def _record_failure(state: State, *, session: str, repo: str, reason: str) -> None:
    """Persist ``ARM_FAILED`` and make it findable, then leave the message to the caller.

    Both pointers are written on this path too. Without the session pointer the next tool
    call reads "arming never executed" and denies with a message about a sandbox, hiding the
    real reason; without the worktree pointer ``status`` cannot find the activation at all.
    """
    with state.transaction(create=True):
        state.update(status="ARM_FAILED", reason=reason, session_id=session, worktree=repo, armed_at=now())
    pointer_write(session, repo)
    commands.write_latest(repo, session)


def _armed_message(request: _Request, frozen: _Frozen) -> str:
    config = request.config
    variant = config.as_str("variant")
    reviewer = f"{config.as_str('model')}{f' (variant {variant})' if variant else ''}"
    return f"""\
**opencode-review-loop is ARMED for this worktree.**

- repository: {request.repo}
- plan: {frozen.plan} (frozen copy: {frozen.act_dir}/plan.frozen.md)
- baseline tree: {frozen.baseline}
- activation commit: {frozen.head_commit or "<empty repository>"}
- reviewer: {reviewer}
- pre-existing uncommitted work folded into phase 1: {"true" if frozen.allow_dirty else "false"}
- block_severity: {config.as_str("block_severity")}

**Phases are not set yet, so every file mutation is currently denied.**

Do this first, and nothing else:

1. Read the frozen plan.
2. Split it into the smallest sensible sequence of phases, each of which ends in
   one commit.
3. Run exactly this, one `--phase` per phase, in order:

       {commands.plugin_root()}/scripts/ocrl.sh set-phases --phase "…" --phase "…"

After that the loop is: implement phase N -> `git add -A && git commit -m "…"`.
The commit is intercepted, the working tree is reviewed by OpenCode, and the
commit only proceeds when the review passes. Findings come back as a denial with
the full list; fix them and commit again.

Constraints while the mode is active:
- each phase must commit all of its work and leave a clean worktree
- `git commit --amend`, partial commits, and compound commands that build or
  mutate files before committing are denied
- run builds and tests as their own Bash calls, then commit separately
"""


def run(argv: list[str]) -> int:
    session, plan, flag = _parse(argv)

    repo = commands.current_repo()
    request = _Request(session=session, repo=repo, plan=plan, flag=flag, config=config_module.load(repo))

    if not session:
        # Nothing can be recorded without a session id -- it *is* the key state is stored
        # under -- so this one failure is reported and nothing else.
        sys.stdout.write(f"{NO_SESSION_MESSAGE}\n")
        return 1

    state = State(repo, session)
    try:
        frozen = _arm(state, request)
    except _ArmFailure as exc:
        _record_failure(state, session=session, repo=repo, reason=str(exc))
        sys.stdout.write(ARM_FAILED_MESSAGE.format(reason=str(exc)))
        return 1

    sys.stdout.write(_armed_message(request, frozen))
    return 0


def _arm(state: State, request: _Request) -> _Frozen:
    """Everything that can fail. Raises ``_ArmFailure``; the caller persists and reports."""
    repo, config = request.repo, request.config
    if request.flag and request.flag != "--allow-dirty":
        raise _ArmFailure(f'the second argument was "{request.flag}"; the only accepted value is --allow-dirty')

    plan = _resolve_plan(request.plan)

    if gitsnap.git_run(repo, ["rev-parse", "--git-dir"]).returncode != 0:
        raise _ArmFailure(
            f"the working directory ({os.getcwd()}) is not inside a git repository; the commit is the phase boundary, so a repository is required"
        )

    allow_dirty = request.flag == "--allow-dirty" or config.as_bool("allow_dirty")
    if not allow_dirty and not gitsnap.worktree_clean(repo):
        raise _ArmFailure(
            "the worktree is dirty. Either commit or stash the existing changes, or re-run with "
            f"--allow-dirty to fold them into phase 1's review:\n{gitsnap.dirty_summary(repo)}"
        )

    _check_reviewer(config)

    head_commit = gitsnap.head_commit(repo)
    frozen = _Frozen(
        plan=plan,
        act_dir=state.act_dir,
        baseline=_baseline_tree(repo, head_commit),
        head_commit=head_commit,
        allow_dirty=allow_dirty,
    )

    ensure_private_dir(state.act_dir, root=paths.state_root())
    _freeze_plan(plan, state.act_dir)

    with state.transaction(create=True):
        # A fresh document: re-arming the same session starts a new activation, and carrying
        # the old one's approved trees forward would approve a tree nobody reviewed for it.
        state.new()
        state.update(
            status="ARMED",
            reason="",
            session_id=request.session,
            worktree=repo,
            plan_path=plan,
            baseline_tree=frozen.baseline,
            activation_commit=head_commit,
            last_approved_tree=frozen.baseline,
            armed_at=now(),
            allow_dirty=allow_dirty,
        )
        state.mark_tree_approved(frozen.baseline)

    pointer_write(request.session, repo)
    commands.write_latest(repo, request.session)
    return frozen
