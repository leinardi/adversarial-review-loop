"""Command-shape classification, mirrored from ``selftest.sh``'s allowlist table.

Two things are asserted, per command:

* the verdict this implementation reaches, spelled out here so the table is readable on its
  own and a change of behaviour cannot hide behind a shared helper;
* that the still-live shell implementation reaches the *same* verdict with the *same*
  explanation. Phase 3 is a translation, and the message is part of what was translated --
  it is what the model is shown when a commit is refused.

The shell section stays in ``selftest.sh`` for now, because ``scripts/ocrl.sh`` is still the
live entrypoint and those assertions still cover production code.
"""

from __future__ import annotations

import signal
import subprocess
import time

import pytest
from conftest import bash_cmdshape

from ocrl import cmdshape
from ocrl._vendor import bashlex
from ocrl._vendor.bashlex import errors as bashlex_errors
from ocrl.cmdshape import CommandShapeError

# -- the corpus ------------------------------------------------------------

ACCEPTED = [
    "git commit -m x",
    'git commit -m "feat(x): a message with spaces"',
    "git add -A && git commit -m x",
    "git add -A && git status --porcelain && git commit -m x",
    'git commit -am "both"',
    'git add -u && git commit --message="long form"',
    "git add src lib && git commit -m x",
    # Beyond the shell suite's table, still expected to pass:
    "git commit -m 'single quoted'",
    "git commit -m a\\ b",
    "git status -uall && git commit -m x",
    "git status --porcelain=v1 && git commit -m x",
    'git commit --trailer "Co-authored-by: someone" -m x',
    "git commit -S -m x",
    "git commit --no-verify --signoff -m x",
    "git add -- src && git commit -m x",
    'git commit --author="A U Thor <a@example.invalid>" -m x',
]

DENIED = [
    "make build && git commit -m x",
    "git rm f && git commit -m x",
    "git diff --output=/tmp/x && git commit -m x",
    "git commit --amend",
    "git commit --amend -m x",
    'git commit -m "$(printf hi)"',
    "git commit -m `hostname`",
    "git commit -m x; rm -rf /",
    "git commit -m x > out.txt",
    "git commit -m x | tee log",
    "git commit --only src -m x",
    "git commit --include src -m x",
    "git commit src/main.go -m x",
    "git -C /other commit -m x",
    "git commit -F msg.txt",
    "git add -p && git commit -m x",
    "sed -i s/a/b/ f && git commit -m x",
    "git add -A & git commit -m x",
    "git status",
    "git commit -m",
    # Beyond the shell suite's table, still expected to fail:
    "",
    "git",
    "git commit -m x && ",
    'git commit -m "unterminated',
    "git commit -m x # comment",
    "git commit -m *.py",
    "git commit -m x{a,b}",
    "git commit --fixup=HEAD",
    "git commit --file msg.txt",
    "git commit --patch",
    "git commit --chmod=+x",
    "git commit --template t",
    "git commit --pathspec-from-file=list",
    "git add --force f && git commit -m x",
    "git add --renormalize && git commit -m x",
    "git commit -",
    "git commit -x -m x",
    "git commit --unknown-flag -m x",
    "git commit --author -m x",
    "git status --porcelain && git add -A",
    "GIT_DIR=/x git commit -m x",
    "git commit -m x && git checkout -- . && git commit -m y",
    " && ".join(["git add -A"] * 9 + ["git commit -m x"]),
]


@pytest.mark.parametrize("command", ACCEPTED)
def test_accepted(command: str) -> None:
    cmdshape.validate_commit(command)


@pytest.mark.parametrize("command", DENIED)
def test_denied(command: str) -> None:
    with pytest.raises(CommandShapeError):
        cmdshape.validate_commit(command)


# -- agreement with the shell implementation -------------------------------


#: Options whose specific reason the shell lost to `printf` -- see `_long_reason`. The
#: verdict is identical; only the explanation differs, and here it differs deliberately.
PRINTF_BUG_OPTIONS = ("--file", "--template", "--pathspec-from-file", "--chmod")

