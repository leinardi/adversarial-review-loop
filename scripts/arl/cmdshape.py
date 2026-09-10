"""Command-shape classification: may this command be allowed to create a commit?

Two layers decide, and only the first is policy. **The deny-list**
(:func:`_deny_shell_grammar`) refuses nearly the whole shell grammar -- ``$``, backticks,
``;``, ``|``, redirection, subshells, braces, unquoted globs, a bare ``&``, newlines,
comments -- and runs first. What survives is a flat sequence of words joined by ``&&``, and
**bashlex**, vendored under :mod:`arl._vendor`, turns that into words.

Detection -- which commands reach the gate at all -- is still textual and cannot be
otherwise: it runs on commands the deny-list *would* refuse, so there may be nothing
parseable to work with. See :func:`detection_form` and :func:`is_set_phases`.

:func:`unresolved_expansion` runs on **every** Bash call rather than only the commit path,
and is not the boundary either: it exists so textual detection cannot go blind on a command
*name*, and is scoped to exactly that.

**The deny-list and the parser are one design.** Relaxing what is accepted without re-reading
:func:`_words` is the specific change that breaks this module, and neither will tell you. See
``docs/design/deny-list-and-parser.md``.
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

import contextlib
import re
import signal
from collections.abc import Iterator, Sequence
from types import FrameType
from typing import Any, Final, NoReturn

from arl.errors import OcrlError
from arl.util import log, log_exception

__all__ = [
    "PARSE_TIMEOUT_SECONDS",
    "CommandShapeError",
    "CommandShapeTimeout",
    "ShellMetacharacterError",
    "detection_form",
    "head_ref_deletion",
    "is_escape",
    "is_set_phases",
    "mentions_commit",
    "mentions_reset",
    "mentions_update_ref",
    "reset_target",
    "set_phases_refusal",
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


class ShellMetacharacterError(CommandShapeError):
    """One of :data:`_METACHARACTERS` was found outside quotes.

    A ``CommandShapeError`` carrying the character, so every caller already denies on it and
    the message is unchanged; the character is exposed so :func:`validate_commit` can say
    something more useful about the two that a commit sequence actually gets written with.
    Adding this class refuses nothing new -- see :func:`_deny_shell_grammar`.
    """

    def __init__(self, message: str, character: str) -> None:
        super().__init__(message)
        self.character = character


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

#: Metacharacters that can run a second program or move data between files.
_METACHARACTERS: Final = ";|<>(){}"

#: Glob characters, refused unquoted: the set they expand to is decided by the filesystem
#: at exec time, not by anything the gate can see when it decides.
_GLOB_CHARACTERS: Final = "*?[]"


def tokenize(command: str) -> list[str]:
    """Split a command into words, with ``&&`` surviving as its own token.

    Two layers, and the order **is** the design: :func:`_deny_shell_grammar` refuses nearly the
    whole shell grammar, and bashlex splits whatever survives.

    Keeping the deny-list in front of the parser is not belt-and-braces. bashlex parses the whole
    language, so alone it would hand back a clean AST for a pipeline or a subshell and leave the
    gate deciding node by node which constructs cannot reach the filesystem -- the same policy,
    re-expressed against a grammar large enough to hide a mistake in.

    What the parser buys is word splitting, quote removal and escape handling as bash's own rules
    rather than re-derived here. Two consequences, both intended: a command bashlex cannot parse
    is refused rather than tokenized on a guess (the hand-rolled loop read a trailing backslash as
    if it were not there), and a syntactically broken command's refusal is now worded by the
    parser. The verdict is the same in every such case.

    See ``docs/design/deny-list-and-parser.md``.
    """
    _deny_shell_grammar(command)
    # bashlex has no concept of an empty program: `parse("")` walks off the end of its own
    # AST. The shell tokenizer returned no tokens, and every caller already has a message
    # for that, so it stays their decision rather than becoming a parse error here.
    if not command.strip(" \t"):
        return []
    return _words(_parse(command))


def _deny_shell_grammar(command: str) -> None:  # noqa: PLR0912 - one branch per shell `case` arm; splitting it would hide the deny-list
    r"""Refuse everything that could run a second program or touch a file after the snapshot.

    **This is the security boundary.** Deliberately one flat loop rather than split up, so a
    reviewer can read it top to bottom and see that nothing was dropped;
    ``tests/unit/test_cmdshape.py`` is what proves that claim.

    Quote-aware, which is the whole subtlety: ``git commit -m "a;b"`` is a legitimate message and
    ``git commit -m x; rm -rf /`` is two commands. ``started`` is kept because a ``#`` is a
    comment only where a word is not already open.

    "Exactly as bash does" is load-bearing in **both** directions. Reading a quote as still open
    where bash has closed it lets a metacharacter through and is the dangerous mistake; reading it
    as closed where bash has not is the safe one, and it is what a missing backslash arm did --
    refusing ``-m "handle \"this\""``, which bash accepts.

    ``&>`` and ``&>>`` are named as the redirection they are rather than as backgrounding, which
    is what lets :func:`validate_commit` give them the redirect denial. Both were refused before
    and are refused now; only the message moved.
    """
    if "$" in command:
        raise CommandShapeError('the command contains "$" (variable or command substitution)')
    if "`" in command:
        raise CommandShapeError("the command contains a backtick (command substitution)")
    if "\n" in command or "\r" in command:
        raise CommandShapeError("the command spans multiple lines")

    started = False
    quote = ""
    index = 0
    length = len(command)

    while index < length:
        char = command[index]
        if quote:
            if quote == '"' and char == "\\":
                # Inside double quotes a backslash escapes the next character, so
                # `git commit -m "handle \"quoted\" input"` is one word and not two quotes
                # with a bare `quoted` between them. Without this arm the `\"` *closed* the
                # quote, everything after it was scanned as unquoted, and an ordinary commit
                # message or phase description was refused for an "unterminated quote" -- or
                # for a metacharacter that was quoted all along. Single quotes are untouched:
                # the shell has no escape inside them, and neither does this.
                index += 2
                started = True
                continue
            if char == quote:
                quote = ""
            started = True
        elif char in ("'", '"'):
            quote = char
            started = True
        elif char in (" ", "\t"):
            started = False
        elif char == "\\":
            # A trailing backslash escapes nothing; the shell's ${s:i:1} yielded "" here and
            # so does running off the end of the string.
            index += 1
            started = True
        elif char == "&":
            following = command[index + 1 : index + 2]
            if following == ">":
                # `&>` and `&>>` are bash's "redirect stdout and stderr" operators, not a
                # background `&` that happens to be followed by one. The refusal is the same
                # either way -- both are refused, and were before this arm existed -- but the
                # `&` message named the wrong construct, and a redirection reported as
                # backgrounding cannot reach the commit path's redirect denial below.
                operator = "&>>" if command[index + 2 : index + 3] == ">" else "&>"
                raise ShellMetacharacterError(f'the command contains the shell metacharacter "{operator}" (redirection)', operator)
            if following != "&":
                raise CommandShapeError('the command backgrounds a process ("&")')
            started = False
            index += 1
        elif char in _METACHARACTERS:
            # A `ShellMetacharacterError`, not a bare `CommandShapeError`: same message, same
            # characters, same order -- it only carries the character along so the commit path
            # can add context to a pipe or a redirection. Nothing here refuses more or less.
            raise ShellMetacharacterError(
                f'the command contains the shell metacharacter "{char}" (pipeline, redirection, subshell or sequencing)', char
            )
        elif char in _GLOB_CHARACTERS:
            raise CommandShapeError(f'the command contains an unquoted glob character "{char}"')
        elif char == "#":
            if not started:
                raise CommandShapeError("the command contains a comment")
        else:
            started = True
        index += 1

    if quote:
        raise CommandShapeError("the command has an unterminated quote")


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


class CommandShapeTimeout(CommandShapeError):
    """The parse did not finish inside its deadline.

    A ``CommandShapeError``, so every caller already denies on it; named separately so a
    test can tell a deadline apart from a refusal, and a reader can tell that a hang is a
    handled outcome rather than a hope.
    """


#: Wall-clock ceiling on one parse, enforced in-process.
#:
#: The shim in ``scripts/arl.sh`` already runs each hook under ``timeout``, but that layer
#: cannot answer: when it fires it kills this process *and* the shim's own command
#: substitution, so the shim's fallback is what reaches Claude. This deadline is the layer
#: that can still emit the event's real denial, so it sits far below the shim's ceiling
#: (50 s on the tightest hook). bashlex is pure Python, so SIGALRM is delivered between
#: bytecodes and a parse is genuinely interruptible.
#:
#: Nothing in the corpus takes longer than a millisecond; five seconds is "the parser is
#: wedged", not "the parser is busy".
PARSE_TIMEOUT_SECONDS: Final = 5.0


def _on_parse_alarm(signum: int, frame: FrameType | None) -> NoReturn:
    raise CommandShapeTimeout(f"the command took longer than {PARSE_TIMEOUT_SECONDS:g}s to parse, so it cannot be shown to be a safe commit")


@contextlib.contextmanager
def _parse_deadline() -> Iterator[None]:
    """Run the body under a SIGALRM deadline, if this process can have one.

    Only the main thread of the main interpreter may install a signal handler. Under a hook
    that is always where we are; a caller that imports this module into a thread gets no
    deadline rather than an exception, and the shim's ``timeout`` remains its backstop.

    **This assumes the gate is the only user of ``ITIMER_REAL`` in the process.** A process
    has exactly one of them, so arming this one cancels any timer already running, and the
    ``finally`` clears rather than restores it -- restoring a partly-elapsed timer would be
    a guess at how much of it was left. The assumption holds by construction: the gate is
    started by ``scripts/arl-bootstrap.py``, runs one subcommand and exits, and nothing
    else in ``arl`` touches ``signal``. It is written down because it is the kind of thing
    an added timer elsewhere would break silently -- the parse would still be bounded, the
    other timer would simply never fire.
    """
    try:
        previous = signal.signal(signal.SIGALRM, _on_parse_alarm)
    except ValueError:
        log("no parse deadline: not the main thread")
        yield
        return
    try:
        signal.setitimer(signal.ITIMER_REAL, PARSE_TIMEOUT_SECONDS)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _parse(command: str) -> Any:
    """Parse one command with bashlex and insist it is a single statement, or raise.

    The commit path's entry point. :func:`unresolved_expansion` uses :func:`_parse_trees`
    directly: "more than one statement" is a *commit-shape* policy, and a pre-check that runs
    on every Bash call has no business enforcing it.
    """
    trees = _parse_trees(command)
    if len(trees) != 1:
        raise CommandShapeError("the command is more than one statement")
    return trees[0]


def _parse_trees(command: str) -> Sequence[Any]:
    """Parse a command with bashlex, or raise ``CommandShapeError`` saying why not.

    bashlex is imported here rather than at module scope: it builds its LALR tables at
    import time, which costs ~55 ms, and only a command that already looks like a commit --
    or that a textual scan has already flagged as unreadable -- ever reaches this function.
    A ``Read`` must not pay for a parser it never runs.

    Every failure is a refusal. ``ParsingError`` is the ordinary one -- the command is not
    valid bash -- and anything else coming out of the parser is a defect in it, which is
    exactly the case where guessing at the words would be worst.
    """
    from arl._vendor import bashlex  # noqa: PLC0415 - see the docstring: ~55 ms of table construction
    from arl._vendor.bashlex import errors as bashlex_errors  # noqa: PLC0415

    try:
        with _parse_deadline():
            trees = bashlex.parse(command)
    except CommandShapeError:
        raise  # the deadline; it already says what happened
    except bashlex_errors.ParsingError as exc:
        raise CommandShapeError(f"the command is not valid shell syntax ({exc})") from exc
    except Exception as exc:
        # bashlex is a parser generator's output over hostile input: an unexpected
        # exception is a bug in it, not a verdict. Say so on stderr and deny.
        log_exception()
        raise CommandShapeError(f"the command could not be parsed ({type(exc).__name__})") from exc

    return _nodes(trees, "a parse that is not a list of nodes")


def _nodes(value: Any, what: str) -> Sequence[Any]:
    """Insist that the parser handed back something walkable, or refuse.

    ``bashlex.parse`` returns a list of trees and every node carries a list of parts -- but
    "returns" is a property of a working parser, and this module's contract is that a broken
    one produces a *denial with a reason*, not a ``TypeError`` for someone else to catch.
    The fail-closed guard in :class:`arl.hookio.Hook` would still deny either way; the
    difference is whether the model is told the parser misbehaved or told the gate crashed.
    """
    if isinstance(value, (list, tuple)):
        return value
    raise CommandShapeError(f"the parser returned {what} ({type(value).__name__})")


#: AST nodes that carry a literal word. ``assignment`` is included so ``VAR=x git commit``
#: keeps the token stream the shell produced -- and is refused a line later, by
#: ``_validate_segment``, for not starting with ``git``. Turning it into a parser-level
#: refusal here would change what the model is told for no gain.
_WORD_KINDS: Final = frozenset({"word", "assignment"})


def _words(tree: Any) -> list[str]:
    """Flatten the AST the deny-list allows to exist into the shell's token stream.

    That is exactly two shapes: one command, or commands joined by ``&&``. Anything else --
    a pipeline, a subshell, a compound, a redirect -- is refused. Those are unreachable
    while the deny-list runs first, and they are checked anyway: this is the layer that
    would have to hold if the deny-list were ever relaxed, and a reader of that future
    change should find it already written down rather than assume it.
    """
    kind = getattr(tree, "kind", "")
    if kind == "command":
        return _command_words(tree)
    if kind != "list":
        raise CommandShapeError(f'the command is a "{kind}", not a plain command or an "&&" chain')

    tokens: list[str] = []
    expect_command = True
    for part in _nodes(getattr(tree, "parts", None), "a chain whose parts are not a list of nodes"):
        part_kind = getattr(part, "kind", "")
        if expect_command:
            if part_kind != "command":
                raise CommandShapeError(f'the chain contains a "{part_kind}", not a plain command')
            tokens.extend(_command_words(part))
        else:
            if part_kind != "operator" or getattr(part, "op", "") != "&&":
                raise CommandShapeError('only "&&" may join the commands in a commit sequence')
            tokens.append("&&")
        expect_command = not expect_command

    if expect_command:
        raise CommandShapeError("the command ends in an operator with nothing after it")
    return tokens


def _command_words(node: Any) -> list[str]:
    words: list[str] = []
    for part in _nodes(getattr(node, "parts", ()), "a command whose parts are not a list of nodes"):
        kind = getattr(part, "kind", "")
        if kind not in _WORD_KINDS:
            raise CommandShapeError(f'the command contains a "{kind}", which this gate does not accept in a commit sequence')
        word = getattr(part, "word", None)
        if not isinstance(word, str):
            raise CommandShapeError(f'the parser returned a "{kind}" with no word')
        _reject_unreadable_word(part, word)
        words.append(word)
    return words


def _reject_unreadable_word(node: Any, word: str) -> None:
    """Refuse a word whose value is decided at exec time rather than by its text.

    ``tilde`` is the one exception: ``~/x`` reaches ``git`` as written and expands there,
    and the shell tokenizer passed it through unchanged. A substitution cannot be passed
    through -- its value is unknowable here -- but it also cannot be reached: ``$`` and
    backticks are refused by the deny-list before the parser runs. This is what makes that
    unreachability a checked property instead of a comment.
    """
    for part in _nodes(getattr(node, "parts", ()), "a word whose parts are not a list of nodes"):
        kind = getattr(part, "kind", "")
        if kind != "tilde":
            raise CommandShapeError(f'the word "{word}" contains a "{kind}" whose value the gate cannot know')


# --------------------------------------------------------------------------
# Cheap detection: does this command try to create a commit at all?
#
# Deliberately loose -- anything flagged here still has to pass full validation. This runs on
# every Bash call, ahead of any parse, so it has to answer even for input the parser would
# reject outright.
# --------------------------------------------------------------------------

_SPACE: Final = r"[ \t\v\f\r]"

#: Segment separators: bash runs what is on either side as its own command, so each is the
#: start of a fresh "is word 0 git?" question. Newline included -- the shell read a line at a
#: time and this reads the whole string.
_SEPARATORS: Final = frozenset(";&|()\n")

#: git global options that consume the **next word** as their value, so the subcommand is the
#: word after that. Measured against real git 2.55 by asking which word git reports as "not a
#: git command": ``git -C ZZ nosuchsubcmd`` complains about ``nosuchsubcmd`` (``-C`` ate
#: ``ZZ``), while ``git --no-pager ZZ nosuchsubcmd`` complains about ``ZZ``.
#:
#: ``--super-prefix`` is not in git 2.55 any more and ``--exec-path`` with a separate value
#: prints the path instead of running a subcommand; both are listed anyway, because listing an
#: option that takes no value can only ever cost a false denial, while omitting one that does
#: is a hole. The attached spellings (``--git-dir=<path>``) consume no extra word and are
#: recognised by the ``=`` instead.
_VALUE_OPTIONS: Final = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source", "--super-prefix", "--exec-path"}
)

#: git global options that are switches: they consume no value, so the **next** word is the
#: subcommand. Anything not in either set is an option this build has never heard of, and
#: :func:`_subcommand_is` then tries it both ways rather than guessing -- see there.
_SWITCH_OPTIONS: Final = frozenset(
    {
        "-v",
        "--version",
        "-h",
        "--help",
        "-p",
        "--paginate",
        "-P",
        "--no-pager",
        "--bare",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-advice",
        "--literal-pathspecs",
        "--no-literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--html-path",
        "--man-path",
        "--info-path",
    }
)


def detection_words(command: str) -> list[list[str]]:
    r"""The command as bash would word-split it, one list of words per segment.

    **Why words and not a regex over the flattened text.** :func:`detection_form` removes the
    quotes, which also removes the word boundary they were carrying: ``git -C "dir with space"
    commit`` flattens to ``git -C dir with space commit``, where no pattern can tell the path
    from the subcommand. The old detectors matched nothing there and the command reached the
    shell ungated -- measured, it lands an unreviewed commit, and the same quoting hid a
    ``reset`` and an ``update-ref`` from their guards. Splitting on the *unquoted* whitespace
    keeps the boundary the quotes were there to state.

    Quote and backslash removal is :func:`detection_form`'s, applied here per character so the
    boundaries survive it. A ``\`` before a newline is a line continuation -- both characters
    vanish and the word continues -- which is what makes ``git com\<newline>mit`` one word.

    Segments split on the separators bash itself uses (:data:`_SEPARATORS`), so ``make && git
    commit`` offers ``git`` as a word 0 rather than burying it. Deliberately cruder than the
    real tokenizer: this runs on every Bash call, ahead of any parse, and it must have an
    answer for input the parser would reject outright.
    """
    segments: list[list[str]] = [[]]
    word: list[str] = []
    quoted = False
    quote = ""
    index = 0

    def end_word() -> None:
        nonlocal quoted
        if word or quoted:
            segments[-1].append("".join(word))
            word.clear()
        quoted = False

    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                index += 1
                if command[index : index + 1] != "\n":
                    word.append(command[index : index + 1])
            else:
                word.append(char)
        elif char in ("'", '"'):
            quote = char
            quoted = True
        elif char == "\\":
            index += 1
            if command[index : index + 1] != "\n":
                word.append(command[index : index + 1])
        elif char in _SEPARATORS:
            end_word()
            segments.append([])
        elif char.isspace():
            end_word()
        else:
            word.append(char)
        index += 1
    end_word()
    return [segment for segment in segments if segment]


def _is_git(word: str) -> bool:
    """``git``, however it is spelled as a path.

    The shell matched the bare word, so ``/usr/bin/git commit -m x`` -- the same program, and
    what a ``PATH``-wary caller writes -- matched nothing and was passed through ungated.
    """
    return word.rsplit("/", 1)[-1] == "git"


def _subcommand_is(words: list[str], index: int, sub: str) -> bool:
    """Does the git invocation starting at ``words[index]`` run ``git <sub>``?

    Walks the global options between ``git`` and its subcommand, skipping each one's value
    when it takes a separate word (:data:`_VALUE_OPTIONS`) and not when it does not
    (:data:`_SWITCH_OPTIONS`). Reading arity is what keeps this from being either a hole or a
    nuisance: ``git -C . commit -m x`` really is a commit, and ``git --no-pager grep commit``
    really is a read-only grep whose argument happens to be the word ``commit``.

    **An option in neither set is tried both ways**, because it is an option this build has
    never heard of -- a newer git's, or a typo. Under-detecting there would be a hole that
    reopens itself the next time git grows an option; over-detecting costs a denial on a
    command that pairs an unknown global option with the literal word ``commit``, which is a
    trade in the direction Rule 1 points.
    """
    cursor = index + 1
    while cursor < len(words):
        word = words[cursor]
        if not word.startswith("-") or word == "-":
            return word == sub
        if "=" in word or word in _SWITCH_OPTIONS:
            cursor += 1
        elif word in _VALUE_OPTIONS:
            cursor += 2
        else:
            # Unknown arity: this option either took the next word or it did not.
            return words[cursor + 1 : cursor + 2] == [sub] or words[cursor + 2 : cursor + 3] == [sub]
    return False


def _mentions(command: str, sub: str) -> bool:
    """Does any segment of ``command`` run ``git <sub>``, however it is spelled?

    Every position is tried, not only word 0: over-detection routes a command into the gate, which
    then proves it is one of the accepted shapes or denies it.

    The dashed executable is the other spelling. git installs ``git-commit``, ``git-reset`` and
    ``git-update-ref`` in ``$(git --exec-path)`` -- measured on git 2.55 -- and each does what its
    subcommand does. Compared as a whole basename, which keeps ``git-commit-graph write`` and
    ``legit-commit -m x`` out.

    **Each segment is read two ways and the union is the answer.** Respecting the quotes finds
    ``git -C "dir with space" commit``; ignoring them -- splitting every word again on its inner
    whitespace -- keeps ``sh -c "git commit -m x"`` and a ``git commit`` inside a heredoc body.
    The first asks what bash runs *here*, the second whether the text names a commit at all, and
    dropping the second hands back the exec-wrapper bypass quote removal was added to close.
    """
    dashed = f"git-{sub}"
    for segment in detection_words(command):
        flattened = [part for word in segment for part in word.split()]
        for words in (segment, flattened):
            for index, word in enumerate(words):
                if word.rsplit("/", 1)[-1] == dashed:
                    return True
                if _is_git(word) and _subcommand_is(words, index, sub):
                    return True
    return False


_ESCAPE_RE: Final = re.compile(rf"arl(\.sh)?{_SPACE}+(finish|deactivate|resume|config|accept|pause)({_SPACE}|$)", re.MULTILINE)


def detection_form(command: str) -> str:
    r"""Undo backslash escapes and quoting, so detection reads a command as bash will.

    **This is the fix for a real bypass, not a tidy-up.** Matching the raw string missed
    ``g\it commit -m x`` -- which bash runs as ``git commit`` -- so ``pretool`` passed it through
    ungated. The same trick hid ``g\it reset --hard`` and ``arl.sh finish``, and quoting does it
    too: ``'g'it commit`` and ``g"i"t commit`` are both ``git commit`` to bash. So the
    word-removal half of bash's expansion is applied first.

    **A backslash before a newline is a line continuation: both characters disappear.** Bash
    splices the line, so ``git com\<newline>mit -m x`` runs ``git commit``; escaping the newline
    left ``com\nmit``, which no detector matches, and the command reached the shell with no gate
    consulted -- measured, it lands an unreviewed commit. Applies inside double quotes, and
    **not** inside single quotes, exactly as bash does.

    **Used for detection only, never for validation** -- ``tokenize`` reads the raw string,
    because that is where the deny-list lives. Over-detecting is the safe direction.

    What this cannot see is substitution: ``$(echo git) commit`` produces a name no textual pass
    can predict, and no parser closes it either -- bashlex reports a name node whose value is
    decided when it runs. What closes it is :func:`unresolved_expansion` refusing such a name, and
    the deny-list refusing ``$`` and backticks on the commit path.
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
                if command[index : index + 1] != "\n":
                    out.append(command[index : index + 1])
            else:
                out.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char == "\\":
            index += 1
            if command[index : index + 1] != "\n":
                out.append(command[index : index + 1])
        else:
            out.append(char)
        index += 1
    return "".join(out)


