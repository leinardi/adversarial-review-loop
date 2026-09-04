"""``arm`` -- freeze a plan and put this worktree under the gate.

Ports ``cmd_arm``, ``arl_arm_die`` and ``arl_split_args`` from ``scripts/arl.sh``.

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

#  This file is part of adversarial-review-loop.
#
#  Copyright (c) 2026 Roberto Leinardi
#
#  adversarial-review-loop is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  adversarial-review-loop is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from arl import commands, gitsnap, guide, harness, paths, planrev, reviewer_probe
from arl import config as config_module
from arl.atomic import ensure_private_dir, locked, write_private_atomic
from arl.config import Config
from arl.state import State, pointer_read, pointer_write
from arl.util import now

__all__ = ["flag_bool", "flag_str", "parse_flag_tokens", "resolve_until", "run", "split_args"]

#: Whitespace the shell's ``[[:space:]]`` matches in the C locale. ``str.strip()`` would
#: also strip Unicode spaces, which the shell would have left in the path.
_SPACE: Final = " \t\n\v\f\r"

#: Characters a plan path may contain. Anything else is refused rather than quoted.
_PLAN_RE: Final = re.compile(r"[A-Za-z0-9._/@:+,~-][A-Za-z0-9._/@:+,~ -]*")

ARM_FAILED_MESSAGE: Final = """\
**adversarial-review-loop: ARMING FAILED — the review loop is NOT active.**

Reason: {reason}

Every file mutation and every commit in this worktree is denied until this is
resolved. Do not attempt to implement the plan.

Ways out:
  - fix the cause and run `/adversarial-review-loop:implement <plan.md>` again
  - abandon the mode with `/adversarial-review-loop:stop`
"""

NO_SESSION_MESSAGE: Final = (
    "**adversarial-review-loop: ARMING FAILED** — no session id was supplied, so no state could be recorded. "
    "The review loop is NOT active; do not implement anything."
)

VERSION_CONFLICT_MESSAGE: Final = """\
**adversarial-review-loop: ARMING REFUSED — an activation already at {act_dir} was written by a version {version} this build does not understand.**

Nothing here was touched: not the frozen plan, not state.json, not anything else in that \
directory. Overwriting it would destroy whatever that build recorded, which is worse than \
refusing outright.

The review loop is NOT active. Either run the build that wrote it, or -- only if you are sure \
discarding that record is safe -- remove {act_dir} yourself and re-run \
`/adversarial-review-loop:implement <plan.md>`.
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
    """The flags ``implement`` accepts, parsed but not yet semantically validated."""

    allow_dirty: bool = False
    #: Raw ``--until`` text -- "", "0", "all" or a digit string -- resolved by ``resolve_until``.
    until: str = ""
    #: ``None`` means "not given", distinct from an empty string: only a flag the user
    #: actually typed may override the stored harness, model or variant.
    #:
    #: ``harness`` is not validated here, only carried: it is checked in ``_check_reviewer``,
    #: with the binary and the model, so "this build does not implement that" is reported
    #: beside "that binary is not installed" rather than as a separate class of refusal.
    harness: str | None = None
    model: str | None = None
    variant: str | None = None
    #: ``--guide``: a repo-supplied reviewer guide, resolved and frozen once, here. ``None``
    #: means "not given", so the value keeps resolving through ``review_guide``'s ordinary
    #: config chain -- a repository or user default reaches the gate with no flag at all.
    guide: str | None = None


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
    #: The review guide's source path as resolved at arm, or ``""`` when none is in force.
    #: Disclosure only -- the source file is never read again, and the frozen copy beside
    #: ``plan.frozen.md`` is what every review is shown.
    guide_path: str
    #: sha256 of the frozen guide, or ``""``. Shown in the banner so the value a human can
    #: check against the activation directory is on screen at arm time.
    guide_sha256: str
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
    superset of the retired shell's ``arl_split_args`` for the cases that mattered (a path
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
_VALUE_FLAGS: Final = ("--until", "--harness", "--model", "--variant", "--guide")


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
    raw = parse_flag_tokens(
        tokens, bool_flags=_BOOL_FLAGS, value_flags=_VALUE_FLAGS, usage="--allow-dirty, --until, --harness, --model, --variant, --guide"
    )
    return _Flags(
        allow_dirty=flag_bool(raw, "--allow-dirty"),
        until=flag_str(raw, "--until") or "",
        harness=flag_str(raw, "--harness"),
        model=flag_str(raw, "--model"),
        variant=flag_str(raw, "--variant"),
        guide=flag_str(raw, "--guide"),
    )