#: Commands the shell refused by walking off the end of its own token stream, and bashlex
#: refuses as the syntax errors they are. Same verdict, different wording -- and the wording
#: is better: `git commit -m x && ` is not "an empty segment", it is an unfinished command.
#: Each one is asserted individually below, so this exemption cannot quietly grow.
PARSER_WORDED_REFUSALS = ("git commit -m x && ",)


@pytest.mark.parametrize("command", ACCEPTED + DENIED)
def test_the_shell_reaches_the_same_verdict_and_says_the_same_thing(command: str) -> None:
    shell = bash_cmdshape("validate", command)
    try:
        cmdshape.validate_commit(command)
    except CommandShapeError as exc:
        assert shell.returncode == 1, f"python denied {command!r} ({exc}) but the shell accepted it"
        if any(option in command for option in PRINTF_BUG_OPTIONS) or command in PARSER_WORDED_REFUSALS:
            return  # covered by its own test below, which asserts the difference
        assert str(exc) == shell.stdout.decode(), f"the explanation drifted for {command!r}"
    else:
        assert shell.returncode == 0, f"python accepted {command!r} but the shell denied it: {shell.stdout.decode()}"


@pytest.mark.parametrize("command", PARSER_WORDED_REFUSALS)
def test_a_syntax_error_is_refused_as_one(command: str) -> None:
    """The shell called an unfinished command an "empty segment"; bashlex names the syntax.

    Asserted in both directions, because the point is that the *verdict* did not move when
    the wording did.
    """
    assert bash_cmdshape("validate", command).returncode == 1, "the shell must still have denied it"
    with pytest.raises(CommandShapeError, match="not valid shell syntax"):
        cmdshape.validate_commit(command)


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ("git commit --file msg.txt", "reads the message from a path"),
        ("git commit --template t", "opens an editor"),
        ("git commit --pathspec-from-file=list", "stages a set the gate cannot see"),
        ("git commit --chmod=+x", "changes modes outside the snapshot"),
    ],
)
def test_the_reasons_the_shell_lost_to_printf_are_restored(command: str, fragment: str) -> None:
    """These four denials say *why* now; in the shell `printf` ate the message.

    The shell still denied them -- via the generic allowlist message -- so this changes what
    the model is told, not what it is allowed to do.
    """
    shell = bash_cmdshape("validate", command)
    assert shell.returncode == 1, "the shell must still have denied it"
    assert "is not on the allowlist" in shell.stdout.decode(), "the shell's message must be the generic one"

    with pytest.raises(CommandShapeError, match=fragment):
        cmdshape.validate_commit(command)


TOKENIZED = [
    "git commit -m x",
    'git commit -m "one two"',
    "git commit -m a\\ b",
    "git   commit   -m   x",
    "git add -A&&git commit -m x",
    "git add -A && git commit -m ''",
    'git commit -m "a\'b"',
    "git commit -m 'a\"b'",
    "git commit -m 'trailing space '",
]


@pytest.mark.parametrize("command", TOKENIZED)
def test_the_split_into_tokens_matches_the_shell(command: str) -> None:
    shell = bash_cmdshape("tokenize", command)
    assert shell.returncode == 0, shell.stdout.decode()
    expected = [part.decode() for part in shell.stdout.split(b"\0")[:-1]]
    assert cmdshape.tokenize(command) == expected


def test_a_trailing_backslash_is_refused_rather_than_guessed_at() -> None:
    """Three implementations, three readings of ``git commit -m x\\`` -- so it is denied.

    The shell tokenizer dropped the backslash and produced ``x``. A real bash keeps it and
    runs the commit with the message ``x\\``. bashlex calls it an unexpected EOF. Only the
    last one is honest about not knowing, and a command whose words are in dispute is
    exactly what this gate must not wave through: the reviewed message and the committed
    message would differ.
    """
    assert bash_cmdshape("tokenize", "git commit -m x\\").stdout.split(b"\0")[:-1] == [b"git", b"commit", b"-m", b"x"]
    assert _bash_words("x\\") == ["x\\"]
    with pytest.raises(CommandShapeError, match="not valid shell syntax"):
        cmdshape.tokenize("git commit -m x\\")