def mentions_commit(command: str) -> bool:
    """Does this command try to create a commit? See :func:`_mentions`."""
    return _mentions(command, "commit")


def mentions_reset(command: str) -> bool:
    """Does this command run ``git reset``? It moves ``HEAD`` off a reviewed commit."""
    return _mentions(command, "reset")


def mentions_update_ref(command: str) -> bool:
    """Does this command run ``git update-ref``?

    Detected for the same reason ``git reset`` is: it moves or removes a ref, which is a way
    of moving ``HEAD`` off a reviewed commit without ever running ``git commit``. The gate
    denies it outright except as the one bounded root-commit recovery (:func:`head_ref_deletion`).

    Detection is deliberately looser than the validator: the dashed spelling
    ``/usr/lib/git-core/git-update-ref -d HEAD`` reaches the gate here and is then refused by
    :func:`head_ref_deletion`, which accepts only the canonical ``git update-ref -d HEAD``.
    """
    return _mentions(command, "update-ref")


def is_escape(command: str) -> bool:
    """The user-only escapes. Claude's own route -- Bash -- is denied elsewhere (Rule 4)."""
    return _ESCAPE_RE.search(detection_form(command)) is not None


def is_set_phases(command: str, entrypoint: str) -> bool:
    r"""Is this command **exactly** ``<entrypoint> set-phases …`` and nothing else?

    ``set-phases`` is the one command permitted while the phase list is unfrozen -- a state in
    which nothing may change the repository -- so an ``allow`` here runs a program at a moment
    when everything else is denied. Two things must hold, and a substring match checked neither.

    **It must be the whole command.** ``grep 'arl\.sh[[:space:]]\+set-phases'`` *allowed*
    ``git add -A && git commit -m x && .../arl.sh set-phases --phase x``: the commit ran before
    phases were frozen, with no snapshot and no review. The command is tokenized instead, with the
    real tokenizer, and must be a single segment.

    **It must be this gate's own script**, matched as the exact path the caller passes in, never
    by basename -- the repository under review can ship an executable called ``arl``. ``arm``
    prints the exact path for the model to copy.

    The arguments after ``set-phases`` are deliberately unconstrained: :mod:`arl.commands.phases`
    can freeze a phase list and nothing else.

    A ``False`` says nothing about *why*; :func:`set_phases_refusal` turns a refusal into a
    message, and the two must stay in step.
    """
    try:
        tokens = tokenize(command)
    except CommandShapeError:
        return False
    if len(tokens) < 2 or "&&" in tokens:
        return False
    return tokens[0] == entrypoint and tokens[1] == "set-phases"


