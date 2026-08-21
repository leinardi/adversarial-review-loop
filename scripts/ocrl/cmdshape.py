r"""Command-shape classification: may this command be allowed to create a commit?

A translation of ``scripts/lib/cmdshape.sh``, tokenizer included -- bashlex replaces the
tokenizer in a later phase, kept separate on purpose so a parser swap is never entangled with
a language port. **The tokenizer and the deny-list are unchanged**, and no command the shell
accepted is refused here.

What did change is *detection*: which commands are sent to the gate at all. The shell matched
the raw string, so ``g\it commit`` and a ``set-phases`` buried in an ``&&`` chain both evaded
it -- see :func:`detection_form` and :func:`is_set_phases`, which document the two bypasses
and are the only deliberate divergences in this module.

The invariant that makes a hand-rolled tokenizer defensible is unchanged: it is safe only
*because* the deny-list rejects nearly the whole shell grammar before tokenizing. Anything
that could run a second program or touch a file after the snapshot was taken -- ``$``,
backticks, ``;``, ``|``, redirection, subshells, braces, unquoted globs, a bare ``&``,
newlines, comments -- is refused outright, so what reaches the token loop is a flat sequence
of words joined by ``&&``. Widening the deny-list without replacing the tokenizer breaks
that, and the tokenizer will not tell you.

Every rejection raises ``CommandShapeError``; its message is the explanation the shell kept
in ``OCRL_CMD_ERROR`` and the gate shows to the model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from ocrl.errors import OcrlError

__all__ = [
    "CommandShapeError",
    "detection_form",
    "is_escape",
    "is_set_phases",
    "mentions_commit",
    "mentions_reset",
    "reset_target",
    "tokenize",
    "validate_commit",
]

#: Longest ``&&`` chain accepted. A commit sequence is short by nature; anything longer is
#: a script, and a script is not something this gate can reason about.
MAX_SEGMENTS: Final = 8


class CommandShapeError(OcrlError):
    """The command could not be shown to be a safe commit sequence.

    Caught by the gate, which denies with this message. Uncaught it still denies, through
    the fail-closed guard: there is no path on which an unclassifiable command is allowed.
    """


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

#: Metacharacters that can run a second program or move data between files.
_METACHARACTERS: Final = ";|<>(){}"

#: Glob characters, refused unquoted: the set they expand to is decided by the filesystem
#: at exec time, not by anything the gate can see when it decides.
_GLOB_CHARACTERS: Final = "*?[]"


def tokenize(command: str) -> list[str]:  # noqa: PLR0912, PLR0915 - one branch per shell `case` arm; splitting it would hide the deny-list
    """Split a command into words, with ``&&`` surviving as its own token.

    Quote-aware and hostile: the checks below are the security boundary, not tidiness.

    Deliberately one flat loop, mirroring the shell's ``case`` arm for arm. A reviewer has
    to be able to read this against ``cmdshape.sh`` and see that nothing was dropped; that
    matters more here than a branch count.
    """
    if "$" in command:
        raise CommandShapeError('the command contains "$" (variable or command substitution)')
    if "`" in command:
        raise CommandShapeError("the command contains a backtick (command substitution)")
    if "\n" in command or "\r" in command:
        raise CommandShapeError("the command spans multiple lines")

    tokens: list[str] = []
    token: list[str] = []
    started = False
    quote = ""
    index = 0
    length = len(command)

    while index < length:
        char = command[index]
        if quote:
            if char == quote:
                quote = ""
            else:
                token.append(char)
            started = True
        elif char in ("'", '"'):
            quote = char
            started = True
        elif char in (" ", "\t"):
            if started:
                tokens.append("".join(token))
                token = []
                started = False
        elif char == "\\":
            index += 1
            # A trailing backslash escapes nothing; slicing yields "" where the shell's
            # ${s:i:1} did the same.
            token.append(command[index : index + 1])
            started = True
        elif char == "&":
            if command[index + 1 : index + 2] != "&":
                raise CommandShapeError('the command backgrounds a process ("&")')
            if started:
                tokens.append("".join(token))
                token = []
                started = False
            tokens.append("&&")
            index += 1
        elif char in _METACHARACTERS:
            raise CommandShapeError(f'the command contains the shell metacharacter "{char}" (pipeline, redirection, subshell or sequencing)')
        elif char in _GLOB_CHARACTERS:
            raise CommandShapeError(f'the command contains an unquoted glob character "{char}"')
        elif char == "#":
            if not started:
                raise CommandShapeError("the command contains a comment")
            token.append(char)
        else:
            token.append(char)
            started = True
        index += 1

    if quote:
        raise CommandShapeError("the command has an unterminated quote")
    if started:
        tokens.append("".join(token))
    return tokens


# --------------------------------------------------------------------------
# Cheap detection: does this command try to create a commit at all?
#
# Deliberately loose -- anything flagged here still has to pass full validation. The
# character classes spell out POSIX [[:space:]] minus the newline, because the shell ran
# these as `grep -E` over a line at a time.
# --------------------------------------------------------------------------

_SPACE: Final = r"[ \t\v\f\r]"
_NON_SPACE: Final = r"[^ \t\v\f\r\n]"
_BEFORE: Final = r"(^|[ \t\v\f\r;&|(])"

#: ``git``, however it is spelled as a path. The shell matched the bare word, so
#: ``/usr/bin/git commit -m x`` -- the same program, and what a ``PATH``-wary caller writes --
#: matched nothing and was passed through ungated. Any word ending in ``/git`` counts; the
#: strict validator then refuses the non-canonical spelling, which is the safe direction.
_GIT: Final = r"(?:[^ \t\v\f\r\n;&|()]*/)?git"

_COMMIT_RE: Final = re.compile(rf"{_BEFORE}{_GIT}({_SPACE}+-{_NON_SPACE}+)*{_SPACE}+commit({_SPACE}|$)", re.MULTILINE)
_RESET_RE: Final = re.compile(rf"{_BEFORE}{_GIT}({_SPACE}+-{_NON_SPACE}+)*{_SPACE}+reset({_SPACE}|$)", re.MULTILINE)
_ESCAPE_RE: Final = re.compile(rf"ocrl(\.sh)?{_SPACE}+(finish|deactivate)({_SPACE}|$)", re.MULTILINE)


def detection_form(command: str) -> str:
    r"""Undo backslash escapes and quoting, so detection reads a command as bash will.

    **This is the fix for a real bypass, not a tidy-up.** The three detectors below matched
    the raw string, so ``g\it commit -m x`` -- which bash runs as ``git commit`` -- contained
    no literal ``git`` and was not detected as a commit at all. ``pretool`` then passed it
    straight through to be executed, ungated. The same trick hid ``g\it reset --hard`` from
    the reset guard and ``oc\rl.sh finish`` from the Rule 4 denial. Quoting does it too:
    ``'g'it commit`` and ``g"i"t commit`` are both ``git commit`` to bash.

    So the word-removal half of bash's expansion is applied first: a backslash outside single
    quotes escapes the next character, quote delimiters vanish, and everything else survives
    in place.

    **Used for detection only, never for validation.** ``tokenize`` still reads the raw
    string, because that is where the deny-list lives. Over-detecting is the safe direction:
    a false positive routes the command into the commit gate, which either proves it is one
    of the three accepted shapes or denies it.

    **What this still does not see: substitution.** ``$(echo git) commit`` and ``$'g\x69t'``
    produce a command name no textual pass can predict, and denying every command containing
    ``$`` would deny the builds and tests the loop exists to run. Such a commit is not
    approved -- it simply never reaches the gate -- and it is caught at turn end, where the
    phase has not advanced and the Stop gate blocks on the outstanding phase before the
    cumulative review runs. Closing it needs the real parser Phase 7 vendors.
    """
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                index += 1
                out.append(command[index : index + 1])
            else:
                out.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char == "\\":
            index += 1
            out.append(command[index : index + 1])
        else:
            out.append(char)
        index += 1
    return "".join(out)


def mentions_commit(command: str) -> bool:
    return _COMMIT_RE.search(detection_form(command)) is not None


def mentions_reset(command: str) -> bool:
    return _RESET_RE.search(detection_form(command)) is not None


def is_escape(command: str) -> bool:
    """The user-only escapes. Claude's own route -- Bash -- is denied elsewhere (Rule 4)."""
    return _ESCAPE_RE.search(detection_form(command)) is not None