# -- the tokenizer's own refusals ------------------------------------------


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ("git commit -m $HOME", "variable or command substitution"),
        ("git commit -m `id`", "backtick"),
        ("git commit -m x\nrm -rf /", "multiple lines"),
        ("git commit -m x\rrm -rf /", "multiple lines"),
        ("git commit -m x & ", "backgrounds"),
        ("git commit -m x; ls", "metacharacter"),
        ("git commit -m x | ls", "metacharacter"),
        ("git commit -m x < f", "metacharacter"),
        ("git commit -m x > f", "metacharacter"),
        ("git commit -m (x)", "metacharacter"),
        ("git commit -m {x}", "metacharacter"),
        ("git commit -m *", "glob"),
        ("git commit -m ?", "glob"),
        ("git commit -m [ab]", "glob"),
        ("git commit -m x #note", "comment"),
        ("git commit -m 'x", "unterminated quote"),
        ('git commit -m "x', "unterminated quote"),
    ],
)
def test_the_tokenizer_refuses_the_shell_grammar(command: str, fragment: str) -> None:
    """The deny-list is the security boundary; the token loop only runs on what survives it."""
    with pytest.raises(CommandShapeError, match=fragment):
        cmdshape.tokenize(command)


def test_quoting_hides_metacharacters_from_the_deny_list() -> None:
    """Quoted metacharacters are data, so they are allowed -- and stay one token."""
    assert cmdshape.tokenize('git commit -m "a;b|c(d)"') == ["git", "commit", "-m", "a;b|c(d)"]


def test_a_quoted_ampersand_is_not_a_segment_separator() -> None:
    assert cmdshape.tokenize('git commit -m "a && b"') == ["git", "commit", "-m", "a && b"]


# -- the loose detectors ---------------------------------------------------

MENTIONS = [
    "git commit -m x",
    "git commit",
    "cd /tmp && git commit -m x",
    "git -c user.name=x commit -m y",
    "make && git commit",
    "gitcommit",
    "git committer",
    "echo git commit",
    "git reset --soft HEAD^",
    "git reset",
    "resetting git",
    "",
]


@pytest.mark.parametrize("command", MENTIONS)
def test_the_loose_detectors_match_the_shell(command: str) -> None:
    assert cmdshape.mentions_commit(command) == (bash_cmdshape("mentions-commit", command).returncode == 0)
    assert cmdshape.mentions_reset(command) == (bash_cmdshape("mentions-reset", command).returncode == 0)


ESCAPES = [
    "ocrl.sh finish",
    "/x/y/ocrl.sh deactivate",
    "ocrl finish",
    "ocrl.sh status",
    "ocrl.sh finishing",
    "finish",
    "",
]


@pytest.mark.parametrize("command", ESCAPES)
def test_escape_recognition_matches_the_shell(command: str) -> None:
    assert cmdshape.is_escape(command) == (bash_cmdshape("is-escape", command).returncode == 0)


def test_the_documented_escapes_are_recognised() -> None:
    assert cmdshape.is_escape("ocrl.sh finish")
    assert cmdshape.is_escape("/x/y/ocrl.sh deactivate")
    assert not cmdshape.is_escape("ocrl.sh status")


# -- the bounded reconcile reset -------------------------------------------


def test_a_soft_reset_target_is_parsed() -> None:
    assert cmdshape.reset_target("git reset --soft HEAD^") == "HEAD^"
    assert cmdshape.reset_target("git reset --soft --quiet HEAD~2") == "HEAD~2"
    assert cmdshape.reset_target("git reset -q --soft HEAD^") == "HEAD^"