def set_phases_refusal(command: str, entrypoint: str) -> str:
    """Why this ``set-phases`` attempt was refused, or ``""`` if it was not one.

    :func:`is_set_phases` answers a verdict and throws the reason away. That was survivable
    while the reason was always "you wrote something else", and became a dead end once it
    could be "your phase description contains a backtick": the caller fell through to the
    generic *"the phase list has not been frozen"* denial, which names no cause, so a model
    that had just run the exact command the gate asked for was told only to run it again.
    Measured on a real activation: four attempts, four identical messages, then a
    ``NEEDS_HUMAN`` escalation on a plan that only needed its descriptions rephrased.

    **This changes no verdict.** It runs only after :func:`is_set_phases` has already said
    no, and it returns a string for the denial to quote. The textual pre-check is what keeps
    it from claiming an unrelated command was a set-phases attempt -- an ordinary
    ``git status`` in the unfrozen state must still get the ordinary message, since telling
    the model its *quoting* was wrong there would send it after a fault it did not commit.
    """
    if not _looks_like_set_phases(command, entrypoint):
        return ""
    try:
        tokenize(command)
    except CommandShapeError as exc:
        return str(exc)
    if is_set_phases(command, entrypoint):
        return ""
    return "it must be that command on its own, with nothing chained onto it"


