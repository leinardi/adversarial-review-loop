r"""Command-shape classification: may this command be allowed to create a commit?

Two layers decide, and only the first one is policy:

* **The deny-list** refuses nearly the whole shell grammar -- ``$``, backticks, ``;``,
  ``|``, redirection, subshells, braces, unquoted globs, a bare ``&``, newlines, comments.
  Ported arm-for-arm from the plugin's now-retired shell implementation, character for
  character and message for message, and it still runs first. What survives it is a flat
  sequence of words joined by ``&&``.
* **bashlex** -- a real bash parser, vendored under :mod:`ocrl._vendor` -- turns that into
  words. It replaced a hand-rolled quote-and-escape loop, which is the one thing in this
  module a reader could not check against bash without re-deriving bash's own rules.

The deny-list did not move when the parser arrived, and that is deliberate. A parser makes
it *possible* to reason about a pipeline or a subshell; it does not make it wise. Relaxing
what is accepted is a separate change, with its own evidence, and the invariant to carry
into it is that the accepted grammar and the thing that reads it are one design. Widening
the deny-list without re-reading :func:`_words` breaks that, and neither will tell you.

Detection -- which commands are sent to the gate at all -- is still textual, and cannot be
otherwise: it runs on commands the deny-list *would* refuse, so there may be nothing
parseable to work with. See :func:`detection_form` and :func:`is_set_phases`, which document
the two bypasses that the shell's raw-string matching left open.

A third check, :func:`unresolved_expansion`, runs on **every** Bash call rather than only on
the commit path, and is not the boundary either. It exists so textual detection cannot go
blind on a command *name*, and it is scoped to exactly that: a name whose value an expansion
decides. It deliberately does not refuse an expansion in an argument or in a quoted heredoc
body -- refusing those bought no part of the guarantee and cost the loop a Bash call every
time it wanted to write a script. **The deny-list did not move for it**: on the commit path
``$``, backticks, ``;``, ``|``, redirection, subshells, globs and newlines are all still
refused, so ``git commit -m "$(x)"`` is denied exactly as before.

Every rejection raises ``CommandShapeError``; its message is the explanation the shell kept
in ``OCRL_CMD_ERROR`` and the gate shows to the model.
"""

from __future__ import annotations

import contextlib
import re
import signal
from collections.abc import Iterator, Sequence
from types import FrameType
from typing import Any, Final, NoReturn

from ocrl.errors import OcrlError
from ocrl.util import log, log_exception