RESETS = [
    "git reset --soft HEAD^",
    "git reset --soft --quiet HEAD~2",
    "git reset -q --soft HEAD^",
    "git reset --hard HEAD^",
    "git reset --mixed HEAD^",
    "git reset --merge HEAD^",
    "git reset --keep HEAD^",
    "git reset --soft",
    "git reset HEAD^",
    "git reset --soft HEAD^ HEAD~2",
    "git reset --soft --patch HEAD^",
    "git reset --soft - ",
    "git commit -m x",
    "git reset --soft HEAD^ && git commit -m x",
    "",
]


@pytest.mark.parametrize("command", RESETS)
def test_reset_parsing_matches_the_shell(command: str) -> None:
    shell = bash_cmdshape("reset-target", command)
    try:
        target = cmdshape.reset_target(command)
    except CommandShapeError as exc:
        assert shell.returncode == 1, f"python refused {command!r} ({exc}) but the shell parsed it"
        assert str(exc) == shell.stdout.decode(), f"the explanation drifted for {command!r}"
    else:
        assert shell.returncode == 0, f"python parsed {command!r} but the shell refused it"
        assert target == shell.stdout.decode()


@pytest.mark.parametrize("mode", ["--hard", "--mixed", "--merge", "--keep"])
def test_only_a_soft_reset_is_permitted(mode: str) -> None:
    """Every other mode discards the working-tree content the gate exists to review."""
    with pytest.raises(CommandShapeError, match="discard working-tree content"):
        cmdshape.reset_target(f"git reset {mode} HEAD^")


def test_a_chained_reset_is_refused() -> None:
    with pytest.raises(CommandShapeError, match="single command on its own"):
        cmdshape.reset_target("git reset --soft HEAD^ && git commit -m x")


# -- the properties behind the table ---------------------------------------


def test_a_pathspec_makes_a_commit_partial() -> None:
    """`git commit <path>` commits something other than the tree that was reviewed."""
    with pytest.raises(CommandShapeError, match="partial commit"):
        cmdshape.validate_commit("git commit -m x src/main.go")
    with pytest.raises(CommandShapeError, match="partial commit"):
        cmdshape.validate_commit("git commit -m x -- src/main.go")
    # The same pathspec is fine for `git add`, which cannot create the commit.
    cmdshape.validate_commit("git add -- src/main.go && git commit -m x")


def test_global_options_before_the_subcommand_are_refused() -> None:
    """-C, -c, --git-dir and --work-tree can retarget the commit away from the worktree."""
    for command in ("git -C /other commit -m x", "git -c user.name=x commit -m y", "git --git-dir=/g commit -m z"):
        with pytest.raises(CommandShapeError, match="global options before the subcommand"):
            cmdshape.validate_commit(command)


def test_a_commit_segment_is_required() -> None:
    with pytest.raises(CommandShapeError, match='no "git commit" segment found'):
        cmdshape.validate_commit("git add -A && git status")


def test_the_segment_chain_is_bounded() -> None:
    with pytest.raises(CommandShapeError, match="too many chained segments"):
        cmdshape.validate_commit(" && ".join(["git add -A"] * 9 + ["git commit -m x"]))
    # One below the cap still passes, so the bound is the cap and not an off-by-one.
    cmdshape.validate_commit(" && ".join(["git add -A"] * 7 + ["git commit -m x"]))


def test_a_message_flag_without_its_value_is_refused() -> None:
    with pytest.raises(CommandShapeError, match="missing its value"):
        cmdshape.validate_commit("git commit -m")
    with pytest.raises(CommandShapeError, match="missing its value"):
        cmdshape.validate_commit("git add -A && git commit -am")


def test_a_value_consuming_flag_does_not_swallow_the_next_flag_check() -> None:
    """`--trailer <value>` consumes exactly one token, and validation resumes after it."""
    cmdshape.validate_commit("git commit --trailer 'Co-authored-by: someone' -m x")
    with pytest.raises(CommandShapeError, match="not on the allowlist"):
        cmdshape.validate_commit("git commit --trailer 'Co-authored-by: someone' --wat -m x")


