"""The slash command argument channel, end to end through a real shell.

Claude Code substitutes ``$ARGUMENTS`` into a skill body **textually**. The only transform it
applies to the substituted value neutralises ``!`` shell-exec markers; nothing escapes it for
the shell. Whatever the user typed after the slash command therefore becomes shell *source*,
and the skills used to place it inside a double-quoted argument. A reason containing a quote
ended that argument early and the rest of the sentence ran as a command -- a real run recorded
its reason as ``--reason phase`` and then reported ``the: comando non trovato`` -- and a
``$(...)`` or a backtick in it would have run without the quote's help.

A here-document with a quoted delimiter is the one shell construct whose body is never parsed:
no word splitting, no expansion, no metacharacters. That is the shape the skills use now, and
this module is the proof that it holds, because the alternative proof -- typing a hostile
argument into a live session -- cannot run in CI.

The two regexes below are Claude Code's own, read out of the 2.1.263 binary, and
:func:`_neutralise_bang` is its escape verbatim. That makes this a *contract* test: it fails if
a skill body stops parsing the way Claude Code parses it, and it would keep passing if Claude
Code changed its extractor underneath us. Nothing in this repository can detect that; what it
can detect, and what actually broke, is a skill body that hands the shell something it will
reparse.
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

import json
import re
import subprocess
from pathlib import Path
from typing import Final, NamedTuple

import pytest
from conftest import PLUGIN_ROOT, run_bootstrap

#: Claude Code's fenced shell-exec extractor. The capture is the *whole* block, which is what
#: makes a here-document possible at all: the block is handed to one shell as one script, not
#: line by line.
_FENCED: Final = re.compile(r"```!\s*\n?([\s\S]*?)\n?```")

#: Its inline extractor. Every skill that takes arguments must have moved off this form -- a
#: single line cannot carry a here-document, and every other single-line shape reparses.
_INLINE: Final = re.compile(r"(?:^|(?<=\s))!`([^`]+)`", re.MULTILINE)

#: Skills that interpolate ``$ARGUMENTS``, and the flag each one is expected to hand it to.
_ARGUMENT_SKILLS: Final = {
    "accept": "--reason-stdin",
    "config": "--args-stdin",
    "implement": "--args-stdin",
    "pause": "--args-stdin",
    "resume": "--args-stdin",
}

#: Argument strings a user could plausibly type, each of which the old double-quoted form
#: mangled or executed. The first is the one that actually happened.
_HOSTILE: Final = [
    'phase 1 content already approved in round 2; the divergence was the snapshot bug, fixed in the plugin"',
    '--reason "already approved"',
    "$(touch pwned) and `touch pwned`",
    "it's the reviewer's call",
    "a && touch pwned || true",
    "${HOME} $PATH \\ * ?",
    "plans/my plan.md --until 3",
]


def _body(skill: str) -> str:
    return (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def _neutralise_bang(value: str) -> str:
    """Claude Code's ``rv``: the only transform applied to a substituted argument value.

    It exists to stop an argument from *closing* one shell-exec marker and opening another. It
    is not an escape for the shell, which is the whole reason this module exists.
    """
    value = value.replace("`!", "` !").replace("!`", "! `")
    return re.sub(r"(^|\s)!", r"\1\\!", value, flags=re.MULTILINE)


def _commands(body: str, arguments: str) -> list[str]:
    """The shell commands Claude Code would run for this body, in order."""
    substituted = body.replace("$ARGUMENTS", _neutralise_bang(arguments))
    return [match.group(1).strip() for match in _FENCED.finditer(substituted)]


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    """A ``CLAUDE_PLUGIN_ROOT`` whose ``arl.sh`` records argv and stdin instead of running."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shim = scripts / "arl.sh"
    shim.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$#" > "$ARL_RECORD.argc"\nfor a in "$@"; do printf "%s\\0" "$a"; done > "$ARL_RECORD.argv"\ncat > "$ARL_RECORD.stdin"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return tmp_path