def is_set_phases(command: str, entrypoint: str) -> bool:
    """Is this command **exactly** ``<entrypoint> set-phases …`` and nothing else?

    ``set-phases`` is the single command permitted while the phase list is still unfrozen --
    a state in which nothing may change the repository -- so an ``allow`` here runs a program
    at a moment when everything else is denied. Two things therefore have to hold, and the
    shell checked neither.

    **It must be the whole command.** The shell used
    ``grep 'ocrl\\(\\.sh\\)\\?[[:space:]]\\+set-phases'``, a substring match, so
    ``git add -A && git commit -m x && .../ocrl.sh set-phases --phase x`` was *allowed*: the
    commit ran before phases were ever frozen, with no snapshot and no review. The command is
    tokenized instead -- with the real tokenizer, which refuses ``$``, backticks, ``;``,
    ``|``, redirection, subshells, globs and a bare ``&`` outright -- and must be a single
    segment.

    **It must be this gate's own script**, matched as the exact path the caller passes in,
    not by basename. A basename test trusts any executable called ``ocrl``, and the
    repository under review can ship one: ``./ocrl set-phases --phase x`` would then be
    allowed to run arbitrary code at the one moment nothing else may run at all. ``arm``
    prints this exact path for the model to copy, so nothing legitimate is lost.

    The arguments after ``set-phases`` are deliberately not constrained. They are read by
    :mod:`ocrl.commands.phases`, which can freeze a phase list and nothing else -- there is
    no argument to that command that touches the repository under review.
    """
    try:
        tokens = tokenize(command)
    except CommandShapeError:
        return False
    if len(tokens) < 2 or "&&" in tokens:
        return False
    return tokens[0] == entrypoint and tokens[1] == "set-phases"