def resolve_until(raw: str, *, flag: str = "--until") -> int:
    """The pause target from ``--until``'s raw text. Raises ``_ArmFailure`` on nonsense.

    ``""``, ``"0"`` and ``"all"`` all mean "no target" -- the flag was not given, or the
    user explicitly cleared it. Anything else must be a plain positive integer; the upper
    bound (``N <= phase_count``) cannot be checked yet, since phases are not frozen at arm
    time, so ``commands/phases.py::run`` checks it again once they are.

    ``flag`` names the channel the text came from, for the rejection message only.
    ``commands/pausecmd.py`` reads the same grammar off a bare positional, so its rejection
    has to say ``pause "x"`` rather than blaming a ``--until`` the user never typed.

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
        raise _ArmFailure(f'{flag} "{raw}" is not a positive integer, "0" or "all"')
    try:
        value = int(raw)
    except ValueError as exc:
        raise _ArmFailure(f'{flag} "{raw}" is not a positive integer, "0" or "all"') from exc
    if value < 1:
        raise _ArmFailure(f'{flag} "{raw}" is not a positive integer, "0" or "all"')
    return value


def _resolve_plan(plan: str) -> str:
    """Validate the plan path and expand a leading ``~/``. Raises ``_ArmFailure``."""
    if not plan:
        raise _ArmFailure("no plan path was supplied (usage: /adversarial-review-loop:implement <path-to-plan.md>)")
    if not _PLAN_RE.fullmatch(plan):
        raise _ArmFailure(f'the plan path contains characters that are not safe to pass through shell expansion: "{plan}"')
    if plan.startswith("~/"):
        plan = f"{os.environ.get('HOME', '')}/{plan[2:]}"
    if not os.path.isfile(plan):
        raise _ArmFailure(f'the plan path does not resolve to an existing regular file: "{plan}"')
    if not os.access(plan, os.R_OK):
        raise _ArmFailure(f'the plan file is not readable: "{plan}"')
    return plan


#: The watchdog the shim runs every hook under, in the order ``arl_watchdog_pick`` tries them.
#: Kept here so ``arm`` and ``resume`` refuse up front rather than arming into a gate whose every
#: hook would fail closed; ``tests/unit/test_commands_arm.py`` asserts this list still matches the
#: shell's, since the two drifting apart is how the refusal would start lying.
WATCHDOGS: Final = ("timeout", "gtimeout", "perl")


#: Asks perl the one question that decides whether it can be the watchdog. Run with the same
#: scrubbed environment the shim uses, so the answer describes the interpreter the hook will
#: actually get rather than one dressed up by ``PERL5LIB``.
_PERL_MONOTONIC_PROBE: Final = "use Time::HiRes; eval { Time::HiRes::clock_gettime(Time::HiRes::CLOCK_MONOTONIC()); 1 } or exit 1;"
_PERL_PROBE_TIMEOUT: Final = 10


def _perl_has_monotonic() -> bool:
    """Whether this perl can measure a deadline against ``CLOCK_MONOTONIC``."""
    try:
        probe = subprocess.run(
            ["perl", "-e", _PERL_MONOTONIC_PROBE],
            env={**os.environ, "PERL5LIB": "", "PERL5OPT": "", "PERLLIB": ""},
            capture_output=True,
            timeout=_PERL_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _check_watchdog() -> None:
    """Refuse to arm when no usable outer watchdog exists. Raises ``_ArmFailure``.

    ``scripts/arl.sh`` runs every hook under one of :data:`WATCHDOGS`, and without one the gate
    cannot be bounded: the blocking ``flock`` in :mod:`arl.atomic` has no deadline of its own, so
    a wedged lock holder would hang the hook until Claude Code tore it down with nothing. The
    shim already fails closed by name in that case; refusing here means the user finds out while
    they are watching the slash command, instead of one denied tool call at a time.

    "Usable" is why this resolves the layer rather than just counting: the perl supervisor needs
    ``CLOCK_MONOTONIC``, because a deadline measured on the wall clock can be stretched past the
    host's own hook timeout by a backwards clock adjustment -- and a stretched deadline means the
    fail-closed response is never written at all. The shim refuses to run on a wall clock (125),
    so a perl without it would arm cleanly and then deny every tool call; this turns that into
    one refusal, here.

    Checked ahead of the ``ARL_REVIEWER_CMD`` seam below, and never skipped by it: that seam
    stands in for the reviewer, but the watchdog is needed whichever reviewer runs.
    """
    chosen = next((name for name in WATCHDOGS if paths.have(name)), None)
    if chosen is None:
        raise _ArmFailure(
            "no `timeout`, `gtimeout` or `perl` is on PATH, so the review gate cannot be bounded. "
            "Install GNU coreutils (`brew install coreutils` on macOS) or perl."
        )
    if chosen == "perl" and not _perl_has_monotonic():
        raise _ArmFailure(
            "`perl` is the only watchdog available and its Time::HiRes has no CLOCK_MONOTONIC, so "
            "the review gate's deadline could be stretched by a clock adjustment and its response "
            "lost. Install GNU coreutils (`brew install coreutils` on macOS)."
        )


def _check_reviewer(config: Config) -> None:
    """Refuse to arm when the reviewer cannot be reached. Raises ``_ArmFailure``.

    Arming with an unreachable reviewer would produce an activation whose every commit fails
    the review for an operational reason -- denials that look like findings. Better to say so
    now, while the user is watching the slash command's output.

    Three checks, narrowing: the ``harness`` names something this build implements, its binary
    is on ``PATH``, and -- only for a CLI that can enumerate them -- the model is one it
    reports. A harness whose ``probe_models`` answers ``None`` has no model list to check
    against, and that is not a reason to refuse: a name it does not know exits non-zero, which
    is an ``OP_FAILURE`` that blocks, so nothing is ever approved on the strength of a model
    that was never reached (Rule 1).

    The watchdog check rides along here because this is the one preflight both arming paths
    share -- ``resume`` reaches it at ``resume.py:738`` -- so a check added here cannot be armed
    around by resuming instead.
    """
    _check_watchdog()
    # Checked ahead of the test seam, and never skipped by it: the seam replaces the reviewer
    # *command*, not the harness -- session minting, id validation and every lease are still
    # sized from whatever `harness` names, so an unimplemented value would reach the review
    # path anyway. This is the "hard refusal at arm time, never a silent fallback".
    try:
        implementation = harness.selected(config)
    except harness.UnknownHarness as exc:
        raise _ArmFailure(f"{exc}. Set `harness` in .adversarial-review-loop.json or ARL_HARNESS to one of them.") from exc
    if os.environ.get("ARL_REVIEWER_CMD"):
        # The test seam stands in for the reviewer CLI entirely; probing the real binary would
        # make the suite depend on it being installed.
        return
    if not paths.have(implementation.binary):
        raise _ArmFailure(f"the `{implementation.binary}` binary is not on PATH, so the review gate cannot run")
    try:
        models = implementation.probe_models(reviewer_probe.MODELS_PROBE_TIMEOUT)
    except reviewer_probe.ProbeFailed as exc:
        raise _ArmFailure(f"could not list {implementation.name} models ({exc}); the reviewer is unreachable") from exc
    if models is None:
        return
    model = harness.model(config, implementation)
    if model not in models:
        raise _ArmFailure(
            f'the configured model "{model}" is not among the models {implementation.name} reports. '
            "Set `model` in .adversarial-review-loop.json or ARL_MODEL to one that is."
        )


def _baseline_tree(repo: str, head_commit: str) -> str:
    """The tree arming freezes as "reviewed so far": HEAD's, or the empty tree.

    ``git hash-object -t tree /dev/null`` rather than the well-known SHA-1 constant, because
    a repository may use SHA-256 and then the constant is simply the wrong id.
    """
    if head_commit:
        return gitsnap.head_tree(repo)
    return gitsnap.git_run(repo, ["hash-object", "-t", "tree", "/dev/null"]).stdout.decode("utf-8", "surrogateescape").rstrip("\n")


def _freeze_plan(plan: str, act_dir: Path) -> bytes:
    """Copy the plan into the activation directory, and answer the raw bytes written.

    The frozen copy is what every review is shown, so a plan edited afterwards cannot change
    what the gate believes was agreed. A copy that fails refuses to arm, where the shell's
    unchecked ``cp`` would have armed with no plan at all. The raw bytes are handed back so
    the caller can hash exactly what was written as revision 0, rather than reading the file
    a second time.
    """
    try:
        raw = Path(plan).read_bytes()
    except OSError as exc:
        raise _ArmFailure(f'the plan file could not be read: "{plan}" ({exc})') from exc
    try:
        write_private_atomic(
            act_dir / planrev.PLAN_FROZEN_NAME,
            raw.decode("utf-8", "surrogateescape"),
            root=paths.state_root(),
            errors="surrogateescape",
        )
    except OSError as exc:
        raise _ArmFailure(f"the plan could not be frozen into the activation directory ({exc})") from exc
    return raw


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
    # environment can itself outrank a --harness/--model/--variant override (defaults < user <
    # repo < overrides < env), so the override alone is not always what actually gets probed
    # and armed with -- only the fully-resolved config is.
    config = frozen.effective_config
    variant = config.as_str("variant")
    # `_check_reviewer` has already accepted the harness by the time this runs, so resolving
    # the model through it cannot raise here.
    reviewer = f"{config.as_str('harness')} {harness.model(config)}{f' (variant {variant})' if variant else ''}"
    # Named on screen at arm time, and by every other surface afterwards, because this is the
    # one input the repository supplies that becomes *instruction* to the reviewer. A guide can
    # steer attention, so a bad one makes reviews worse -- that is disclosed rather than
    # prevented, and disclosure only works if it is unmissable.
    #
    # The path itself goes through `guide.display_path`: it comes from `review_guide`, which is
    # repository-controlled, and this banner is both printed to a terminal and read by the model
    # as its instructions for what to do next. Raw, a newline in a filename writes further
    # bullets into it and an ESC sequence reaches the terminal.
    guide_line = (
        f"{guide.display_path(frozen.guide_path)} (frozen copy: {frozen.act_dir}/{guide.GUIDE_FROZEN_NAME}, sha256 {frozen.guide_sha256[:12]})"
        if frozen.guide_path
        else "none"
    )
    return f"""\