def _run(command: str, plugin_root: Path) -> tuple[list[str], str]:
    """Run one extracted block through a real ``bash``; return ``(argv, stdin)`` as seen."""
    record = plugin_root / "record"
    subprocess.run(
        ["bash", "-c", command],
        check=True,
        cwd=plugin_root,
        env={
            "PATH": "/usr/bin:/bin",
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "CLAUDE_SESSION_ID": "session-under-test",
            "ARL_RECORD": str(record),
            "HOME": str(plugin_root),
        },
    )
    argv = record.with_suffix(".argv").read_bytes().decode().split("\0")[:-1]
    return argv, record.with_suffix(".stdin").read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", sorted(_ARGUMENT_SKILLS))
def test_every_argument_taking_skill_uses_the_fenced_form(skill: str) -> None:
    """One fenced block, no inline marker anywhere else in the body.

    An inline ``!`cmd``` is a single line, and a single line cannot carry a here-document --
    so this is the check that the fix cannot be silently reverted by someone shortening the
    body back to one line.
    """
    body = _body(skill)
    assert len(_FENCED.findall(body)) == 1, f"{skill}: expected exactly one ```! block"
    assert not _INLINE.findall(body), f"{skill}: an inline !`cmd` marker cannot carry the here-document"
    assert _ARGUMENT_SKILLS[skill] in body, f"{skill}: the argument must reach the CLI as {_ARGUMENT_SKILLS[skill]}"
    assert '"$ARGUMENTS"' not in body, f"{skill}: a quoted $ARGUMENTS is reparsed by the shell"


@pytest.mark.parametrize("skill", sorted(_ARGUMENT_SKILLS))
@pytest.mark.parametrize("arguments", _HOSTILE)
def test_a_hostile_argument_reaches_the_cli_verbatim_and_runs_nothing(skill: str, arguments: str, plugin_root: Path) -> None:
    """The argument arrives on stdin exactly as typed, and nothing inside it executes."""
    commands = _commands(_body(skill), arguments)
    assert len(commands) == 1

    argv, stdin = _run(commands[0], plugin_root)

    assert stdin == _neutralise_bang(arguments) + "\n"
    assert argv[-1] == _ARGUMENT_SKILLS[skill], f"the argument leaked into argv: {argv}"
    assert not (plugin_root / "pwned").exists(), "a command substitution inside the argument ran"


def test_the_real_failure_would_have_been_caught(plugin_root: Path) -> None:
    """The exact string from the run that broke, against the shape that broke on it.

    Kept as its own test so the regression has a name. The old body is reconstructed here
    rather than read from a file, because the point is that no file contains it any more.
    """
    arguments = 'phase 1 content already approved in round 2; the divergence was the snapshot bug"'
    old = '```!\n${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh accept --reason "$ARGUMENTS"\n```'

    with pytest.raises(subprocess.CalledProcessError):
        _run(_commands(old, arguments)[0], plugin_root)

    argv, stdin = _run(_commands(_body("accept"), arguments)[0], plugin_root)
    assert argv == ["accept", "--reason-stdin"]
    assert stdin == arguments + "\n"


class _Equivalent(NamedTuple):
    """One command, an argument it *reports on*, and the phrase that report must contain.

    The argument is deliberately one the command answers about -- an unknown flag, an unknown
    key, two positionals where one is allowed -- so the two spellings agreeing means both were
    parsed, not merely both ignored.
    """

    sub: str
    fixed: list[str]
    argument: str
    expected: str


_EQUIVALENT: Final = [
    _Equivalent("arm", ["--session", "s"], "--nope", 'unrecognised flag "--nope"'),
    _Equivalent("resume", ["--session", "s"], "--nope", 'unrecognised flag "--nope"'),
    _Equivalent("pause", [], "9 9", "usage: /adversarial-review-loop:pause"),
    _Equivalent("config", [], "nosuchkey 1", 'unknown key "nosuchkey"'),
]


