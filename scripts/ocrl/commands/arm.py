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
from ocrl.atomic import ensure_private_dir, locked, write_private_atomic
from ocrl.config import Config
from ocrl.state import State, pointer_write
from ocrl.util import now

__all__ = ["flag_bool", "flag_str", "parse_flag_tokens", "run", "split_args"]

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

VERSION_CONFLICT_MESSAGE: Final = """\
**opencode-review-loop: ARMING REFUSED — an activation already at {act_dir} was written by a version {version} this build does not understand.**

Nothing here was touched: not the frozen plan, not state.json, not anything else in that \
directory. Overwriting it would destroy whatever that build recorded, which is worse than \
refusing outright.

The review loop is NOT active. Either run the build that wrote it, or -- only if you are sure \
discarding that record is safe -- remove {act_dir} yourself and re-run \
`/opencode-review-loop:implement <plan.md>`.
"""


class _ArmFailure(Exception):
    """An arming failure that must be persisted before the command returns."""


class _VersionConflict(Exception):
    """The activation directory already holds a document a newer build wrote.

    Deliberately not an ``_ArmFailure``: that class is always reported through
    ``_record_failure``, which persists ``ARM_FAILED`` -- and that write is itself refused
    for exactly the same document (``state.transaction`` raises regardless of ``create``),
    so routing this through the ordinary failure path would just move the crash rather than
    avoid it. This is reported directly, and nothing is written at all.
    """

    def __init__(self, version: int | None) -> None:
        self.version = version
        super().__init__(f"activation directory holds a version-{version} document")


@dataclass(frozen=True)
class _Request:
    """What arming was asked to do, before any of it has been validated."""

    session: str
    repo: str
    plan: str
    #: Raw flag tokens, exactly as split from argv -- ``["--until", "2", "--allow-dirty"]``.
    #: Parsed and validated inside ``_arm``, where an unrecognised one becomes ``_ArmFailure``.
    flags: tuple[str, ...]
    config: Config


@dataclass(frozen=True)
class _Flags:
    """The four flags ``implement`` accepts, parsed but not yet semantically validated."""

    allow_dirty: bool = False
    #: Raw ``--until`` text -- "", "0", "all" or a digit string -- resolved by ``_resolve_until``.
    until: str = ""
    #: ``None`` means "not given", distinct from an empty string: only a flag the user
    #: actually typed may override the stored model or variant.
    model: str | None = None
    variant: str | None = None


@dataclass(frozen=True)
class _Frozen:
    """What arming settled on, once every check has passed. This is the activation."""

    plan: str
    act_dir: Path
    baseline: str
    head_commit: str
    allow_dirty: bool
    until: int
    overrides: dict[str, str]
    #: The config actually probed and armed with -- defaults < user < repo < overrides < env
    #: already resolved. The one source of truth for "what does this activation actually
    #: run with", since env may itself outrank a --model/--variant override.
    effective_config: Config


def _rstrip_space(text: str) -> str:
    return text.rstrip(_SPACE)


def split_args(raw: str) -> tuple[str, list[str]]:
    """Split the slash command's single argument string into ``(plan, flag_tokens)``.

    The whole string arrives as one argument because Claude Code's positional substitution
    is 0-based -- ``$1`` is the *second* argument, and an out-of-range ``$N`` is left in the
    body literally, where the expansion shell turns it into the empty string. ``$ARGUMENTS``
    is substituted unconditionally and keeps paths containing spaces intact, so the split
    happens here.

    Rule: the plan is every token up to the first one starting with ``--``; the rest are
    flag tokens, whitespace-separated, each kept as its own element so a flag's value is
    still a separate token from its name -- exactly like ``argv``. This is a strict
    superset of the retired shell's ``ocrl_split_args`` for the cases that mattered (a path
    with any run of spaces, ``--allow-dirty`` alone or trailing) but retires its pinned
    oddity: ``"a -x b"`` used to yield ``("a -x", "b")`` by taking the last word as the flag
    whatever it started with. A single ``-`` no longer starts a flag boundary either -- only
    ``--`` does, matching every flag this gate accepts.

    The plan is sliced out of the original string at the boundary's byte offset, not
    rebuilt by rejoining tokens: a path legitimately containing more than one run of
    whitespace (``"my  plans/plan.md"``) must come back exactly as typed, or a file that
    really exists at that path stops resolving the moment a flag is added alongside it.
    """
    raw = raw.strip(_SPACE)
    if not raw:
        return "", []
    for match in re.finditer(rf"[^{re.escape(_SPACE)}]+", raw):
        if match.group().startswith("--"):
            plan = _rstrip_space(raw[: match.start()])
            flag_tokens = re.split(rf"[{re.escape(_SPACE)}]+", raw[match.start() :].strip(_SPACE))
            return plan, flag_tokens
    return raw, []