# --------------------------------------------------------------------------
# Expansion: words this gate cannot read
# --------------------------------------------------------------------------

_EXPANSIONS: Final = {
    "$(": "a command substitution ($( … ))",
    "${": "a variable expansion (${ … })",
    "$'": "an ANSI-C quoted string ($' … ')",
}


def unresolved_expansion(command: str) -> str:
    """Name the expansion that makes this command's words unknowable, or return "".

    Detection reads text, and ``$(printf git) commit -m x`` contains no word this or any
    other textual pass can resolve to ``git``. It runs ``git commit`` all the same. A real
    parser does not fix this either: bashlex would report a command whose *name is a
    substitution node*, and the only sound answer to that is still refusal.

    So the deny-list absorbs it, which is the same trade the tokenizer already makes -- the
    parser is defensible only because almost the whole grammar is refused before it runs.
    A ``$`` or a backtick outside single quotes is refused while the gate is enforcing;
    inside single quotes both are literal to bash and are left alone, so ordinary
    ``grep '$foo'`` still works.

    This does not make the gate a complete barrier, and nothing textual can: ``eval``,
    ``xargs``, ``env`` and a shell function all reach ``git`` with a literal command name.
    What catches those is ``confirm-commit`` noticing afterwards that HEAD moved to a tree no
    review ever approved.
    """
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = ""
        elif quote == '"':
            if char == "\\":
                index += 1
            elif char == '"':
                quote = ""
            elif char in "$`":
                return _expansion_at(command, index)
        elif char == "\\":
            index += 1
        elif char in ("'", '"'):
            quote = char
        elif char in "$`":
            return _expansion_at(command, index)
        index += 1
    return ""


def _expansion_at(command: str, index: int) -> str:
    if command[index] == "`":
        return "a backtick (command substitution)"
    return _EXPANSIONS.get(command[index : index + 2], "a variable expansion ($ … )")


# --------------------------------------------------------------------------
# Per-subcommand flag allowlists (default-deny on anything unknown)
# --------------------------------------------------------------------------

_SHORT_OK: Final[dict[str, str]] = {
    "add": "Auvn",
    "status": "sbzuv",
    "commit": "asqvnmS",
}

#: Short flags whose remainder -- or next token -- is a value.
_SHORT_TAKES_VALUE: Final = frozenset(
    {
        ("commit", "m"),
        ("commit", "S"),  # optional attached key id
        ("status", "u"),  # -uall / -uno, value optional
    }
)

_LONG_OK: Final[dict[str, frozenset[str]]] = {
    "add": frozenset({"--all", "--no-ignore-removal", "--ignore-removal", "--update", "--verbose", "--dry-run"}),
    "status": frozenset({"--short", "--long", "--branch", "--no-branch", "--verbose", "--ignored", "--porcelain", "--untracked-files", "--null"}),
    "commit": frozenset(
        {
            "--all",
            "--signoff",
            "--no-signoff",
            "--quiet",
            "--verbose",
            "--no-verify",
            "--verify",
            "--allow-empty",
            "--allow-empty-message",
            "--no-post-rewrite",
            "--no-gpg-sign",
            "--gpg-sign",
            "--trailer",
            "--status",
            "--no-status",
        }
    ),
}

