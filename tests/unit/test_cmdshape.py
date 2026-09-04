"""Command-shape classification, mirrored from ``selftest.sh``'s allowlist table.

The verdict this implementation reaches for each command is spelled out here so the table is
readable on its own and a change of behaviour cannot hide behind a shared helper. This used to
also assert that the plugin's own (now-deleted) shell implementation reached the same verdict
with the same explanation; that comparison is gone along with ``scripts/lib/cmdshape.sh``, but
a handful of tests below still drive a real, unmodified system ``bash`` directly -- those are
about bash's own word-splitting and quoting rules, not about this plugin's retired Bash port,
and stay for exactly that reason.
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

import signal
import subprocess
import time

import pytest

from arl import cmdshape
from arl._vendor import bashlex
from arl._vendor.bashlex import errors as bashlex_errors
from arl.cmdshape import CommandShapeError

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
    # A commit body: repeated -m is the only way to write one, since a real newline is
    # refused by the deny-list and -F/--file is off the allowlist. Pinned here because
    # every banner and denial message now tells the model to use it.
    'git commit -m "subject" -m "body"',
    'git add -A && git commit -m "s" -m "b1" -m "b2"',
    'git commit --message="s" --message="b"',
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
    # Only the attached `--message=` form is on the allowlist; the space form is not, and
    # the asymmetry is pinned so it stays deliberate rather than becoming a surprise.
    'git commit --message "s"',
    " && ".join(["git add -A"] * 9 + ["git commit -m x"]),
]


@pytest.mark.parametrize("command", ACCEPTED)
def test_accepted(command: str) -> None:
    cmdshape.validate_commit(command)


@pytest.mark.parametrize("command", DENIED)
def test_denied(command: str) -> None:
    with pytest.raises(CommandShapeError):
        cmdshape.validate_commit(command)


#: Commands whose failure mode is bashlex naming a real syntax error -- `git commit -m x && `
#: is not "an empty segment", it is an unfinished command.
PARSER_WORDED_REFUSALS = ("git commit -m x && ",)


@pytest.mark.parametrize("command", PARSER_WORDED_REFUSALS)
def test_a_syntax_error_is_refused_as_one(command: str) -> None:
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
    """These four denials say *why*; the plugin's retired Bash port lost the reason to `printf`."""
    with pytest.raises(CommandShapeError, match=fragment):
        cmdshape.validate_commit(command)


def test_the_short_spelling_of_file_gets_the_same_reason_as_the_long_one() -> None:
    """``-F`` is ``--file``, so it may not be refused with less than ``--file`` is refused with.

    The shell gave every short flag the generic "not on the allowlist" message, and this one
    inherited it: a model that reached for ``-F msg.txt`` was told it was off the allowlist and
    told nothing about what to do instead, which is how a real run went on to try a newline
    inside ``-m`` and lose a second round. The verdict was always the same; only the
    explanation is new, and it carries the way out.
    """
    with pytest.raises(CommandShapeError, match="reads the message from a path") as denial:
        cmdshape.validate_commit("git commit -F msg.txt")
    assert "repeated -m" in str(denial.value), "the denial must name the form that does work"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("git commit -m x", ["git", "commit", "-m", "x"], id="plain"),
        pytest.param('git commit -m "one two"', ["git", "commit", "-m", "one two"], id="double-quoted-spaces"),
        pytest.param("git commit -m a\\ b", ["git", "commit", "-m", "a b"], id="escaped-space"),
        pytest.param("git   commit   -m   x", ["git", "commit", "-m", "x"], id="repeated-whitespace-collapses"),
        pytest.param("git add -A&&git commit -m x", ["git", "add", "-A", "&&", "git", "commit", "-m", "x"], id="adjacent-ampersands"),
        pytest.param("git add -A && git commit -m ''", ["git", "add", "-A", "&&", "git", "commit", "-m", ""], id="empty-single-quoted-argument"),
        pytest.param('git commit -m "a\'b"', ["git", "commit", "-m", "a'b"], id="single-quote-inside-double-quotes"),
        pytest.param("git commit -m 'a\"b'", ["git", "commit", "-m", 'a"b'], id="double-quote-inside-single-quotes"),
        pytest.param("git commit -m 'trailing space '", ["git", "commit", "-m", "trailing space "], id="trailing-space-preserved-inside-quotes"),
    ],
)
def test_the_split_into_tokens(command: str, expected: list[str]) -> None:
    """Fixed expectations, not a comparison: a parser regression here must fail this test on
    its own, without needing an accepted/denied verdict to also flip."""
    assert cmdshape.tokenize(command) == expected