def _parse(argv: list[str]) -> tuple[str, str, list[str]]:
    """``(session, plan, flag_tokens)`` from the dispatcher's arguments.

    ``--session``, ``--plan`` and ``--args`` are the only tokens treated specially --
    ``--args`` is ``split_args`` on the shim's single substituted string, and every other
    token (whether it came from ``--args`` or directly on argv, in tests) is appended to the
    same flat token list, in order. Parsing those tokens into named, validated flags happens
    in ``_arm``, exactly where the equivalent single-flag check used to live.

    An option whose value is missing consumes what is there and stops, rather than the
    shell's ``shift 2`` -- which fails on a one-element list, leaves the arguments untouched
    and spins forever. That is a bug fix, not a behaviour change: no reachable caller can
    tell the difference between "spun forever" and "was rejected".
    """
    session = plan = ""
    flag_tokens: list[str] = []
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
                args_plan, args_flags = split_args(value)
                if args_plan:
                    plan = args_plan
                flag_tokens.extend(args_flags)
            index += 2
            continue
        if arg:
            flag_tokens.append(arg)
        index += 1
    return session, plan, flag_tokens


#: Flags accepted by ``implement``, and whether each one takes a value.
_BOOL_FLAGS: Final = ("--allow-dirty",)
_VALUE_FLAGS: Final = ("--until", "--model", "--variant")


def parse_flag_tokens(tokens: list[str], *, bool_flags: tuple[str, ...], value_flags: tuple[str, ...], usage: str) -> dict[str, str | bool]:
    """Generic ``--flag`` / ``--flag value`` tokenizer, shared by ``arm`` and ``resume``.

    Every token must be one of ``bool_flags`` or ``value_flags`` -- there are no bare
    positionals here, unlike a plan path (which each caller separates out before this runs),
    so a stray token lands here as an unknown flag rather than silently becoming part of a
    value. Raises ``_ArmFailure`` on the first problem: an unrecognised flag, or a value flag
    given nothing -- or another flag -- as its value. A missing value and a value that is
    itself another flag are the same mistake: nothing was actually given. Without this,
    ``--variant --allow-dirty`` would silently set ``variant="--allow-dirty"`` instead of
    reporting that ``--variant`` got no value -- swallowing the next flag whole rather than
    refusing.
    """
    result: dict[str, str | bool] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in bool_flags:
            result[token] = True
            index += 1
            continue
        if token in value_flags:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise _ArmFailure(f'"{token}" requires a value')
            result[token] = tokens[index + 1]
            index += 2
            continue
        raise _ArmFailure(f'unrecognised flag "{token}"; accepted flags are {usage}')
    return result


def flag_str(raw: dict[str, str | bool], key: str) -> str | None:
    """Narrow a parsed flag to its string value, or ``None`` when it was not given as one."""
    value = raw.get(key)
    return value if isinstance(value, str) else None


def flag_bool(raw: dict[str, str | bool], key: str) -> bool:
    return bool(raw.get(key, False))