#: Long options accepted with an attached value.
_LONG_OK_PREFIXES: Final[dict[str, tuple[str, ...]]] = {
    "add": (),
    "status": ("--ignored=", "--porcelain=", "--untracked-files="),
    "commit": ("--gpg-sign=", "--author=", "--date=", "--message=", "--trailer=", "--cleanup="),
}

#: Long options that consume the following token as their value.
_LONG_CONSUMES_NEXT: Final = frozenset({"--trailer", "--untracked-files"})


def _short_ok(sub: str, char: str) -> bool:
    return char in _SHORT_OK.get(sub, "")


def _short_takes_value(sub: str, char: str) -> bool:
    return (sub, char) in _SHORT_TAKES_VALUE


def _short_value_required(sub: str, char: str) -> bool:
    return (sub, char) == ("commit", "m")


def _long_ok(sub: str, option: str) -> bool:
    return option in _LONG_OK.get(sub, frozenset()) or option.startswith(_LONG_OK_PREFIXES.get(sub, ()))


def _long_reason(option: str) -> str:  # noqa: PLR0911 - one return per shell `case` arm, matched exactly rather than by prefix
    """A specific explanation for options that are refused for a specific reason.

    Matched exactly, ``--x=`` forms included, rather than by splitting on ``=``: an option
    the shell did not list in a given form fell through to the generic "not on the
    allowlist" message, and that wording is asserted on.

    Also consulted for short flags, as ``-<char>``. None of the keys below are single-dash
    today, so a rejected short flag always gets the generic message -- as in the shell.

    **One shell bug is not reproduced.** ``_ocrl_long_reason`` emitted these strings with
    ``printf '<reason>'``, so the four whose text begins with ``--`` -- ``--file``,
    ``--template``, ``--pathspec-from-file`` and ``--chmod`` -- were read by ``printf`` as
    *options*, and it answered with a usage error on stderr and nothing on stdout. The
    caller saw an empty reason and fell back to the generic "not on the allowlist" message.
    The verdict was never affected, in either direction; only the explanation was lost, and
    it is the explanation that tells the model what to do instead. ``tests/unit`` asserts
    this difference explicitly rather than letting it pass as drift.
    """
    if option == "--amend":
        return "amending rewrites the commit that was already reviewed, so the reviewed tree can no longer be verified against it"
    if option in ("--only", "--include"):
        return f"partial commits ({option}) commit something other than the reviewed snapshot"
    if option in ("--interactive", "--patch"):
        return "interactive staging cannot be reconciled with the snapshot that was reviewed"
    if option == "--file" or option.startswith("--file="):
        return "--file reads the message from a path that may change after the snapshot"
    if option in ("--fixup", "--squash") or option.startswith(("--fixup=", "--squash=")):
        return f"{option} produces a commit that is meant to be rewritten later"
    if option == "--template" or option.startswith("--template="):
        return "--template opens an editor, which stalls the hook"
    if option == "--pathspec-from-file" or option.startswith("--pathspec-from-file="):
        return "--pathspec-from-file stages a set the gate cannot see"
    if option == "--chmod" or option.startswith("--chmod="):
        return "--chmod changes modes outside the snapshot"
    if option in ("--force", "--renormalize"):
        return f"{option} can stage content the snapshot deliberately excluded"
    return ""


# --------------------------------------------------------------------------
# Segment validation
# --------------------------------------------------------------------------


def _check_subcommand(sub: str) -> None:
    if sub.startswith("-"):
        raise CommandShapeError(
            f'git global options before the subcommand ("{sub}") are not allowed: '
            "-C, -c, --git-dir and --work-tree can retarget the commit away from the reviewed worktree"
        )
    if sub in ("add", "status", "commit"):
        return
    if sub == "rm":
        raise CommandShapeError(
            "git rm deletes from the working tree after the snapshot was taken; run it as a separate command and let the next gate pick it up"
        )
    if sub == "diff":
        raise CommandShapeError("git diff can write files (--output, --ext-diff) and buys nothing in a commit sequence")
    raise CommandShapeError(f"git {sub} is not one of the allowed subcommands (add, status, commit)")