# --------------------------------------------------------------------------
# Detection: the raw string is not what bash runs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(r"g\it commit", "git commit", id="backslash"),
        pytest.param(r"g\\it commit", r"g\it commit", id="escaped-backslash"),
        pytest.param("'g'it commit", "git commit", id="single-quotes"),
        pytest.param('g"i"t commit', "git commit", id="double-quotes"),
        pytest.param(r"'g\it' commit", r"g\it commit", id="backslash-is-literal-in-single-quotes"),
        pytest.param(r'"g\it" commit', "git commit", id="backslash-escapes-in-double-quotes"),
        pytest.param("git commit -m 'a b'", "git commit -m a b", id="ordinary-text-survives"),
    ],
)
def test_the_detection_form_is_what_bash_will_run(raw: str, expected: str) -> None:
    assert cmdshape.detection_form(raw) == expected


def _bash_words(text: str) -> list[str]:
    """The words a real bash makes of ``text``. Expansion only -- nothing is executed."""
    proc = subprocess.run(["bash", "-c", f"printf '%s\\0' {text}"], capture_output=True, check=True)
    return [word.decode() for word in proc.stdout.split(b"\0") if word]


@pytest.mark.parametrize(
    ("disguised", "real"),
    [
        pytest.param(r"g\it", "git", id="backslash"),
        pytest.param("'g'it", "git", id="single-quotes"),
        pytest.param('g"i"t', "git", id="double-quotes"),
        pytest.param(r"oc\rl.sh", "ocrl.sh", id="escaped-entrypoint"),
        pytest.param("'o'crl.sh", "ocrl.sh", id="quoted-entrypoint"),
    ],
)
def test_a_real_bash_reads_the_disguised_word_as_the_real_name(disguised: str, real: str) -> None:
    """The premise these bypasses rest on, asserted against bash rather than assumed."""
    assert _bash_words(disguised) == [real]
    assert cmdshape.detection_form(disguised) == real


@pytest.mark.parametrize(
    "command",
    [
        r"g\it commit -m x",
        r"g\it add -A && g\it commit -m x",
        "'g'it commit -m x",
        'g"i"t commit -m x',
    ],
)
def test_a_disguised_commit_is_detected(command: str) -> None:
    """Each of these ran ungated: the detector matched the raw string, which has no ``git``."""
    assert cmdshape.mentions_commit(command)


@pytest.mark.parametrize("command", [r"g\it reset --soft HEAD~1", "'g'it reset --hard HEAD"])
def test_a_disguised_reset_is_detected(command: str) -> None:
    assert cmdshape.mentions_reset(command)


@pytest.mark.parametrize("command", [r"oc\rl.sh finish", "/p/'o'crl.sh deactivate", r"ocrl\.sh finish"])
def test_a_disguised_escape_is_detected(command: str) -> None:
    assert cmdshape.is_escape(command)


#: The exact path ``arm`` prints for the model to copy. Nothing else is the exception.
ENTRYPOINT = "/plugin/scripts/ocrl.sh"


@pytest.mark.parametrize(
    ("command", "accepted"),
    [
        pytest.param(f"{ENTRYPOINT} set-phases --phase one", True, id="the-trusted-path"),
        pytest.param(f'{ENTRYPOINT} set-phases --phase "one" --phase "two"', True, id="several-phases"),
        pytest.param("./ocrl set-phases --phase one", False, id="a-program-the-repo-ships"),
        pytest.param("ocrl.sh set-phases --phase one", False, id="bare-name-off-PATH"),
        pytest.param("/elsewhere/ocrl.sh set-phases", False, id="another-copy"),
        pytest.param(f"git add -A && git commit -m x && {ENTRYPOINT} set-phases --phase x", False, id="commit-chained-ahead"),
        pytest.param(f"{ENTRYPOINT} set-phases --phase x && git commit -m x", False, id="commit-chained-behind"),
        pytest.param(f"echo {ENTRYPOINT} set-phases", False, id="mentioned-not-invoked"),
        pytest.param(f"{ENTRYPOINT} status", False, id="another-subcommand"),
        pytest.param(f"bash {ENTRYPOINT} set-phases", False, id="through-an-interpreter"),
        pytest.param(f"{ENTRYPOINT} set-phases; git commit -m x", False, id="sequenced"),
        pytest.param(f"{ENTRYPOINT} set-phases --phase $(id)", False, id="substitution"),
    ],
)
def test_only_the_trusted_set_phases_command_is_the_armed_exception(command: str, accepted: bool) -> None:
    """Two bypasses at once: a substring match, and trusting anything named ``ocrl``.

    The shell matched ``ocrl\\(\\.sh\\)\\?[[:space:]]\\+set-phases`` anywhere in the raw
    command, so a commit could ride along in front of it -- and any executable with that name,
    including one the repository under review ships, satisfied it.
    """
    assert cmdshape.is_set_phases(command, ENTRYPOINT) is accepted