#: ``set-phases`` as its own shell word: at least one separator in front of it, and the word
#: ending at a separator or at the end of the command. Both halves matter, and for opposite
#: reasons -- without the leading one ``<entrypoint>set-phases …`` names a *different program*
#: and would be coached as though it were this one, and without the trailing one a tab before
#: the first ``--phase`` hid a genuine attempt behind the generic denial.
_SET_PHASES_WORD: Final = re.compile(r"[ \t]+set-phases([ \t]|\Z)")


def _looks_like_set_phases(command: str, entrypoint: str) -> bool:
    """Does ``command`` begin with ``<entrypoint> set-phases``, read as plain text?

    Textual on purpose: the tokenizer is exactly what is unavailable here, because the reason
    this is being asked at all is that tokenizing raised. That rules out asking the shell
    where the word boundaries are, so they are spelled out -- separators are a space or a tab,
    the two the deny scan itself treats as separators, and a newline is not one of them
    because a command containing one is refused before any of this is asked.

    The entrypoint is matched with ``startswith`` rather than a pattern: it is a path from the
    caller, and a path is full of characters a regex would read as syntax.
    """
    stripped = command.strip(" \t")
    if not stripped.startswith(entrypoint):
        return False
    return _SET_PHASES_WORD.match(stripped, len(entrypoint)) is not None