__all__ = [
    "PARSE_TIMEOUT_SECONDS",
    "CommandShapeError",
    "CommandShapeTimeout",
    "ShellMetacharacterError",
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

    Two layers, in this order, and the order **is** the design:

    1. :func:`_deny_shell_grammar` refuses nearly the whole shell grammar. It is the
       hand-rolled scanner that used to do the splitting too, with the token accumulation
       taken out: the same characters are refused, in the same order, with the same
       messages.
    2. Whatever survives that is a flat sequence of words joined by ``&&``, and *bashlex*
       -- a real bash parser, vendored -- is what turns it into words.

    Keeping the deny-list in front of the parser is not belt-and-braces. bashlex parses the
    whole language, so on its own it would happily hand back a clean AST for a pipeline, a
    subshell or a redirection; the gate would then have to decide, node by node, which
    constructs cannot reach the filesystem after the snapshot -- the same policy as today,
    re-expressed against a grammar large enough to hide a mistake in. The deny-list keeps
    that decision a short list of characters a reader can check.

    What the parser buys is the *other* half: word splitting, quote removal and escape
    handling are now bash's rules as implemented by a parser, not as re-derived by a loop
    of this repository's own. Two things follow, both intended:

    - a command bashlex cannot parse is refused rather than tokenized on a guess. The
      hand-rolled loop read ``git commit -m x\\`` -- a trailing backslash -- as ``x``, where
      bash keeps the backslash and bashlex calls it an unexpected EOF. The gate now denies
      it. Denying a command whose words three implementations disagree about is the safe
      direction, and the model can re-issue it quoted.
    - the wording of a refusal for a *syntactically* broken command comes from the parser
      now (``git commit -m x && `` was "the command contains an empty segment", and is now
      a parse failure). The verdict is the same in every such case; only the explanation
      moved, and ``tests/unit/test_cmdshape.py`` asserts each one that did.
    """
    _deny_shell_grammar(command)
    # bashlex has no concept of an empty program: `parse("")` walks off the end of its own
    # AST. The shell tokenizer returned no tokens, and every caller already has a message
    # for that, so it stays their decision rather than becoming a parse error here.
    if not command.strip(" \t"):
        return []
    return _words(_parse(command))


def _deny_shell_grammar(command: str) -> None:  # noqa: PLR0912 - one branch per shell `case` arm; splitting it would hide the deny-list
    """Refuse everything that could run a second program or touch a file after the snapshot.

    **This is the security boundary.** Ported arm-for-arm from the plugin's retired shell
    implementation's ``case`` statement (now gone; see ``git log`` before the Phase 8 removal
    for the original), deliberately kept as one flat loop rather than split up, so a reviewer
    can read it top to bottom and see that nothing was dropped. ``tests/unit/test_cmdshape.py``
    is what now proves that claim, not a diff against the shell.

    Quote-aware, which is the whole subtlety: ``git commit -m "a;b"`` is a legitimate commit
    message and ``git commit -m x; rm -rf /`` is two commands. The scan therefore tracks
    quotes and escapes exactly as the splitting loop used to -- it *is* that loop, with the
    token accumulation removed and ``started`` kept, since a ``#`` is a comment only where a
    word is not already open.

    **One message differs from the shell's deliberately, and no verdict does.** ``&>`` and
    ``&>>`` are bash's "redirect stdout and stderr" operators; the shell's ``&`` arm caught
    them first and called them backgrounding. Both were refused then and are refused now --
    what changed is only that they are now named as the redirection they are, which is also
    what lets :func:`validate_commit` give them the redirect denial it gives ``>`` and ``|``.
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
#: The shim in ``scripts/ocrl.sh`` already runs each hook under ``timeout``, but that layer
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
    started by ``scripts/ocrl-bootstrap.py``, runs one subcommand and exits, and nothing
    else in ``ocrl`` touches ``signal``. It is written down because it is the kind of thing
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
    from ocrl._vendor import bashlex  # noqa: PLC0415 - see the docstring: ~55 ms of table construction
    from ocrl._vendor.bashlex import errors as bashlex_errors  # noqa: PLC0415

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
    The fail-closed guard in :class:`ocrl.hookio.Hook` would still deny either way; the
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
_ESCAPE_RE: Final = re.compile(rf"ocrl(\.sh)?{_SPACE}+(finish|deactivate|resume|config|accept)({_SPACE}|$)", re.MULTILINE)


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
    cumulative review runs.

    **The vendored parser does not close this, and no parser can.** bashlex reports a
    command whose *name is a substitution node*; what the node evaluates to is decided when
    it runs. What closes it is :func:`unresolved_expansion` refusing such a name outright
    while the gate is enforcing, and -- on the commit path specifically -- the deny-list
    refusing ``$`` and backticks anywhere at all.
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

    Detection reads text, and ``$(printf git) commit -m x`` contains no word this or any
    other textual pass can resolve to ``git``. It runs ``git commit`` all the same. A real
    parser does not fix that by itself: bashlex reports a command whose *name is a
    substitution node*, and the only sound answer to that is still refusal. This function is
    what makes the refusal, on every Bash call, before anything is classified.

    **Its guarantee is about a command name, and nothing wider.** An expansion in an
    *argument* was never part of it: ``echo "exit=$?"`` runs ``echo``, which is exactly what
    it says. Refusing those cost a real loop a scratchpad file and a second Bash call roughly
    six times in one session -- once forcing a ``$`` to be written ``chr(36)`` in Python
    source, an obfuscation that made the script less readable and nothing safer. This function
    already says as much about the wider hole it cannot close: ``eval``, ``xargs``, ``env``
    and a shell function all reach ``git`` with a literal command name, and what catches those
    is ``confirm-commit`` noticing afterwards that HEAD moved to a tree no review approved.

    Four steps, in order:

    1. **A textual scan, heredoc-aware.** ``$`` and backticks outside single quotes are
       located exactly as before, with one addition: a heredoc opened with a *quoted*
       delimiter (``<<'EOF'``, ``<<"EOF"``, ``<<-'EOF'``) has a body bash expands **nothing**
       in, so that body is skipped whole. Finding nothing returns ``""`` with no parse, which
       is every ordinary command -- the ~55 ms bashlex import stays off the hot path.
    2. **An unquoted heredoc body is refused outright.** ``<<EOF`` *is* expanded by bash, and
       bashlex hides that body in a ``heredoc`` node rather than in a word, so step 3 would
       clear it while bash cheerfully ran the substitution inside it. Quote the delimiter and
       the body is data again.
    3. **Parse, then refuse only a name.** Only a command that today is denied outright gets
       this far, so no call that currently succeeds starts paying for the parser. Every
       ``command`` node in the tree is walked -- a pipeline and a ``;`` list both hold several
       -- and its name is its first ``word`` part, ``assignment`` prefixes skipped and
       ``redirect`` parts ignored. A name word carrying any non-``tilde`` part is refused, by
       :func:`_reject_unreadable_word`'s own rule applied to the name alone. A parse failure
       or a :class:`CommandShapeTimeout` denies with the message the textual scan already had.
    4. **The wrapper guard**, ``_EXEC_WRAPPERS`` -- see there for why it is a speed bump
       rather than a boundary.

    A ``$`` inside single quotes is literal to bash and is left alone at every step, so
    ordinary ``grep '$foo'`` still works.
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
    """``(index of the first unresolved expansion, is it in an expanded heredoc body)``, or
    ``None`` when the text holds none.

    The same quote-and-escape tracking :func:`unresolved_expansion` has always done, plus line
    continuations, comments, arithmetic and heredocs. A heredoc body is not shell text -- bash
    reads it as data -- and whether it is *expanded* data is decided by one thing: whether any
    part of the delimiter was quoted.

    **The invariant this function must not break: never skip text bash executes.** The only
    thing it ever skips is a heredoc body, so every rule below exists to stop a ``<<`` being
    read as one where bash does not read it as one, or as one that ends later than bash ends it.
    Each was a live bypass, each verified by running the payload under real bash:

    - **A line continuation carries the logical line on.** ``\\`` + newline is removed by bash
      before anything else is parsed, so the next character is *not* at the start of a line and
      no heredoc body starts there. Treating it as an ordinary escape set ``started``, which
      turned the ``#`` after it into an ordinary word instead of a comment -- and then
      ``\\``-newline-``# <<':'`` opened a heredoc out of commented text and swallowed the
      command on the next line.
    - **A comment is skipped, so a ``<<`` inside one cannot open a heredoc.** ``# <<':'``
      queued a delimiter of ``:`` and read the substitution on the next line as body, while
      bash discarded the comment and ran it. ``#`` opens a comment only where a word is not
      already open, which is bash's own rule and the one ``_deny_shell_grammar`` implements;
      ``echo a#b`` has no comment in it.
    - **``<<`` inside ``(( ))`` is a left shift, not a redirect.** ``((1 << 'true'))`` queued a
      delimiter of ``true`` and skipped to the next line saying so -- and bash, whose
      arithmetic merely fails there, went on to execute what had been skipped. Heredocs are
      not recognised while an arithmetic command is open. ``$((`` needs no such rule: the ``$``
      is flagged before the ``((`` is ever reached.
    - **A heredoc body is skipped only once its delimiter is fully known.** See
      :func:`_heredoc_delimiter`, which answers ``None`` -- no heredoc, keep scanning as shell
      text -- for anything it cannot resolve exactly.

    Delimiters are queued rather than consumed on sight, because bash queues them too:
    ``cmd <<'A' <<'B'`` takes A's body first and then B's, both starting on the line after the
    ``<<``. Consuming the first one where it appears would skip past the second's ``<<`` and
    read its body as shell text.
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
    """``(delimiter, strips leading tabs, expands its body, index after the delimiter)`` for
    the ``<<`` at ``index``, or ``None`` when the delimiter cannot be resolved exactly.

    The delimiter is a whole word, read with bash's quote removal rather than by looking at
    its first character: ``<<E'OF'`` delimits on ``EOF``, not on ``E'OF'``. Getting that wrong
    is not cosmetic -- a delimiter this function resolves *later* than bash does would make
    :func:`_scan_expansion` swallow the lines between the two terminators, which bash executes.

    Quoting **any** part of the word turns expansion off for the whole body, which is why
    ``quoted`` accumulates across the word instead of being decided by the first character.
    ``<<-`` strips leading tabs from the body *and* from the terminator line, so that flag
    travels with the delimiter.

    A ``\\``-newline inside the word is a **line continuation**, not an escape: bash removes
    both characters and the word carries on, so ``<<E\\``-newline-``OF`` delimits on ``EOF``
    and is not quoted by it. Reading it as an escape produced a delimiter with a newline in
    it -- something no line can ever equal -- so the terminator was never found and every
    line to the end of the text was skipped as body, bash executing all of it.

    ``None`` -- meaning "this is not a heredoc, keep scanning as shell text" -- for an empty
    word, an unterminated quote, and a word containing ``$`` or a backtick. That last one is
    the fail-closed direction: the scan cannot prove where such a delimiter ends, and refusing
    to skip means the body is read as shell text and any expansion in it is flagged.
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