# --------------------------------------------------------------------------
# Expansion: words the gate cannot read
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("$(printf git) commit -m x", id="command-substitution"),
        pytest.param("`printf git` commit -m x", id="backtick"),
        pytest.param("$'\\x67it' commit -m x", id="ansi-c-quoting"),
        pytest.param("${GIT} commit -m x", id="braced-variable"),
        pytest.param('echo "$HOME"', id="expansion-in-double-quotes"),
        pytest.param("make test && echo $?", id="anywhere-in-the-command"),
    ],
)
def test_a_command_whose_words_are_unknowable_is_named(command: str) -> None:
    """``$(printf git) commit`` runs ``git commit`` and contains no ``git`` to match on."""
    assert cmdshape.unresolved_expansion(command)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("make test", id="plain"),
        pytest.param("grep '$foo' file.txt", id="dollar-in-single-quotes"),
        pytest.param(r"echo \$notreally", id="escaped-dollar"),
        pytest.param('git commit -m "a message"', id="ordinary-quoting"),
    ],
)
def test_a_command_with_no_expansion_is_left_alone(command: str) -> None:
    """A ``$`` bash treats as literal is literal here too, so ordinary regexes still work."""
    assert cmdshape.unresolved_expansion(command) == ""


# --------------------------------------------------------------------------
# The parser: every way it can fail is a denial
# --------------------------------------------------------------------------
#
# Unit-level correctness of the tokenizer says nothing about what happens when the parser
# *misbehaves*, and that is the half a vendored dependency adds. A parser that raises, that
# returns something unexpected, or that never returns must each end in a refusal -- never in
# an approval, and never in an unhandled exception that some outer `except` might read as
# "not a commit".
#
# `validate_commit` is driven rather than `_parse`, because the property is that the gate's
# own entry point denies, not that a private helper raises.

#: The one command the whole corpus agrees is a valid commit. If the parser breaks, *this*
#: is what must stop being accepted.
GOOD = "git add -A && git commit -m x"


class _FakeNode:
    """Whatever bashlex might hand back that this gate is not prepared for."""

    def __init__(self, kind: str, **attrs: object) -> None:
        self.kind = kind
        for name, value in attrs.items():
            setattr(self, name, value)


def _patch_parse(monkeypatch: pytest.MonkeyPatch, replacement: object) -> None:
    monkeypatch.setattr(bashlex, "parse", replacement)