def test_a_trailing_backslash_is_refused_rather_than_guessed_at() -> None:
    """Two implementations, two readings of ``git commit -m x\\`` -- so it is denied.

    A real bash keeps the backslash and runs the commit with the message ``x\\``. bashlex
    calls it an unexpected EOF. A command whose words are in dispute is exactly what this
    gate must not wave through: the reviewed message and the committed message would differ.
    """
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
        ("git commit -m x & ls", "backgrounds"),
        ("git commit -m x &>out", "redirection"),
        ("git commit -m x &>>out", "redirection"),
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


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('git add -A && git commit -m "x" 2>&1 | tail -40', id="the-measured-shape"),
        pytest.param("git commit -m x | tee log", id="pipe"),
        pytest.param("git commit -m x > out.txt", id="redirect-out"),
        pytest.param("git commit -m x < in.txt", id="redirect-in"),
        pytest.param("git commit -m x &>out", id="ampersand-redirect"),
        pytest.param("git commit -m x &>>out", id="ampersand-append-redirect"),
    ],
)
def test_a_piped_or_redirected_commit_is_denied_for_the_real_reason(command: str) -> None:
    """``git add -A && git commit -m "…" 2>&1 | tail -40`` is what a model writes to keep a long
    commit's output readable. It stays refused -- but "the command contains the shell
    metacharacter" names the character and not the consequence.

    A pipeline exits with its *last* command's status, so a failed commit reports success:
    ``PostToolUseFailure`` never fires, the clean pending-clear in ``posttool`` is never taken,
    and ``confirm-commit`` finds HEAD did not move and drives the activation into ``RECONCILE``.

    ``&>`` and ``&>>`` belong here too. Both are bash redirections -- verified: ``echo hi
    &>file`` writes the file and backgrounds nothing -- but the deny-list's ``&`` arm reached
    them first and called them backgrounding, which is both the wrong diagnosis and a way past
    this denial. They were always refused; only the explanation was wrong.
    """
    with pytest.raises(CommandShapeError, match="piped or redirected") as caught:
        cmdshape.validate_commit(command)
    assert "RECONCILE" in str(caught.value)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git commit -m x; ls", id="sequencing"),
        pytest.param("git commit -m x && (ls)", id="subshell"),
        pytest.param("git commit -m {x}", id="braces"),
    ],
)
def test_the_other_metacharacters_keep_the_generic_message(command: str) -> None:
    """Only a pipe and a redirection get the specific denial. A ``;`` or a subshell reads as
    somebody scripting, and there is no single consequence to name for it."""
    with pytest.raises(CommandShapeError, match="shell metacharacter"):
        cmdshape.validate_commit(command)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('git add -A && git commit -m "x" 2>&1 | tail -40', id="the-measured-shape"),
        pytest.param("git commit -m x | tee log", id="pipe"),
        pytest.param("git commit -m x > out.txt", id="redirect-out"),
    ],
)
def test_the_deny_list_itself_did_not_move(command: str) -> None:
    """The specific message is added by ``validate_commit``, not by the deny-list. Driven
    through ``tokenize``, every one of these still refuses with the character it always named --
    the same characters, in the same order, with the same words."""
    with pytest.raises(CommandShapeError, match="shell metacharacter"):
        cmdshape.tokenize(command)


