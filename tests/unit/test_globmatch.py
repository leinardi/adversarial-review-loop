"""Glob matching, compared case by case against a real bash.

``ignore_globs`` decides whether a review is skipped, so a matcher that is merely
"close enough" to ``[[ $p == $g ]]`` is a way for code to reach a commit unreviewed. Every
case below is checked against bash itself; the hand-written expectations exist so that a
disagreement points at which of the two is wrong.
"""

from __future__ import annotations

import itertools

import pytest
from conftest import bash_glob

from ocrl import globmatch

#: Path, glob and verdict, stated outright: these are the cases a plausible implementation
#: gets wrong.
STATED = [
    # `^` negates in bash. fnmatch reads this set as {'^', 'a'} and matches -- inverted.
    ("a", "[^a]", False),
    ("b", "[^a]", True),
    ("^", "[^a]", True),
    ("a", "[!a]", False),
    ("b", "[!a]", True),
    # A backslash escapes. fnmatch has no escape, so it reads `\*` as "anything".
    ("a*b", r"a\*b", True),
    ("axb", r"a\*b", False),
    ("a", r"\a", True),
    ("?", r"\?", True),
    ("x", r"\?", False),
    ("\\", "\\\\", True),
    ("a\\", "a\\", True),
    ("a", "a\\", False),
    # POSIX classes are one character, not a set of literals.
    ("x", "[[:alpha:]]", True),
    ("1", "[[:alpha:]]", False),
    ("1", "[^[:alpha:]]", True),
    ("x", "[^[:alpha:]]", False),
    ("A", "[[:upper:]]", True),
    ("5", "[[:alpha:][:digit:]]", True),
    ("_", "[[:alpha:][:digit:]]", False),
    ("f", "[[:xdigit:]]", True),
    ("g", "[[:xdigit:]]", False),
    (" ", "[[:blank:]]", True),
    # An invalid class or range has no members, and the rest of the set still stands.
    ("x", "[[:bogus:]]", False),
    ("x", "[^[:bogus:]]", True),
    ("a", "[a[:bogus:]b]", True),
    ("x", "[a[:bogus:]b]", False),
    ("a", "[z-a]", False),
    ("z", "[z-a]", False),
    ("b", "[^z-a]", True),
    # Bracket-expression corners.
    ("]", "[]]", True),
    ("]", "[]a]", True),
    ("a", "[]a]", True),
    ("x", "[!]a]", True),
    ("]", "[!]a]", False),
    ("]", r"[\]]", True),
    ("-", "[a-]", True),
    ("-", "[-a]", True),
    ("b", r"[a\-c]", False),
    ("-", r"[a\-c]", True),
    ("b", "[a-c]", True),
    ("A", "[a-z]", False),
    # An unterminated bracket is a literal one.
    ("[a", "[a", True),
    ("a", "[a", False),
    ("a[]b", "a[]b", True),
    # `*` and `?` are string operators here: they cross `/`, and `.` is not special.
    ("a/b/c", "*", True),
    ("src/deep/file.md", "*.md", True),
    ("a.b", "a.b", True),
    ("axb", "a.b", False),
    ("", "*", True),
    ("", "?", False),
    ("", "", True),
    ("a", "", False),
    ("docs/guide.md", "docs/*", True),
    ("docs/nested/guide.md", "docs/*", True),
]


@pytest.mark.parametrize(("path", "glob", "expected"), STATED)
def test_stated_cases(path: str, glob: str, expected: bool) -> None:
    assert globmatch.matches(path, glob) is expected
    assert bash_glob(path, glob) is expected, "bash disagrees with the expectation itself"


# A cross product, to catch the cases nobody thought to state.
PATHS = [
    "a",
    "b",
    "^",
    "-",
    "]",
    "[",
    "*",
    "?",
    "\\",
    "1",
    "A",
    " ",
    ".",
    "ab",
    "a*b",
    "a.b",
    "a/b",
    "docs/guide.md",
    "src/main.py",
    "CHANGELOG.md",
    "",
]