@pytest.mark.parametrize("case", _EQUIVALENT, ids=[row.sub for row in _EQUIVALENT])
def test_the_stdin_form_parses_exactly_as_the_argv_form(case: _Equivalent, git_repo: Path, clean_env: dict[str, str]) -> None:
    """``--args-stdin`` is the same parse as ``--args``, only delivered differently.

    The argv spelling stays for a real command line and for a skill body an older install
    still serves from its cache, so the two have to keep agreeing. Comparing whole outputs
    rather than a substring is deliberate: a stdin path that dropped the argument would still
    produce *an* answer, and only the argv run says which answer is right.
    """
    argv_form = case.argument.split() if case.sub == "config" else ["--args", case.argument]
    on_argv = run_bootstrap([case.sub, *case.fixed, *argv_form], cwd=git_repo, env=clean_env)
    on_stdin = run_bootstrap([case.sub, *case.fixed, "--args-stdin"], cwd=git_repo, env=clean_env, stdin=f"{case.argument}\n".encode())

    assert case.expected in on_argv.stdout + on_argv.stderr, "the fixture argument stopped being one the command reports on"
    assert on_stdin.stdout == on_argv.stdout
    assert on_stdin.stderr == on_argv.stderr
    assert on_stdin.returncode == on_argv.returncode


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ('verify_cmd "make test"', "make test"),
        ("verify_cmd make test", "make test"),
        ("verify_cmd 'make  test'", "make  test"),
        ('verify_cmd "make test', '"make test'),
    ],
    ids=["double-quoted", "bare", "single-quoted-inner-spaces", "unbalanced"],
)
def test_config_still_honours_the_quotes_a_user_types(typed: str, stored: str, git_repo: Path, clean_env: dict[str, str]) -> None:
    """``config`` is the one command whose old body interpolated ``$ARGUMENTS`` *unquoted*.

    The shell split it, so quoting a multi-word value worked and had to keep working once the
    string stopped reaching a shell. An unbalanced quote falls back to a whitespace split
    rather than erroring, so the last row keeps the stray quote as part of the value -- the
    user sees what they typed stored verbatim and can fix it, rather than a parser complaint
    about a character they may not have meant as a quote at all.
    """
    written = run_bootstrap(["config", "--args-stdin", "--repo"], cwd=git_repo, env=clean_env, stdin=f"{typed}\n".encode())
    assert written.returncode == 0, written.stdout + written.stderr

    document = json.loads((git_repo / ".adversarial-review-loop.json").read_text(encoding="utf-8"))
    assert document["verify_cmd"] == stored


def test_an_absent_argument_is_empty_rather_than_a_hang(git_repo: Path, clean_env: dict[str, str]) -> None:
    """A ``--args-stdin`` with nothing behind it must not wait for a here-document.

    The skills always supply one, but an operator typing the flag by hand at a terminal would
    otherwise get a process that never returns -- and for the hook entrypoints that share this
    interpreter, a gate that never returns is a gate that gets killed by the shim.
    """
    proc = run_bootstrap(["pause", "--args-stdin"], cwd=git_repo, env=clean_env, stdin=b"")

    bare = run_bootstrap(["pause"], cwd=git_repo, env=clean_env)

    assert proc.returncode == bare.returncode
    assert proc.stdout == bare.stdout
    assert proc.stderr == bare.stderr


def test_the_real_cli_survives_the_here_document(tmp_path: Path) -> None:
    """The receiving end, through the real shim rather than the stub.

    The tests above stop at the process boundary, so a here-document the shell delivers
    perfectly and the CLI then refuses to read would still pass them. This one runs
    ``scripts/arl.sh`` itself: nothing is armed in ``tmp_path``, so the accept refuses, and
    that refusal is the evidence -- the process started, read its argument and returned a
    message of its own rather than dying on a shell parse.
    """
    reason = 'a "quoted" reason; $(touch pwned) with `touch pwned` and $PATH'
    proc = subprocess.run(
        ["bash", "-c", f"{PLUGIN_ROOT}/scripts/arl.sh accept --reason-stdin <<'ARL-ARGUMENTS-EOF'\n{reason}\nARL-ARGUMENTS-EOF\n"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        text=True,
    )

    assert "adversarial-review-loop" in proc.stdout
    assert not (tmp_path / "pwned").exists(), "a command substitution inside the reason ran"