def _consume_short_cluster(sub: str, tokens: Sequence[str], index: int) -> int:
    """Validate one ``-abc`` cluster, returning the index its value consumption reached."""
    token = tokens[index]
    position = 1
    while position < len(token):
        char = token[position]
        if not _short_ok(sub, char):
            reason = _long_reason(f"-{char}")
            if reason:
                raise CommandShapeError(f"git {sub} -{char} is not allowed: {reason}")
            raise CommandShapeError(f"git {sub} -{char} is not on the allowlist for this gate")
        if _short_takes_value(sub, char):
            if position < len(token) - 1:
                break  # the rest of the token is the value
            if _short_value_required(sub, char):
                index += 1
                if index >= len(tokens):
                    raise CommandShapeError(f"git {sub} -{char} is missing its value")
            break
        position += 1
    return index


def _validate_segment(tokens: Sequence[str]) -> None:  # noqa: PLR0912 - one branch per token shape the shell distinguishes
    """Prove one ``&&``-separated segment cannot change working-tree content."""
    if not tokens:
        raise CommandShapeError("the command contains an empty segment")
    if tokens[0] != "git":
        raise CommandShapeError(f'segment starts with "{tokens[0]}"; only git add, git status and git commit may appear alongside a commit')
    if len(tokens) < 2:
        raise CommandShapeError('a bare "git" with no subcommand')

    sub = tokens[1]
    _check_subcommand(sub)

    # A pathspec on `git commit` is a partial commit: it commits something other than the
    # tree that was reviewed.
    allow_positional = sub != "commit"

    index = 2
    after_ddash = False
    while index < len(tokens):
        token = tokens[index]
        if after_ddash:
            if not allow_positional:
                raise CommandShapeError(
                    f'git commit with a pathspec ("{token}") is a partial commit; it would commit something other than the reviewed snapshot'
                )
        elif token == "--":
            after_ddash = True
        elif token.startswith("--"):
            reason = _long_reason(token)
            if reason:
                raise CommandShapeError(f"git {sub} {token} is not allowed: {reason}")
            if not _long_ok(sub, token):
                raise CommandShapeError(f"git {sub} {token} is not on the allowlist for this gate")
            if token in _LONG_CONSUMES_NEXT:
                index += 1  # consumes its value
        elif token.startswith("-") and len(token) > 1:
            index = _consume_short_cluster(sub, tokens, index)
        elif token == "-":
            raise CommandShapeError('a bare "-" argument reads from stdin')
        elif not allow_positional:
            raise CommandShapeError(
                f'git commit with a pathspec ("{token}") is a partial commit; it would commit something other than the reviewed snapshot'
            )
        index += 1


def validate_commit(command: str) -> None:
    """Accept a commit sequence, or raise ``CommandShapeError`` explaining the refusal."""
    tokens = tokenize(command)
    if not tokens:
        raise CommandShapeError("empty command")

    segment: list[str] = []
    saw_commit = False
    segments = 0

    for token in tokens:
        if token != "&&":
            segment.append(token)
            continue
        segments += 1
        if segments > MAX_SEGMENTS:
            raise CommandShapeError("too many chained segments; keep the commit sequence short")
        _validate_segment(segment)
        saw_commit = saw_commit or _is_commit_segment(segment)
        segment = []

    _validate_segment(segment)
    saw_commit = saw_commit or _is_commit_segment(segment)

    if not saw_commit:
        raise CommandShapeError('no "git commit" segment found')


def _is_commit_segment(segment: Sequence[str]) -> bool:
    return len(segment) > 1 and segment[1] == "commit"


def reset_target(command: str) -> str:
    """The target of a bounded ``git reset --soft <target>``, used during reconcile.

    Only ``--soft`` is permitted: every other mode discards working-tree content, which is
    the content the gate is meant to be reviewing.
    """
    tokens = tokenize(command)
    if "&&" in tokens:
        raise CommandShapeError("the recovery reset must be a single command on its own")
    if len(tokens) < 3 or tokens[0] != "git" or tokens[1] != "reset":
        raise CommandShapeError('not a plain "git reset" command')

    target = ""
    soft = False
    for token in tokens[2:]:
        if token == "--soft":
            soft = True
        elif token in ("--quiet", "-q"):
            continue
        elif token in ("--hard", "--mixed", "--merge", "--keep"):
            raise CommandShapeError(f"git reset {token} would discard working-tree content; only --soft is permitted during reconcile")
        elif token.startswith("-"):
            raise CommandShapeError(f"git reset {token} is not permitted during reconcile")
        elif target:
            raise CommandShapeError("git reset accepts exactly one target during reconcile")
        else:
            target = token

    if not soft:
        raise CommandShapeError('only "git reset --soft <target>" is permitted during reconcile')
    if not target:
        raise CommandShapeError("git reset --soft needs an explicit target during reconcile")
    return target