def test_the_vendored_parser_is_what_reads_the_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break bashlex and the accepted command must stop being accepted.

    Without this, every other test here could pass against a gate that still ran the old
    hand-rolled tokenizer and never imported the parser at all.
    """
    cmdshape.validate_commit(GOOD)  # the premise: it is accepted while the parser works

    def explode(_command: str) -> list[object]:
        raise RuntimeError("bashlex is broken")

    _patch_parse(monkeypatch, explode)
    with pytest.raises(CommandShapeError, match="could not be parsed"):
        cmdshape.validate_commit(GOOD)


@pytest.mark.parametrize(
    "exception",
    [
        pytest.param(RuntimeError("boom"), id="runtime-error"),
        pytest.param(RecursionError("too deep"), id="recursion-error"),
        pytest.param(AttributeError("NoneType has no attribute kind"), id="attribute-error"),
        pytest.param(IndexError("list index out of range"), id="index-error"),
        pytest.param(TypeError("unorderable"), id="type-error"),
    ],
)
def test_a_parser_exception_is_a_denial(monkeypatch: pytest.MonkeyPatch, exception: Exception) -> None:
    def raiser(_command: str) -> list[object]:
        raise exception

    _patch_parse(monkeypatch, raiser)
    with pytest.raises(CommandShapeError):
        cmdshape.validate_commit(GOOD)


def test_the_parsers_own_syntax_error_is_a_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ParsingError`` is the expected failure, and it is reported as a syntax error."""

    def raiser(_command: str) -> list[object]:
        raise bashlex_errors.ParsingError("unexpected token", GOOD, 4)

    _patch_parse(monkeypatch, raiser)
    with pytest.raises(CommandShapeError, match="not valid shell syntax"):
        cmdshape.validate_commit(GOOD)


@pytest.mark.parametrize(
    ("trees", "fragment"),
    [
        pytest.param([], "more than one statement", id="no-tree"),
        pytest.param([_FakeNode("command"), _FakeNode("command")], "more than one statement", id="two-trees"),
        pytest.param([_FakeNode("pipeline", parts=[])], "not a plain command", id="a-pipeline-at-the-top"),
        pytest.param([_FakeNode("compound", parts=[])], "not a plain command", id="a-compound-at-the-top"),
        pytest.param([_FakeNode("nonesuch")], "not a plain command", id="a-node-kind-that-does-not-exist"),
        pytest.param(
            [_FakeNode("list", parts=[_FakeNode("command", parts=[]), _FakeNode("operator", op="||"), _FakeNode("command", parts=[])])],
            'only "&&" may join',
            id="the-wrong-operator",
        ),
        pytest.param(
            [_FakeNode("list", parts=[_FakeNode("command", parts=[]), _FakeNode("command", parts=[])])],
            'only "&&" may join',
            id="two-commands-with-no-operator",
        ),
        pytest.param(
            [_FakeNode("list", parts=[_FakeNode("command", parts=[]), _FakeNode("operator", op="&&")])],
            "ends in an operator",
            id="a-chain-that-ends-in-an-operator",
        ),
        pytest.param(
            [_FakeNode("command", parts=[_FakeNode("redirect", output="f")])],
            "does not accept in a commit sequence",
            id="a-redirect-inside-the-command",
        ),
        pytest.param(
            [_FakeNode("command", parts=[_FakeNode("word", word=None)])],
            "with no word",
            id="a-word-that-is-not-a-string",
        ),
        pytest.param(
            [_FakeNode("command", parts=[_FakeNode("word", word="git", parts=[_FakeNode("commandsubstitution")])])],
            "whose value the gate cannot know",
            id="a-word-built-from-a-substitution",
        ),
    ],
)
def test_a_malformed_ast_is_a_denial(monkeypatch: pytest.MonkeyPatch, trees: list[object], fragment: str) -> None:
    """None of these is reachable while the deny-list runs first. All of them still deny.

    This is the layer that has to hold if the deny-list is ever widened -- the change the
    module docstring warns about -- so it is asserted now, while the reasoning is written
    down next to it.
    """
    _patch_parse(monkeypatch, lambda _command: list(trees))
    with pytest.raises(CommandShapeError, match=fragment):
        cmdshape.validate_commit(GOOD)