def test_quoting_hides_metacharacters_from_the_deny_list() -> None:
    """Quoted metacharacters are data, so they are allowed -- and stay one token."""
    assert cmdshape.tokenize('git commit -m "a;b|c(d)"') == ["git", "commit", "-m", "a;b|c(d)"]


def test_an_ampersand_redirect_is_named_as_a_redirect_not_as_backgrounding() -> None:
    """``&>`` is one operator, and the deny-list must not read it as ``&`` plus ``>``.

    Verified against bash: ``echo hi &>file`` writes the file and backgrounds nothing. The
    verdict never changed -- both forms were refused before -- but a redirection reported as
    backgrounding is a wrong diagnosis, and it never reached the commit path's redirect denial.
    ``&&`` and a genuine trailing ``&`` must be untouched by the fix.
    """
    assert cmdshape.tokenize("git add -A && git commit -m x") == ["git", "add", "-A", "&&", "git", "commit", "-m", "x"]
    with pytest.raises(CommandShapeError, match="backgrounds"):
        cmdshape.tokenize("git commit -m x &")
    cmdshape.validate_commit('git commit -m "a&>b"')  # quoted, so it is message text


def test_a_quoted_ampersand_is_not_a_segment_separator() -> None:
    assert cmdshape.tokenize('git commit -m "a && b"') == ["git", "commit", "-m", "a && b"]


# -- the loose detectors ---------------------------------------------------


@pytest.mark.parametrize(
    ("command", "commit", "reset"),
    [
        ("git commit -m x", True, False),
        ("git commit", True, False),
        ("cd /tmp && git commit -m x", True, False),
        pytest.param("git -c user.name=x commit -m y", False, False, id="global-option-disguise-not-detected"),
        ("make && git commit", True, False),
        pytest.param("gitcommit", False, False, id="no-word-boundary-no-match"),
        pytest.param("git committer", False, False, id="longer-word-does-not-match"),
        ("echo git commit", True, False),
        ("git reset --soft HEAD^", False, True),
        ("git reset", False, True),
        pytest.param("resetting git", False, False, id="reset-as-a-substring-does-not-match"),
        ("", False, False),
    ],
)
def test_the_loose_detectors(command: str, commit: bool, reset: bool) -> None:
    """Pins false positives and false negatives alike -- a detector too loose escalates every
    ordinary command, and one too strict lets a real commit or reset through ungated."""
    assert cmdshape.mentions_commit(command) is commit
    assert cmdshape.mentions_reset(command) is reset


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("arl.sh finish", True),
        ("/x/y/arl.sh deactivate", True),
        ("arl finish", True),
        ("arl.sh resume", True),
        ("arl.sh config", True),
        ("arl.sh accept", True),
        ("arl.sh accept --reason x", True),
        ("arl.sh pause", True),
        ("arl.sh pause 3", True),
        ("/x/y/arl.sh resume --until 2", True),
        ("arl.sh status", False),
        pytest.param("arl.sh finishing", False, id="lookalike-suffix-not-an-escape"),
        pytest.param("finish", False, id="bare-word-without-the-entrypoint-is-not-an-escape"),
        ("", False),
    ],
)
def test_the_documented_escapes_are_recognised(command: str, expected: bool) -> None:
    assert cmdshape.is_escape(command) is expected


# -- the bounded reconcile reset -------------------------------------------


def test_a_soft_reset_target_is_parsed() -> None:
    assert cmdshape.reset_target("git reset --soft HEAD^") == "HEAD^"
    assert cmdshape.reset_target("git reset --soft --quiet HEAD~2") == "HEAD~2"
    assert cmdshape.reset_target("git reset -q --soft HEAD^") == "HEAD^"