# --------------------------------------------------------------------------
# Expansion: words this gate cannot read
# --------------------------------------------------------------------------

_EXPANSIONS: Final = {
    "$(": "a command substitution ($( … ))",
    "${": "a variable expansion (${ … })",
    "$'": "an ANSI-C quoted string ($' … ')",
}

#: What a non-``tilde`` part of a parsed word means, by bashlex node kind. Every one of them
#: has a value only the shell knows at exec time; the mapping exists so the denial names the
#: construct the model actually wrote.
_PART_EXPANSIONS: Final = {
    "commandsubstitution": "a command substitution ($( … ) or backticks)",
    "parameter": "a variable expansion ($NAME, ${ … } or $' … ')",
    "processsubstitution": "a process substitution (<( … ))",
}

#: Programs whose whole job is to run a command line handed to them as an argument. The
#: name-only rule below would clear ``sh -c "$CMD"`` -- a literal name, an argument the gate
#: cannot read -- and that argument is a command name in every sense that matters here, so
#: these are the one case where an argument is still refused.
#:
#: **A speed bump, not a boundary.** ``python3 -c "$CODE"`` walks straight through it, as
#: ``python3 script.py`` always did; so does any interpreter not on this list, and so does a
#: shell function. What actually catches a commit made that way is ``confirm-commit``
#: noticing afterwards that HEAD moved to a tree no review approved. This list buys one
#: specific thing: ``sh -c 'git commit'`` is caught today by ``detection_form``'s quote
#: stripping, and ``sh -c "$CMD"`` must not become the trivial way around that.
_EXEC_WRAPPERS: Final = frozenset({"sh", "bash", "zsh", "env", "xargs", "eval", "timeout", "nohup", "command", "sudo"})