@pytest.mark.parametrize(
    ("returned", "fragment"),
    [
        pytest.param(None, "not a list of nodes", id="parse-returned-nothing"),
        pytest.param(5, "not a list of nodes", id="parse-returned-a-scalar"),
        pytest.param(_FakeNode("command", parts=[]), "not a list of nodes", id="parse-returned-a-bare-node"),
        pytest.param([_FakeNode("list", parts=None)], "not a list of nodes", id="a-chain-whose-parts-are-none"),
        pytest.param([_FakeNode("list")], "not a list of nodes", id="a-chain-with-no-parts-at-all"),
        pytest.param([_FakeNode("command", parts=None)], "not a list of nodes", id="a-command-whose-parts-are-none"),
        pytest.param(
            [_FakeNode("command", parts=[_FakeNode("word", word="git", parts=None)])],
            "not a list of nodes",
            id="a-word-whose-parts-are-none",
        ),
    ],
)
def test_a_parser_that_returns_something_unwalkable_is_a_denial(
    monkeypatch: pytest.MonkeyPatch,
    returned: object,
    fragment: str,
) -> None:
    """A broken parser must produce a refusal with a reason, not a ``TypeError``.

    The hook guard denies on an unhandled exception too, so the *verdict* was never at risk.
    What was at risk is the contract this module documents -- and the difference the model
    sees: "the parser misbehaved" against "the gate crashed", the second of which reads like
    a bug to route around rather than a command to re-issue.
    """
    _patch_parse(monkeypatch, lambda _command: returned)
    with pytest.raises(CommandShapeError, match=fragment):
        cmdshape.validate_commit(GOOD)


def test_a_tilde_is_the_one_word_part_that_is_read_as_written() -> None:
    """``~/x`` reaches git as written; the shell tokenizer passed it through and so does this."""
    assert cmdshape.tokenize("git add ~/x") == ["git", "add", "~/x"]


# -- the deadline ----------------------------------------------------------


def test_a_hanging_parse_denies_instead_of_stalling_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parser that never returns must still produce a verdict, and produce it early.

    The shim runs each hook under ``timeout`` as well, but that layer kills this process
    along with the shim's command substitution -- so the shim's generic fallback is what
    Claude would see. This deadline is the one that can still deny in the event's own words.
    """
    monkeypatch.setattr(cmdshape, "PARSE_TIMEOUT_SECONDS", 0.25)

    def never_returns(_command: str) -> list[object]:
        time.sleep(30)
        raise AssertionError("the deadline did not fire")

    _patch_parse(monkeypatch, never_returns)

    started = time.monotonic()
    with pytest.raises(cmdshape.CommandShapeTimeout, match="longer than"):
        cmdshape.validate_commit(GOOD)
    assert time.monotonic() - started < 5, "the deadline fired, but far too late to be the reason"


def test_a_wedged_pure_python_loop_is_interrupted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """``time.sleep`` is a syscall; a spin loop is what a runaway parser actually looks like.

    bashlex being pure Python is the reason this works: SIGALRM is delivered between
    bytecodes, so the interpreter can raise inside the loop.
    """
    monkeypatch.setattr(cmdshape, "PARSE_TIMEOUT_SECONDS", 0.25)

    def spins(_command: str) -> list[object]:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            pass
        raise AssertionError("the deadline did not fire")

    _patch_parse(monkeypatch, spins)

    with pytest.raises(cmdshape.CommandShapeTimeout):
        cmdshape.validate_commit(GOOD)


def test_the_timeout_is_a_command_shape_error_so_every_caller_already_denies() -> None:
    """The gate catches ``CommandShapeError``; a deadline that was not one would escape it."""
    assert issubclass(cmdshape.CommandShapeTimeout, CommandShapeError)


def test_the_timer_is_disarmed_and_the_previous_handler_restored() -> None:
    """A gate that left SIGALRM armed would fire it into whatever ran next."""
    sentinel = signal.getsignal(signal.SIGALRM)
    cmdshape.validate_commit(GOOD)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is sentinel


def test_a_failed_parse_also_disarms_the_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = signal.getsignal(signal.SIGALRM)

    def raiser(_command: str) -> list[object]:
        raise RuntimeError("boom")

    _patch_parse(monkeypatch, raiser)
    with pytest.raises(CommandShapeError):
        cmdshape.validate_commit(GOOD)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is sentinel