@pytest.mark.parametrize("mode", ["--hard", "--mixed", "--merge", "--keep"])
def test_only_a_soft_reset_is_permitted(mode: str) -> None:
    """Every other mode discards the working-tree content the gate exists to review."""
    with pytest.raises(CommandShapeError, match="discard working-tree content"):
        cmdshape.reset_target(f"git reset {mode} HEAD^")


def test_a_chained_reset_is_refused() -> None:
    with pytest.raises(CommandShapeError, match="single command on its own"):
        cmdshape.reset_target("git reset --soft HEAD^ && git commit -m x")


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        pytest.param("git reset --soft", "needs an explicit target", id="missing-target"),
        pytest.param("git reset HEAD^", "only .git reset --soft", id="missing---soft"),
        pytest.param("git reset --soft HEAD^ HEAD~2", "exactly one target", id="multiple-targets"),
        pytest.param("git reset --soft --patch HEAD^", "--patch is not permitted", id="patch-flag"),
        pytest.param("git reset --soft - ", "git reset - is not permitted", id="bare-dash-target"),
        pytest.param("git commit -m x", 'not a plain "git reset"', id="not-a-reset-at-all"),
        pytest.param("", 'not a plain "git reset"', id="empty-string"),
    ],
)
def test_a_malformed_reset_is_refused(command: str, fragment: str) -> None:
    with pytest.raises(CommandShapeError, match=fragment):
        cmdshape.reset_target(command)


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
        pytest.param(r"a\rl.sh", "arl.sh", id="escaped-entrypoint"),
        pytest.param("'a'rl.sh", "arl.sh", id="quoted-entrypoint"),
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


@pytest.mark.parametrize(
    "command",
    [r"a\rl.sh finish", "/p/'a'rl.sh deactivate", r"arl\.sh finish", r"a\rl.sh resume", r"a\rl.sh config", r"a\rl.sh pause"],
)
def test_a_disguised_escape_is_detected(command: str) -> None:
    assert cmdshape.is_escape(command)


#: The exact path ``arm`` prints for the model to copy. Nothing else is the exception.
ENTRYPOINT = "/plugin/scripts/arl.sh"