GLOBS = [
    "*",
    "**",
    "?",
    "*.md",
    "*.py",
    "docs/*",
    "a*b",
    r"a\*b",
    "[ab]",
    "[!ab]",
    "[^ab]",
    "[a-c]",
    "[!a-c]",
    "[]]",
    "[",
    "[a",
    r"\?",
    r"\\",
    "[[:alpha:]]",
    "[^[:alpha:]]",
    "[[:digit:]]*",
    "[[:punct:]]",
    "[[:space:]]",
    "a.b",
    "",
    "*/*",
    "?*",
]


@pytest.mark.parametrize(("path", "glob"), list(itertools.product(PATHS, GLOBS)))
def test_every_pair_agrees_with_bash(path: str, glob: str) -> None:
    assert globmatch.matches(path, glob) is bash_glob(path, glob)


# -- the invariant that matters --------------------------------------------

#: Everything the differential tests cover, as (path, glob) pairs.
ALL_PAIRS = (
    [(path, glob) for path, glob, _ in STATED]
    + list(itertools.product(PATHS, GLOBS))
    + list(
        itertools.product(
            ["a", "b", "src", "docs", "@(a|b)", "!(b)", "aaa", "ax", "x", "a(b", "docs/x.md"],
            ["@(a|b)", "@(src|docs)", "!(b)", "!(docs)", "+(a)", "?(a)x", "*(a)", "@(a|b)*", "*@(a|b)", r"\@(a|b)", r"a\(b", "!(docs)/*"],
        )
    )
    + list(
        itertools.product(
            ["é", "É", "\u0661", "\u0085", "\u00a0", "naïve.md", "a", " ", "1"],
            [
                "[[:alpha:]]",
                "[^[:alpha:]]",
                "[[:punct:]]",
                "[[:space:]]",
                "[[:digit:]]",
                "[a-z]",
                "[^a-z]",
                "[abc]",
                "[^abc]",
                "?",
                "*",
                "*.md",
                "[a-é]",
                "[[=a=]]",
            ],
        )
    )
)


@pytest.mark.parametrize(("path", "glob"), ALL_PAIRS)
def test_a_review_is_never_skipped_where_bash_would_review(path: str, glob: str) -> None:
    """The one property that cannot be traded away.

    Matching where bash does not means `all_paths_ignored` skips a review bash would have
    run -- code reaching a commit unreviewed. The converse, matching less than bash, only
    costs a review that was not strictly required, and is how the unreproducible constructs
    are handled deliberately.
    """
    if globmatch.matches(path, glob):
        assert bash_glob(path, glob), f"{glob!r} matches {path!r} here but not in bash: a review would be skipped"


# -- extended globs: honoured by bash unconditionally, refused here --------

EXTGLOB = [
    ("a", "@(a|b)"),
    ("b", "@(a|b)"),
    ("src", "@(src|docs)"),
    ("src", "!(docs)"),
    ("aaa", "+(a)"),
    ("ax", "?(a)x"),
    ("x", "?(a)x"),
    ("aa", "*(a)"),
    ("src/x.md", "!(docs)/*"),
]


@pytest.mark.parametrize(("path", "glob"), EXTGLOB)
def test_an_extended_glob_matches_nothing(path: str, glob: str) -> None:
    """`[[ … ]]` honours extglob even with `shopt extglob` off, so bash matches these.

    Reproducing `!(…)` means negating a pattern language inside the matcher. Refusing
    instead costs a review that bash would have skipped, which is the affordable direction.
    """
    assert bash_glob(path, glob) is True, "bash is expected to honour the extended glob"
    assert globmatch.matches(path, glob) is False


def test_the_literal_reading_of_an_extended_glob_is_not_a_match_either() -> None:
    """The case that started this: bash does *not* match `@(a|b)` against itself."""
    assert bash_glob("@(a|b)", "@(a|b)") is False
    assert globmatch.matches("@(a|b)", "@(a|b)") is False