def unresolved_expansion(command: str) -> str:
    """Name the expansion that makes this command's **name** unknowable, or return "".

    ``$(printf git) commit -m x`` contains no word any textual pass can resolve to ``git`` and
    runs ``git commit`` all the same; bashlex only reports that the name *is* a substitution node,
    so the sound answer is refusal. This makes it, on every Bash call, before anything is
    classified.

    **Its guarantee is about a command name and nothing wider.** An expansion in an *argument* was
    never part of it -- ``echo "exit=$?"`` runs ``echo`` -- and refusing those cost a real loop a
    scratchpad file and a second Bash call roughly six times in one session. The wider hole it
    cannot close (``eval``, ``xargs``, ``env``, a shell function) is caught by ``confirm-commit``
    noticing afterwards that HEAD moved to a tree no review approved.

    Four steps, in order:

    1. **A textual scan, heredoc-aware.** A heredoc opened with a *quoted* delimiter expands
       nothing, so its body is skipped whole. Finding nothing returns ``""`` with no parse, which
       is every ordinary command -- the ~55 ms bashlex import stays off the hot path.
    2. **An unquoted heredoc body is refused outright.** Bash expands it, and bashlex files it
       under a ``heredoc`` node where step 3 would never see it.
    3. **Parse, then refuse only a name.** Every ``command`` node's name is its first ``word``
       part, assignment prefixes skipped and redirects ignored; a name carrying any non-``tilde``
       part is refused. A parse failure or :class:`CommandShapeTimeout` denies.
    4. **The wrapper guard**, ``_EXEC_WRAPPERS`` -- a speed bump, not a boundary.

    A ``$`` inside single quotes is literal to bash and is left alone at every step.
    """
    found = _scan_expansion(command)
    if found is None:
        return ""
    index, in_expanded_heredoc = found
    if in_expanded_heredoc:
        return f"{_expansion_at(command, index)} in the body of a heredoc whose delimiter is unquoted, which bash expands"
    try:
        trees = _parse_trees(command)
    except CommandShapeError:
        # Including the deadline. Neither leaves anything to reason about, so the refusal is
        # the one the scan already justified.
        return _expansion_at(command, index)
    for node in _command_nodes(trees):
        reason = _unreadable_command_name(node)
        if reason:
            return reason
    return ""


def _unreadable_command_name(node: Any) -> str:
    """Why this one ``command`` node's name cannot be read, or ``""``.

    The name is the first part that is neither an ``assignment`` prefix (``VAR=x git commit``
    keeps ``git`` as its name) nor a ``redirect`` (``> /dev/null`` can sit anywhere in a
    command's parts, including before the name). A node with no word part at all runs no
    program -- a bare assignment, a bare redirection -- so there is nothing to refuse.
    """
    name_word: Any = None
    arguments: list[Any] = []
    for part in _nodes(getattr(node, "parts", ()), "a command whose parts are not a list of nodes"):
        kind = getattr(part, "kind", "")
        if kind == "redirect" or (name_word is None and kind == "assignment"):
            continue
        if name_word is None:
            if kind != "word":
                # Unreachable with today's grammar; a name this module cannot even identify
                # is the one case where guessing would be worst.
                return f'a "{kind}" where the command name should be'
            name_word = part
            continue
        if kind == "word":
            arguments.append(part)

    if name_word is None:
        return ""
    name = getattr(name_word, "word", "")
    expansion = _word_expansion(name_word)
    if expansion:
        return f"{expansion} in the command name"
    if not isinstance(name, str) or name.rsplit("/", 1)[-1] not in _EXEC_WRAPPERS:
        return ""
    for argument in arguments:
        expansion = _word_expansion(argument)
        if expansion:
            return f"{expansion} in an argument to `{name}`, which runs the command line it is given"
    return ""


def _word_expansion(word: Any) -> str:
    """The first non-``tilde`` part of a parsed word, described, or ``""`` if it is literal.

    ``tilde`` is the one exception, for :func:`_reject_unreadable_word`'s reason: ``~/x``
    reaches the program as written and expands there.
    """
    for part in _nodes(getattr(word, "parts", ()), "a word whose parts are not a list of nodes"):
        kind = getattr(part, "kind", "")
        if kind != "tilde":
            return _PART_EXPANSIONS.get(kind, f'a "{kind}" whose value the gate cannot know')
    return ""


def _command_nodes(trees: Sequence[Any]) -> list[Any]:
    """Every ``command`` node anywhere in ``trees``, found by walking node attributes.

    Iterative rather than recursive on purpose: the input is a repository-supplied command
    line, and ``$( $( $( …`` nests as deeply as it likes. A recursive walk would answer a
    deep nest with a ``RecursionError`` -- a crash for the fail-closed guard to catch rather
    than the denial with a reason this module promises.

    Attributes are read generically instead of by name (``parts``, ``command``, ``output``,
    ``list``, ``heredoc``, ...) because bashlex spells a child differently in almost every
    node kind, and a walk that enumerated them would silently stop finding command names the
    day a kind was missed.
    """
    found: list[Any] = []
    stack = list(trees)
    while stack:
        node = stack.pop()
        if getattr(node, "kind", "") == "command":
            found.append(node)
        for value in vars(node).values() if hasattr(node, "__dict__") else ():
            if isinstance(value, (list, tuple)):
                stack.extend(item for item in value if _is_ast_node(item))
            elif _is_ast_node(value):
                stack.append(value)
    return found


def _is_ast_node(value: Any) -> bool:
    return hasattr(value, "__dict__") and isinstance(getattr(value, "kind", None), str)


def _expansion_at(command: str, index: int) -> str:
    if command[index] == "`":
        return "a backtick (command substitution)"
    return _EXPANSIONS.get(command[index : index + 2], "a variable expansion ($ … )")