**adversarial-review-loop is ARMED for this worktree.**

- repository: {request.repo}
- plan: {frozen.plan} (frozen copy: {frozen.act_dir}/plan.frozen.md)
- baseline tree: {frozen.baseline}
- activation commit: {frozen.head_commit or "<empty repository>"}
- reviewer: {reviewer}
- review guide: {guide_line}
- pre-existing uncommitted work folded into phase 1: {"true" if frozen.allow_dirty else "false"}
- pause target: {frozen.until if frozen.until else "none"} (checked again once phases are frozen)
- block_severity: {config.as_str("block_severity")}
- final cumulative review at the end: {"enabled" if config.as_bool("final_review") else "disabled (final_review)"}

**Phases are not set yet, so every file mutation is currently denied.**

Do this first, and nothing else:

1. Read the frozen plan.
2. Split it into the smallest sensible sequence of phases, each of which ends in
   one commit.
3. Run exactly this, one `--phase` per phase, in order:

       {commands.plugin_root()}/scripts/arl.sh set-phases --phase "…" --phase "…"

After that the loop is: implement phase N -> `git add -A && git commit -m "…"`.
The commit is intercepted, the working tree goes to the reviewer named above, and the
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
    # Answer the session's intent marker: the arming command is running, which is the one
    # thing the marker exists to prove. Only when a pointer already exists -- a re-arm; a
    # first arm keeps its marker until its own success/failure record writes the pointer, so
    # a hard crash mid-arm still reads as "arming never ran". See resume._ack_intent.
    existing_pointer = pointer_read(session)
    if existing_pointer:
        pointer_write(session, existing_pointer)

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

    until = resolve_until(parsed.until)

    # The candidate overrides are built, and the config reloaded with them applied, *before*
    # the reviewer is probed: probing the stored config would validate a model this run will
    # not use, and pass `--model <invalid>` on the strength of a model nobody asked for. The
    # same argument is why `--harness` is in here: the binary checked and the model list
    # probed have to be the ones this activation will actually run against.
    overrides = {
        key: value
        for key, value in (("harness", parsed.harness), ("model", parsed.model), ("variant", parsed.variant), ("review_guide", parsed.guide))
        if value is not None
    }
    probe_config = config_module.load(repo, overrides=overrides)
    _check_reviewer(probe_config)

    # Resolved and read **before** the lock, so a guide the gate will not accept costs nothing:
    # nothing is created, nothing is frozen, and the activation directory is left exactly as it
    # was found. Only the write happens under the lock, beside the plan's. Read once here and
    # frozen from those same bytes, rather than re-read inside the lock -- re-reading would
    # validate one file and freeze another.
    guide_path = guide.resolve(probe_config, repo)
    guide_bytes: bytes | None = None
    if guide_path:
        try:
            guide_bytes = guide.read_source(guide_path)
        except guide.GuideRejected as exc:
            raise _ArmFailure(str(exc)) from exc
    # The harness is pinned to whatever was actually probed, whether or not `--harness` was
    # typed -- unlike `model` and `variant`, which stay unpinned and keep resolving through the
    # config layers on every round.
    #
    # It is the *only* key that decides which binary has to exist, so it is the only one whose
    # drift silently voids the check just above: a repo config edited to another harness
    # mid-activation leaves every later review failing with "that binary is not on PATH", an
    # operational failure that reads as the reviewer's fault. `.adversarial-review-loop.json`
    # travels with the tree under review and is not a trust boundary (AGENTS.md, "Adding
    # config"), so "the reviewer this activation was armed against" must not be something an
    # edit to it can change. Pinning is also what makes a mid-activation switch *explicit*:
    # `--harness` on `resume`, or `ARL_HARNESS`, which still outranks this overlay.
    #
    # **What is pinned is `probe_config`'s harness, not `parsed.harness`** -- the value read
    # back out of the fully-resolved config, not the flag that went in. `ARL_HARNESS`
    # outranks this overlay (`config.load`: overrides < env), so with the two disagreeing the
    # flag never reaches the probe: `_check_reviewer` above checked the *environment's*
    # harness. Storing the flag anyway would pin a harness nothing verified, and the moment
    # the variable left the environment the activation would run it. Same rule the banner
    # already follows for `--model`/`--variant`: report and record what was resolved.
    overrides["harness"] = probe_config.as_str("harness")

    head_commit = gitsnap.head_commit(repo)
    frozen = _Frozen(
        plan=plan,
        act_dir=state.act_dir,
        baseline=_baseline_tree(repo, head_commit),
        head_commit=head_commit,
        allow_dirty=allow_dirty,
        until=until,
        overrides=overrides,
        guide_path=guide_path if guide_bytes is not None else "",
        guide_sha256=hashlib.sha256(guide_bytes).hexdigest() if guide_bytes is not None else "",
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

        plan_bytes = _freeze_plan(plan, state.act_dir)
        guide_revisions = []
        if guide_bytes is not None:
            try:
                guide_revisions = [guide.freeze(guide_bytes, state.act_dir, guide.GUIDE_FROZEN_NAME, phase=1)]
            except guide.GuideRejected as exc:
                raise _ArmFailure(str(exc)) from exc

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
            # Revision 0, so the list is never empty and "revised" is exactly "more than one
            # entry" -- without this baseline there is nothing to compare a first revision
            # against, and `reviewer._range_text`'s disclosure has no honest form.
            plan_revisions=[{"at": now(), "phase": 1, "sha256": hashlib.sha256(plan_bytes).hexdigest(), "file": planrev.PLAN_FROZEN_NAME}],
            # Empty when no guide is in force, and that is the whole encoding of "off": there
            # is no revision 0 to synthesize, and `guide.verified_active` will not invent one.
            guide_path=frozen.guide_path,
            guide_revisions=guide_revisions,
        )
        state.mark_tree_approved(frozen.baseline)
        state.save()

    pointer_write(request.session, repo)
    commands.write_latest(repo, request.session)
    return frozen