@pytest.mark.parametrize("glob", [r"\@(a|b)", r"a\(b", "[@]", "a@b"])
def test_escaped_and_ordinary_parentheses_still_work(glob: str) -> None:
    """Only an *unescaped* operator followed by `(` is an extended glob."""
    for path in ("@(a|b)", "a(b", "@", "a@b", "a"):
        assert globmatch.matches(path, glob) is bash_glob(path, glob)


# -- POSIX classes: exact for ASCII, refused beyond it ---------------------

#: NUL is left out because neither side can be handed one: it cannot travel through argv,
#: and a shell variable cannot hold it either, so no glob or path ever contains one.
ASCII = [chr(code) for code in range(1, 128)]
CLASS_NAMES = ["alnum", "alpha", "blank", "cntrl", "digit", "graph", "lower", "print", "punct", "space", "upper", "xdigit"]


@pytest.mark.parametrize("name", CLASS_NAMES)
def test_every_posix_class_agrees_with_bash_across_all_of_ascii(name: str) -> None:
    """128 characters per class, because "close enough" is not a property one can assert."""
    glob = f"[[:{name}:]]"
    mismatched = [char for char in ASCII if globmatch.matches(char, glob) is not bash_glob(char, glob)]
    assert mismatched == [], f"[[:{name}:]] disagrees for {[hex(ord(c)) for c in mismatched]}"


@pytest.mark.parametrize("name", CLASS_NAMES)
def test_every_negated_posix_class_agrees_with_bash_across_all_of_ascii(name: str) -> None:
    glob = f"[^[:{name}:]]"
    mismatched = [char for char in ASCII if globmatch.matches(char, glob) is not bash_glob(char, glob)]
    assert mismatched == [], f"[^[:{name}:]] disagrees for {[hex(ord(c)) for c in mismatched]}"


#: Characters whose class membership Python and the C library genuinely disagree about.
LOCALE_TRAPS = [
    ("\u0661", "[[:punct:]]"),  # Arabic-Indic one: punct to Python, not to bash
    ("\u0661", "[[:alpha:]]"),  # ... and alpha to bash, not to Python
    ("\u0085", "[[:space:]]"),  # NEL: space to Python, not to bash
    ("\u00a0", "[[:space:]]"),  # NBSP: likewise
    ("é", "[[:alpha:]]"),
    ("é", "[^[:alpha:]]"),
    ("é", "[a-z]"),
    ("é", "[^a-z]"),
]


@pytest.mark.parametrize(("path", "glob"), LOCALE_TRAPS)
def test_a_class_or_range_never_decides_a_non_ascii_character(path: str, glob: str) -> None:
    """Membership beyond ASCII is a locale table, and no Python predicate reproduces it.

    Answering "not a member" would be worse than refusing: under a negated set it becomes
    "matches", which skips a review.
    """
    assert globmatch.matches(path, glob) is False


def test_a_non_ascii_range_endpoint_makes_the_pattern_unmatchable() -> None:
    """`[a-é]` is ordered by collation, not by code point, so it decides nothing here."""
    for path in ("b", "é", "a"):
        assert globmatch.matches(path, "[a-é]") is False


def test_plain_members_still_decide_non_ascii_characters() -> None:
    """Equality is not locale data, so an ordinary set is answered as bash answers it."""
    for path, glob in [("é", "[abc]"), ("é", "[^abc]"), ("é", "[éa]"), ("é", "?"), ("naïve.md", "*.md")]:
        assert globmatch.matches(path, glob) is bash_glob(path, glob)


def test_a_pathological_pattern_does_not_blow_up() -> None:
    """A naive recursive matcher goes exponential here; this one must not."""
    assert globmatch.matches("a" * 200 + "b", "*a*a*a*a*a*b") is True
    assert globmatch.matches("a" * 200, "*a*a*a*a*a*b") is False