def _parse_flags(tokens: list[str]) -> _Flags:
    """Turn raw flag tokens into ``_Flags``. Raises ``_ArmFailure`` on the first problem."""
    raw = parse_flag_tokens(tokens, bool_flags=_BOOL_FLAGS, value_flags=_VALUE_FLAGS, usage="--allow-dirty, --until, --model, --variant")
    return _Flags(
        allow_dirty=flag_bool(raw, "--allow-dirty"),
        until=flag_str(raw, "--until") or "",
        model=flag_str(raw, "--model"),
        variant=flag_str(raw, "--variant"),
    )


def _resolve_until(raw: str) -> int:
    """The pause target from ``--until``'s raw text. Raises ``_ArmFailure`` on nonsense.

    ``""``, ``"0"`` and ``"all"`` all mean "no target" -- the flag was not given, or the
    user explicitly cleared it. Anything else must be a plain positive integer; the upper
    bound (``N <= phase_count``) cannot be checked yet, since phases are not frozen at arm
    time, so ``commands/phases.py::run`` checks it again once they are.

    ``str.isdigit()`` is deliberately not used: it answers ``True`` for Unicode digits that
    ``int()`` cannot parse (superscripts among them), which would raise ``ValueError``
    straight out of this function instead of the ``_ArmFailure`` every other rejection here
    goes through -- crashing arming rather than persisting why it failed (Rule 0). A plain
    ASCII-only pattern rules that out. ``int()`` is still wrapped: an absurdly long digit
    string hits Python's own conversion length limit and raises ``ValueError`` on its own.
    """
    if raw in ("", "0", "all"):
        return 0
    if not re.fullmatch(r"[0-9]+", raw):
        raise _ArmFailure(f'--until "{raw}" is not a positive integer, "0" or "all"')
    try:
        value = int(raw)
    except ValueError as exc:
        raise _ArmFailure(f'--until "{raw}" is not a positive integer, "0" or "all"') from exc
    if value < 1:
        raise _ArmFailure(f'--until "{raw}" is not a positive integer, "0" or "all"')
    return value


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


def _record_failure(state: State, *, session: str, repo: str, reason: str, publish_latest: bool = True) -> None:
    """Persist ``ARM_FAILED`` and make it findable, then leave the message to the caller.

    The session pointer is always written: without it the next tool call reads "arming never
    executed" and denies with a message about a sandbox, hiding the real reason. The worktree
    (``latest``) pointer is what ``status`` and the other shell-run commands resolve, and
    ``publish_latest=False`` is for ``resume``'s cross-session failure *before* it has retired
    the predecessor -- ``latest`` must keep naming the still-live predecessor, not a session
    that failed to take over from it.
    """
    with state.transaction(create=True):
        state.update(status="ARM_FAILED", reason=reason, session_id=session, worktree=repo, armed_at=now())
    pointer_write(session, repo)
    if publish_latest:
        commands.write_latest(repo, session)