@pytest.mark.parametrize(
    ("command", "accepted"),
    [
        pytest.param(f"{ENTRYPOINT} set-phases --phase one", True, id="the-trusted-path"),
        pytest.param(f'{ENTRYPOINT} set-phases --phase "one" --phase "two"', True, id="several-phases"),
        pytest.param("./arl set-phases --phase one", False, id="a-program-the-repo-ships"),
        pytest.param("arl.sh set-phases --phase one", False, id="bare-name-off-PATH"),
        pytest.param("/elsewhere/arl.sh set-phases", False, id="another-copy"),
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
    """Two bypasses at once: a substring match, and trusting anything named ``arl``.

    The shell matched ``arl\\(\\.sh\\)\\?[[:space:]]\\+set-phases`` anywhere in the raw
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
        pytest.param("VAR=x $(printf git) commit", id="behind-an-assignment-prefix"),
        pytest.param("> /dev/null $(printf git) commit", id="behind-a-redirect"),
        pytest.param("make test | $(printf git) commit", id="second-command-of-a-pipeline"),
        pytest.param("make test; $(printf git) commit", id="second-command-of-a-list"),
    ],
)
def test_a_command_whose_name_is_unknowable_is_named(command: str) -> None:
    """``$(printf git) commit`` runs ``git commit`` and contains no ``git`` to match on."""
    assert "in the command name" in cmdshape.unresolved_expansion(command)


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


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('echo "exit=$?"', id="status-in-an-argument"),
        pytest.param('fallow > /dev/null 2>&1; echo "exit=$?"', id="status-after-a-failing-command"),
        pytest.param("make test && echo $?", id="status-after-a-chain"),
        pytest.param('echo "$HOME"', id="variable-in-an-argument"),
        pytest.param("python3 - <<'PY'\nif re.match(r'/^\\/api$/', line):\n    print(1)\nPY", id="quoted-heredoc-with-a-regex"),
        pytest.param("cat <<'TS'\nconst q = `SELECT ${id}`;\nTS", id="quoted-heredoc-with-a-template-literal"),
        pytest.param("cat <<-'EOF'\n\t$x\n\tEOF", id="tab-stripping-quoted-heredoc"),
        pytest.param('cat <<"EOF"\n$x\nEOF', id="double-quoted-delimiter"),
        pytest.param("cat <<'A'\n$1\nA\ncat <<'B'\n`x`\nB", id="two-queued-heredocs"),
        pytest.param('python3 -c "$CODE"', id="an-interpreter-is-not-a-wrapper"),
    ],
)
def test_an_expansion_outside_the_command_name_is_allowed(command: str) -> None:
    """The measured friction this narrowing exists for.

    Every one of these was refused before, and none of them buys the guarantee the check
    makes: each runs a program named by a literal word. The heredoc cases matter twice over
    -- bash expands **nothing** in a body whose delimiter is quoted, so refusing them was
    protecting against a substitution that cannot happen, and it cost a scratchpad file and a
    second Bash call every time the loop wanted to write a script.
    """
    assert cmdshape.unresolved_expansion(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat <<EOF\n$x\nEOF", id="unquoted-delimiter"),
        pytest.param("cat <<EOF\n`date`\nEOF", id="unquoted-delimiter-backtick"),
        pytest.param("cat <<-EOF\n\t$x\n\tEOF", id="unquoted-tab-stripping-delimiter"),
        pytest.param("cat << EOF\n$x\nEOF", id="unquoted-delimiter-after-a-space"),
        pytest.param("cat <<E\\\nOF\n$x\nEOF", id="spliced-delimiter-is-unquoted"),
    ],
)
def test_an_unquoted_heredoc_body_is_still_refused(command: str) -> None:
    """``<<EOF`` *is* expanded by bash, and bashlex files that body under a ``heredoc`` node
    rather than a word -- so the name-only rule would clear it while bash ran the substitution
    inside it. The textual scan has to answer this one, and it denies."""
    assert "heredoc" in cmdshape.unresolved_expansion(command)


def test_an_escaped_dollar_in_an_expanded_heredoc_body_is_not_an_expansion() -> None:
    """A backslash escapes ``$`` inside an unquoted heredoc body, as it does in bash."""
    assert cmdshape.unresolved_expansion("cat <<EOF\n\\$x\nEOF") == ""


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('sh -c "$CMD"', id="sh"),
        pytest.param('/bin/sh -c "$CMD"', id="sh-by-path"),
        pytest.param('bash -c "${CMD}"', id="bash"),
        pytest.param("env $(printf FOO=1) make", id="env"),
        pytest.param("xargs $CMD", id="xargs"),
        pytest.param('eval "$CMD"', id="eval"),
    ],
)
def test_an_expansion_in_an_argument_to_an_exec_wrapper_is_refused(command: str) -> None:
    """``sh -c 'git commit'`` is caught today by ``detection_form``'s quote stripping;
    ``sh -c "$CMD"`` must not become the trivial way around that. A speed bump, not a
    boundary -- ``python3 -c "$CODE"`` walks straight through, as ``python3 script.py``
    always did."""
    assert "in an argument to" in cmdshape.unresolved_expansion(command)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("# <<':'\n$(printf git) commit -m x\n:", id="heredoc-opened-inside-a-comment"),
        pytest.param("echo a;# <<':'\n$(printf git) commit -m x\n:", id="comment-after-a-separator"),
        pytest.param("cat <<E'OF'\nbody\nEOF\n$(printf git) commit -m x", id="delimiter-quoted-in-the-middle"),
        pytest.param('cat <<E"OF"\nbody\nEOF\n$(printf git) commit -m x', id="delimiter-double-quoted-in-the-middle"),
        pytest.param("cat <<E\\OF\nbody\nEOF\n$(printf git) commit -m x", id="delimiter-with-a-backslash"),
        pytest.param("cat <<$E\nbody\n$E\n$(printf git) commit -m x", id="delimiter-holding-an-expansion"),
        pytest.param("\\\n# <<':'\n$(printf git) commit -m x\n:", id="comment-after-a-line-continuation"),
        pytest.param("((1 << 'true'))\n$(printf git) commit -m x\ntrue", id="left-shift-in-an-arithmetic-command"),
        pytest.param("((1 << (2 << 'true')))\n$(printf git) commit -m x\ntrue", id="left-shift-in-nested-arithmetic"),
        pytest.param('cat <<"E\\qOF"\nbody\nE\\qOF\n$(printf git) commit -m x', id="backslash-kept-in-a-double-quoted-delimiter"),
        pytest.param('cat <<"E\\\\OF"\nbody\nE\\OF\n$(printf git) commit -m x', id="escaped-backslash-in-a-delimiter"),
        pytest.param("cat <<'EOF' \\\n arg\nbody\nEOF\n$(printf git) commit -m x", id="continuation-inside-a-heredoc-opener"),
        pytest.param("cat <<E\\\nOF\nbody\nEOF\n$(printf git) commit -m x", id="continuation-inside-the-delimiter"),
        pytest.param("cat <<\\\nEOF\nbody\nEOF\n$(printf git) commit -m x", id="continuation-immediately-after-the-operator"),
        pytest.param("cat << \\\nEOF\nbody\nEOF\n$(printf git) commit -m x", id="continuation-after-the-operator-and-a-space"),
        pytest.param("cat <<-\\\nEOF\nbody\n\tEOF\n$(printf git) commit -m x", id="continuation-after-a-tab-stripping-operator"),
        pytest.param("cat <<E\\\nO\\\nF\nbody\nEOF\n$(printf git) commit -m x", id="two-continuations-in-one-delimiter"),
        pytest.param("cat <<E\\\n'OF'\nbody\nEOF\n$(printf git) commit -m x", id="continuation-then-a-quoted-run"),
    ],
)
def test_the_scan_never_skips_text_bash_executes(command: str) -> None:
    """The scanner may only skip a heredoc body, and only once it knows exactly where that
    body ends. Two ways it could get that wrong, both of which hid a live command name:

    - a ``<<`` inside a **comment** is not a heredoc at all. ``# <<':'`` queued a delimiter of
      ``:``, so the substitution on the next line was read as body and never seen -- while bash
      discarded the comment and ran it. Verified against bash: it executes.
    - a delimiter must be read as a whole word with bash's quote removal. ``<<E'OF'`` delimits
      on ``EOF``; resolving it as ``E'OF'`` runs past the real terminator and swallows the
      lines after it, which bash executes. Quote removal has to be bash's exactly: inside
      double quotes a backslash is dropped only before ``$``, a backtick, ``"``, ``\\`` or a
      newline, so ``<<"E\\qOF"`` delimits on ``E\\qOF`` and not on ``EqOF``.
    - a ``\\``-newline is a line continuation, not an escape, both in the command text and
      *inside the delimiter word*. In the text, treating it as an escape left a word open, so
      the ``#`` that followed was not read as a comment and the ``<<`` inside that comment
      opened a heredoc. In the delimiter, it produced a delimiter with a newline in it --
      which no line can ever equal -- so the terminator was never found and everything to the
      end of the text was skipped as body. ``<<E\\``-newline-``OF`` delimits on ``EOF``.
    - ``<<`` inside ``(( ))`` is a left shift. bash's arithmetic merely fails on
      ``((1 << 'true'))`` and carries on to the next line; a scan that read it as a redirect
      skipped that line instead.

    Every payload here was run under real bash: each one executes ``$(printf git) commit -m x``,
    and none carries a literal ``git commit`` for detection to match. Each is a command whose
    *name* is an expansion, so each must be refused.
    """
    assert cmdshape.unresolved_expansion(command)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("echo a#b", id="hash-inside-a-word-is-not-a-comment"),
        pytest.param("grep '#' file", id="hash-inside-quotes-is-not-a-comment"),
        pytest.param("make build  # runs the build", id="an-ordinary-trailing-comment"),
        pytest.param("cat <<'EOF'\n# <<':'\nEOF", id="a-comment-inside-a-quoted-heredoc-body"),
        pytest.param("cat <<'EOF' # why\n$x\nEOF", id="a-comment-after-a-heredoc-opener"),
        pytest.param("((1+1)) && cat <<'EOF'\n$x\nEOF", id="a-heredoc-after-a-closed-arithmetic-command"),
        pytest.param("make a \\\n  && make b", id="an-ordinary-line-continuation"),
        pytest.param('cat <<"E\\qOF"\n$x\nE\\qOF', id="a-double-quoted-delimiter-keeping-its-backslash"),
        pytest.param("cat <<E\\\nOF\nplain body\nEOF", id="a-delimiter-spliced-by-a-continuation"),
        pytest.param("python3 - \\\n  --flag <<'PY'\n$x\nPY", id="a-continuation-before-a-heredoc-opener"),
    ],
)
def test_comment_handling_matches_bash(command: str) -> None:
    """``#`` opens a comment only where a word is not already open -- bash's own rule, and the
    one ``_deny_shell_grammar`` already implements. A comment after a heredoc opener must not
    consume the newline: the body still starts on the next line.

    The last four are the other direction of the same rules: closing ``))`` re-enables heredoc
    recognition, an ordinary continuation is not a heredoc opener, a delimiter whose backslash
    bash *keeps* must still match its own terminator, and one spliced by a continuation must
    match the terminator bash splices it into.
    """
    assert cmdshape.unresolved_expansion(command) == ""


def test_a_delimiter_the_scan_cannot_resolve_is_not_a_heredoc() -> None:
    """An unterminated quote leaves the delimiter's end unknowable, so ``_heredoc_delimiter``
    answers ``None`` and no body is skipped.

    What the text then reads as is an unterminated single quote running to the end -- which is
    exactly what bash reads it as. Verified against bash: it refuses the command outright
    (``unexpected EOF while looking for "'"``, exit 2) and runs nothing, so there is no
    expansion here for the gate to have missed.
    """
    assert cmdshape.unresolved_expansion("cat <<'EOF\nbody\n$(printf git) commit") == ""


def test_a_quoted_heredoc_plus_a_stray_expansion_denies_on_the_parse() -> None:
    """The scan clears the heredoc body and flags the ``$`` after it, but bashlex cannot parse
    a multi-line heredoc at all, so there is nothing to reason about and the refusal stands.
    Over-refusal is the safe direction here."""
    assert cmdshape.unresolved_expansion("cat <<'PY'\n$x\nPY\necho $Y")


def test_the_narrowing_does_not_reach_the_commit_deny_list() -> None:
    """The deny-list did not move. ``unresolved_expansion`` lets this through -- the name is
    the literal word ``git`` -- and ``validate_commit`` refuses it exactly as before."""
    assert cmdshape.unresolved_expansion('git commit -m "$(x)"') == ""
    with pytest.raises(cmdshape.CommandShapeError, match=r'contains "\$"'):
        cmdshape.validate_commit('git commit -m "$(x)"')


def test_a_heredoc_naming_a_commit_still_routes_to_the_commit_gate() -> None:
    """The heredoc allowance must not become a way to hide a commit. ``mentions_commit`` reads
    the raw text multiline, so a body containing the words ``git commit`` is still detected --
    and ``validate_commit`` then refuses the shape."""
    command = "cat <<'PY'\nsubprocess.run('git commit -m x')\nPY"
    assert cmdshape.unresolved_expansion(command) == ""
    assert cmdshape.mentions_commit(command)
    with pytest.raises(cmdshape.CommandShapeError):
        cmdshape.validate_commit(command)


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