def _scan_expansion(command: str) -> tuple[int, bool] | None:  # noqa: PLR0912, PLR0915 - see `_deny_shell_grammar`: one flat scanner, one branch per character class
    r"""``(index of the first unresolved expansion, is it in an expanded heredoc body)``, or ``None``.

    **The invariant this function must not break: never skip text bash executes.** The only thing
    it skips is a heredoc body, so every rule exists to stop a ``<<`` being read as a heredoc
    where bash does not, or as one ending later than bash ends it. Each was a live bypass, each
    verified by running the payload under real bash:

    - **A line continuation carries the logical line on**, so the next character is not at the
      start of a line and no body starts there. Read as an ordinary escape, ``\<newline># <<':'``
      opened a heredoc out of commented text and swallowed the next command.
    - **A comment is skipped**, so ``# <<':'`` cannot queue a delimiter and read the next line as
      body while bash discards the comment and runs it. ``#`` opens a comment only where a word is
      not already open, which is bash's own rule.
    - **``<<`` inside ``(( ))`` is a left shift.** ``((1 << 'true'))`` queued a delimiter and
      skipped the next line, which bash went on to execute. ``$((`` needs no rule: the ``$`` is
      flagged first.
    - **A body is skipped only once its delimiter is fully known** -- see
      :func:`_heredoc_delimiter`.

    Delimiters are queued rather than consumed on sight, because bash queues them too:
    ``cmd <<'A' <<'B'`` takes A's body then B's, and consuming the first where it appears would
    read B's body as shell text.
    """
    quote = ""
    pending: list[tuple[str, bool, bool]] = []
    started = False
    arithmetic = 0
    index = 0
    length = len(command)

    while index < length:
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
                return index, False
        elif char == "\\":
            if command[index + 1 : index + 2] == "\n":
                # A line continuation. Bash removes both characters before parsing, so the
                # logical line -- and `started` with it -- carries on unbroken and no heredoc
                # body starts here.
                index += 2
                continue
            index += 1
            started = True
        elif char in ("'", '"'):
            quote = char
            started = True
        elif char == "\n":
            started = False
            if pending:
                index, found = _consume_heredocs(command, index + 1, pending)
                pending = []
                if found is not None:
                    return found, True
                continue
        elif char == "(" and command[index + 1 : index + 2] == "(":
            arithmetic += 1
            started = False
            index += 2
            continue
        elif char == ")" and command[index + 1 : index + 2] == ")" and arithmetic:
            arithmetic -= 1
            started = False
            index += 2
            continue
        elif char in " \t;&|()":
            started = False
        elif char == "#" and not started:
            # Bash discards the rest of the line, so nothing in it can open a heredoc or
            # decide a command name. The newline is deliberately left unconsumed: a `<<`
            # earlier on this line still has a body starting after it.
            newline = command.find("\n", index)
            index = length if newline == -1 else newline
            continue
        elif char == "<" and command[index + 1 : index + 2] == "<" and command[index + 2 : index + 3] != "<":
            start = None if arithmetic else _heredoc_delimiter(command, index)
            if start is not None:
                delimiter, strip_tabs, expands, index = start
                pending.append((delimiter, strip_tabs, expands))
                started = False
                continue
            started = True
        elif char in "$`":
            return index, False
        else:
            started = True
        index += 1
    return None


def _heredoc_delimiter(command: str, index: int) -> tuple[str, bool, bool, int] | None:
    r"""``(delimiter, strips leading tabs, expands its body, index after the delimiter)`` for the
    ``<<`` at ``index``, or ``None`` when the delimiter cannot be resolved exactly.

    The delimiter is a whole word read with bash's quote removal, not by its first character:
    ``<<E'OF'`` delimits on ``EOF``. Resolving it *later* than bash does would make
    :func:`_scan_expansion` swallow lines bash executes.

    Quoting **any** part of the word turns expansion off for the whole body, so ``quoted``
    accumulates across the word. ``<<-`` strips leading tabs from the body and the terminator, so
    that flag travels with the delimiter.

    A ``\``-newline inside the word is a **line continuation**: ``<<E\<newline>OF`` delimits on
    ``EOF`` and is not quoted by it. Read as an escape it produced a delimiter containing a
    newline -- which no line can equal -- so every remaining line was skipped as body while bash
    executed all of it.

    ``None`` -- not a heredoc, keep scanning as shell text -- for an empty word, an unterminated
    quote, and a word containing ``$`` or a backtick. That last is the fail-closed direction.
    """
    index += 2
    strip_tabs = command[index : index + 1] == "-"
    if strip_tabs:
        index += 1
    while command[index : index + 1] in (" ", "\t"):
        index += 1

    delimiter: list[str] = []
    quoted = False
    length = len(command)
    while index < length:
        char = command[index]
        if char == "\\" and command[index + 1 : index + 2] == "\n":
            # A line continuation: both characters go, and the word is no more quoted for it.
            index += 2
            continue
        if char in " \t\n;&|()<>":
            break
        if char in "$`":
            return None
        if char == "\\":
            if index + 1 >= length:
                return None
            delimiter.append(command[index + 1])
            quoted = True
            index += 2
            continue
        if char in ("'", '"'):
            end = _closing_quote(command, index)
            if end is None:
                return None
            delimiter.append(_unquote(command[index + 1 : end], char))
            quoted = True
            index = end + 1
            continue
        delimiter.append(char)
        index += 1

    if not delimiter:
        return None
    return "".join(delimiter), strip_tabs, not quoted, index


def _closing_quote(command: str, index: int) -> int | None:
    """The index of the quote that closes the one at ``index``, or ``None`` if none does.

    A backslash escapes the next character inside double quotes and nothing at all inside
    single quotes, exactly as in bash.
    """
    quote = command[index]
    index += 1
    while index < len(command):
        if quote == '"' and command[index] == "\\":
            index += 2
            continue
        if command[index] == quote:
            return index
        index += 1
    return None


