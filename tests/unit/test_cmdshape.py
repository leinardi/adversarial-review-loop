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

import pytest
from conftest import bash_cmdshape

from ocrl import cmdshape
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


@pytest.mark.parametrize("command", ACCEPTED + DENIED)
def test_the_shell_reaches_the_same_verdict_and_says_the_same_thing(command: str) -> None:
    shell = bash_cmdshape("validate", command)
    try:
        cmdshape.validate_commit(command)
    except CommandShapeError as exc:
        assert shell.returncode == 1, f"python denied {command!r} ({exc}) but the shell accepted it"
        if any(option in command for option in PRINTF_BUG_OPTIONS):
            return  # covered by its own test below, which asserts the difference
        assert str(exc) == shell.stdout.decode(), f"the explanation drifted for {command!r}"
    else:
        assert shell.returncode == 0, f"python accepted {command!r} but the shell denied it: {shell.stdout.decode()}"


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
    "git commit -m x\\",
    "git commit -m 'trailing space '",
]


@pytest.mark.parametrize("command", TOKENIZED)
def test_the_split_into_tokens_matches_the_shell(command: str) -> None:
    shell = bash_cmdshape("tokenize", command)
    assert shell.returncode == 0, shell.stdout.decode()
    expected = [part.decode() for part in shell.stdout.split(b"\0")[:-1]]
    assert cmdshape.tokenize(command) == expected


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