def _armed_message(request: _Request, frozen: _Frozen) -> str:
    # frozen.effective_config, not request.config or frozen.overrides directly: the
    # environment can itself outrank a --model/--variant override (defaults < user < repo <
    # overrides < env), so the override alone is not always what actually gets probed and
    # armed with -- only the fully-resolved config is.
    config = frozen.effective_config
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
- pause target: {frozen.until if frozen.until else "none"} (checked again once phases are frozen)
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
    session, plan, flag_tokens = _parse(argv)

    repo = commands.current_repo()
    request = _Request(session=session, repo=repo, plan=plan, flags=tuple(flag_tokens), config=config_module.load(repo))

    if not session:
        # Nothing can be recorded without a session id -- it *is* the key state is stored
        # under -- so this one failure is reported and nothing else.
        sys.stdout.write(f"{NO_SESSION_MESSAGE}\n")
        return 1

    state = State(repo, session)
    if not state.load() and state.version_conflict:
        # A fast, unlocked exit for the common case: no point running the dirty-worktree
        # check or probing the reviewer for an arm that is already going to be refused.
        # **Not the guarantee** -- a concurrent newer-build arm can land between this read
        # and the freeze below, so the authoritative check is the one _arm repeats under the
        # activation lock, immediately before it writes anything.
        sys.stdout.write(VERSION_CONFLICT_MESSAGE.format(act_dir=state.act_dir, version=state.version_conflict_value))
        return 1

    try:
        frozen = _arm(state, request)
    except _VersionConflict as exc:
        # _record_failure is deliberately not used here: it writes ARM_FAILED through the
        # same create=True path that state.transaction now refuses for a version conflict,
        # so calling it would just move the crash rather than avoid it.
        sys.stdout.write(VERSION_CONFLICT_MESSAGE.format(act_dir=state.act_dir, version=exc.version))
        return 1
    except _ArmFailure as exc:
        _record_failure(state, session=session, repo=repo, reason=str(exc))
        sys.stdout.write(ARM_FAILED_MESSAGE.format(reason=str(exc)))
        return 1

    sys.stdout.write(_armed_message(request, frozen))
    return 0


def _arm(state: State, request: _Request) -> _Frozen:
    """Everything that can fail. Raises ``_ArmFailure``; the caller persists and reports."""
    repo, config = request.repo, request.config
    parsed = _parse_flags(list(request.flags))

    plan = _resolve_plan(request.plan)

    if gitsnap.git_run(repo, ["rev-parse", "--git-dir"]).returncode != 0:
        raise _ArmFailure(
            f"the working directory ({os.getcwd()}) is not inside a git repository; the commit is the phase boundary, so a repository is required"
        )

    allow_dirty = parsed.allow_dirty or config.as_bool("allow_dirty")
    if not allow_dirty and not gitsnap.worktree_clean(repo):
        raise _ArmFailure(
            "the worktree is dirty. Either commit or stash the existing changes, or re-run with "
            f"--allow-dirty to fold them into phase 1's review:\n{gitsnap.dirty_summary(repo)}"
        )

    until = _resolve_until(parsed.until)

    # The candidate overrides are built, and the config reloaded with them applied, *before*
    # the reviewer is probed: probing the stored config would validate a model this run will
    # not use, and pass `--model <invalid>` on the strength of a model nobody asked for.
    overrides = {key: value for key, value in (("model", parsed.model), ("variant", parsed.variant)) if value is not None}
    probe_config = config_module.load(repo, overrides=overrides)
    _check_reviewer(probe_config)

    head_commit = gitsnap.head_commit(repo)
    frozen = _Frozen(
        plan=plan,
        act_dir=state.act_dir,
        baseline=_baseline_tree(repo, head_commit),
        head_commit=head_commit,
        allow_dirty=allow_dirty,
        until=until,
        overrides=overrides,
        effective_config=probe_config,
    )

    ensure_private_dir(state.act_dir, root=paths.state_root())

    # The freeze and the save happen inside the *same* hold of the activation lock as the
    # version recheck, not `state.transaction(create=True)` -- which would need to be
    # entered twice (once to check, once to write) and reopen exactly the window this
    # closes. `run`'s check above is only a fast, unlocked exit; a concurrent newer-build
    # arm can land between it and here, and this is what actually serialises against that:
    # whichever process takes the lock first finishes checking, freezing and saving before
    # the other can observe anything, so there is no gap in which an old build can read
    # "no conflict" and then overwrite a plan a newer build wrote a moment later.
    with locked(state.lock_file, root=paths.state_root()):
        if not state.load() and state.version_conflict:
            raise _VersionConflict(state.version_conflict_value)

        _freeze_plan(plan, state.act_dir)

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
            stop_after_phase=until,
            overrides=overrides,
        )
        state.mark_tree_approved(frozen.baseline)
        state.save()

    pointer_write(request.session, repo)
    commands.write_latest(repo, request.session)
    return frozen