def _unquote(body: str, quote: str) -> str:
    r"""One quoted run of a delimiter word with its quoting removed, exactly as bash removes it.

    Inside single quotes nothing is special, backslash included.

    Inside double quotes a backslash is special **only** before ``$``, a backtick, ``"``,
    another backslash, or a newline; before anything else bash keeps both characters. Removing
    it unconditionally made ``<<"E\qOF"`` resolve to ``EqOF`` where bash delimits on ``E\qOF``,
    so the real terminator was never recognised and every line after it -- which bash
    executes -- was skipped as heredoc body. A backslash-newline is a line continuation and
    both characters go.
    """
    if quote == "'":
        return body
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        following = body[index + 1 : index + 2]
        if char == "\\" and following == "\n":
            index += 2
            continue
        if char == "\\" and following in ("$", "`", '"', "\\"):
            out.append(following)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _consume_heredocs(command: str, position: int, pending: Sequence[tuple[str, bool, bool]]) -> tuple[int, int | None]:
    """Walk past every queued heredoc body, answering ``(index after them, offending index)``.

    The second element is the position of the first ``$`` or backtick found in a body bash
    *expands*, or ``None``. A body whose delimiter was quoted is skipped without being read
    at all: bash performs no expansion in it, so nothing in it can decide a command name.

    A backslash still escapes ``$`` and a backtick inside an expanded body, as in bash. An
    unterminated heredoc runs to the end of the text -- bash would refuse the command outright,
    so there is nothing left to protect.
    """
    for delimiter, strip_tabs, expands in pending:
        while position <= len(command):
            end = command.find("\n", position)
            stop = len(command) if end == -1 else end
            line = command[position:stop]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                position = stop if end == -1 else end + 1
                break
            if expands:
                offset = _expansion_in_body(line)
                if offset is not None:
                    return position, position + offset
            if end == -1:
                position = len(command)
                break
            position = end + 1
    return position, None


def _expansion_in_body(line: str) -> int | None:
    """The offset of the first unescaped ``$`` or backtick in one expanded heredoc line."""
    index = 0
    while index < len(line):
        if line[index] == "\\":
            index += 2
            continue
        if line[index] in "$`":
            return index
        index += 1
    return None


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

    Also consulted for short flags, as ``-<char>``. ``-F`` is the one single-dash key, and it
    is here because it is not a different option from ``--file`` -- it is the same option
    spelled short, and a model that reached for it deserves the same explanation and the same
    way out. The shell gave every short flag the generic message; this is a second deliberate
    divergence from it, alongside the ``printf`` bug below, and the verdict is unchanged.

    **One shell bug is not reproduced.** ``_arl_long_reason`` emitted these strings with
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
    if option in ("--file", "-F") or option.startswith("--file="):
        return "--file / -F reads the message from a path that may change after the snapshot; write a multi-paragraph message as repeated -m instead"
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
        # Unreachable through bashlex, which refuses a chain with a missing command before
        # this is ever called -- the shell tokenizer was the one that could produce it. Kept
        # because the alternative to an explicit refusal here is falling through to the
        # checks below on an empty list.
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


#: The metacharacters a commit sequence is actually written with by mistake, as opposed to the
#: ones (``;``, subshells, braces) that read as somebody scripting. They get their own denial
#: because "the command contains the shell metacharacter" says what was found and not why it
#: matters -- and here it matters twice over. See :data:`_PIPELINE_DENIED`.
_PIPELINE_CHARACTERS: Final = frozenset({"|", "<", ">", "&>", "&>>"})

#: Why a pipe or a redirection is refused *on a commit sequence specifically*.
#:
#: The measured shape is ``git add -A && git commit -m "…" 2>&1 | tail -40``, written to keep
#: a long commit's output readable. It was refused as a bare ``>`` metacharacter, which tells
#: the model what character to remove and nothing about the consequence it just avoided.
#:
#: A pipeline exits with its **last** command's status, so a failed ``git commit`` reports
#: success. Claude Code then sees a successful tool call, ``PostToolUseFailure`` never fires,
#: and the clean path in ``posttool._posttool_failure`` -- which simply clears the pending
#: approval -- is never taken. What runs instead is ``confirm-commit``: ``posttool._verify``
#: finds HEAD did not move and calls ``_reconcile``, so a mistyped commit message leaves the
#: activation in ``RECONCILE`` needing a recovery reset rather than a retry.
_PIPELINE_DENIED: Final = (
    'the commit sequence is piped or redirected ("{char}"). A redirection can write a file after the snapshot was taken, and a pipeline '
    "exits with its last command's status rather than git's -- so a commit that failed would report success, the gate would never be told "
    "the call failed, and the check that follows would find HEAD had not moved and put this activation into RECONCILE instead of simply "
    "clearing the approval. Run the commit on its own; read or trim its output in a separate Bash call"
)


def validate_commit(command: str) -> None:
    """Accept a commit sequence, or raise ``CommandShapeError`` explaining the refusal."""
    try:
        tokens = tokenize(command)
    except ShellMetacharacterError as exc:
        if exc.character in _PIPELINE_CHARACTERS:
            raise CommandShapeError(_PIPELINE_DENIED.format(char=exc.character)) from exc
        raise
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


def head_ref_deletion(command: str) -> None:
    """Accept **exactly** ``git update-ref -d HEAD``, the root-commit reconcile recovery.

    A commit that diverged from the reviewed tree is undone with ``git reset --soft <parent>``
    -- except when it is the repository's root commit, which has no parent and therefore no
    reset target that exists. ``update-ref -d HEAD`` deletes the branch ref ``HEAD`` points at,
    which removes that one commit and leaves the index and the working tree exactly as they
    are: the same "keep the content, drop the commit" effect ``--soft`` has everywhere else.

    Nothing else about ``update-ref`` is permitted, and the strictness is the point -- the
    general form writes any ref to any value, which is a way to move ``HEAD`` onto or off any
    commit at all. No ``&&``, no other ref, no old-value argument (the caller has already
    verified which commit ``HEAD`` is on, against the recorded divergence, and an old-value
    argument would only give this parser a second thing to be wrong about).
    """
    tokens = tokenize(command)
    if "&&" in tokens:
        raise CommandShapeError("the recovery ref deletion must be a single command on its own")
    # `tokens[0] != "git"` refuses every non-canonical spelling the *detector* deliberately
    # still catches -- `/usr/bin/git`, and the dashed `git-update-ref` executable. Denying
    # those is the safe direction and costs nothing: the recovery can be re-issued as
    # `git update-ref -d HEAD`. `reset_target` is strict in exactly the same way.
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "update-ref":
        raise CommandShapeError('not a plain "git update-ref" command')
    if tokens[2:] != ["-d", "HEAD"]:
        raise CommandShapeError('only "git update-ref -d HEAD" is permitted during a root-commit reconcile')
