"""Bash pattern matching, as ``[[ $path == $glob ]]`` performs it.

``ignore_globs`` decides whether a review is **skipped**, so the matcher is part of the
gate: a pattern that matches more than the user meant silently stops reviewing code. The
shell compared with ``[[ $p == $g ]]``, and these patterns were written against those rules,
so those are the rules reproduced here.

``fnmatch`` is *not* those rules, which is why this module exists. Three divergences, all of
which make ``fnmatch`` match **more** than bash and therefore skip reviews bash would run:

* ``[^a]`` is a negated class in bash. ``fnmatch`` only understands ``[!a]``, and reads
  ``[^a]`` as the set ``{'^', 'a'}`` -- so ``[^a]`` matches ``a``, the exact inverse of
  what it says.
* ``\\`` escapes the next character in bash: ``a\\*b`` matches the literal ``a*b`` and
  nothing else. ``fnmatch`` has no escape, so the same pattern matches ``a<anything>b``.
* POSIX classes (``[[:alpha:]]``) are ordinary members of a ``fnmatch`` set, so
  ``[[:alpha:]]`` matches ``[``, ``:``, ``a`` … rather than one letter.

Every rule below was pinned against a real bash before being implemented, including the odd
ones: an unterminated ``[`` is a literal ``[``; ``]`` first in a set is a member; and an
invalid range (``[z-a]``) or unknown class (``[[:bogus:]]``) contributes **no** members
while leaving the rest of the set, and any negation, intact -- so ``[^[:bogus:]]`` matches
any single character.

Two constructs are **deliberately not reproduced, and never match**:

* **Extended globs** -- ``@(a|b)``, ``!(x)``, ``+(a)``, ``?(a)``, ``*(a)``. ``[[ … == … ]]``
  honours these *unconditionally*: measured, ``shopt extglob`` reports ``off`` and
  ``[[ a == @(a|b) ]]`` still matches. Reproducing ``!(…)`` means negating a pattern
  language inside the matcher, which is a great deal of machinery for a construct no
  ``ignore_globs`` has ever plausibly needed. An escaped ``\\@(a|b)`` is literal in bash and
  is matched literally here, as usual.
* **Locale tables.** POSIX classes, ranges and equivalence classes are answered by the C
  library from the active locale, and no Python predicate reproduces those tables. Measured
  under ``it_IT.UTF-8``: bash says U+0661, the Arabic-Indic digit one, is ``[[:alpha:]]``
  and not ``[[:punct:]]``, where Python's ``str`` predicates say the opposite of both; U+0085 is
  ``[[:space:]]`` to Python and not to bash. So a class or range decides only **ASCII**
  characters, where every locale agrees with POSIX and the test suite checks all 127 against
  bash. A non-ASCII character reaching a class or range -- or a range with a non-ASCII
  endpoint, whose ordering is collation and not code points -- makes the pattern unmatchable.

Both refusals point the same way, and that is the property to preserve: **a pattern whose
meaning cannot be reproduced matches nothing**, so it can only cause a review that bash
would have skipped, never skip one bash would have run.

Matching is iterative with a single backtrack point for ``*``, so a pattern like ``*a*a*a``
cannot blow up on a long path the way a naive recursive matcher would.
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

from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Final

__all__ = ["matches"]

# Item kinds, in the compiled pattern.
_STAR: Final = 0
_ANY: Final = 1
_LIT: Final = 2
_SET: Final = 3

_Item = tuple[int, object]

#: POSIX character classes, spelled out over ASCII.
#:
#: Written as explicit sets rather than as ``str.isalpha`` and friends because only ASCII
#: ever reaches them -- anything else is refused above -- and over ASCII every locale agrees
#: with POSIX, which makes these exact rather than approximate. Python's own predicates are
#: *not* the same answer: ``"\x1c".isspace()`` is true and ``isspace(0x1c)`` is false in C,
#: and each class here is checked against bash for all 127 characters.
_ALPHA: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIGIT: Final = "0123456789"

_CLASSES: Final[dict[str, Callable[[str], bool]]] = {
    "alnum": lambda c: c in _ALPHA or c in _DIGIT,
    "alpha": lambda c: c in _ALPHA,
    "blank": lambda c: c in " \t",
    "cntrl": lambda c: c < " " or c == "\x7f",
    "digit": lambda c: c in _DIGIT,
    "graph": lambda c: "!" <= c <= "~",
    "lower": lambda c: "a" <= c <= "z",
    "print": lambda c: " " <= c <= "~",
    "punct": lambda c: "!" <= c <= "~" and c not in _ALPHA and c not in _DIGIT,
    "space": lambda c: c in " \t\n\v\f\r",
    "upper": lambda c: "A" <= c <= "Z",
    "xdigit": lambda c: c in "0123456789abcdefABCDEF",
}


#: Characters that introduce an extended glob when followed by "(".
_EXTGLOB_LEADERS: Final = "?*+@!"


class _Unprovable(Exception):
    """This decision needs the C library's locale tables, which cannot be reproduced.

    Raised from deep inside the match rather than answered False, because "not a member" is
    not a safe stand-in: under a negated set it becomes "matches", which is the direction
    that skips a review. The whole pattern is abandoned instead.
    """


class _Set:
    """One bracket expression, evaluated against a single character."""

    __slots__ = ("chars", "classes", "locale_bound", "negate", "ranges", "unprovable")

    def __init__(
        self,
        *,
        negate: bool,
        chars: str,
        ranges: Sequence[tuple[str, str]],
        classes: Sequence[Callable[[str], bool]],
        unprovable: bool,
    ) -> None:
        self.negate = negate
        self.chars = chars
        self.ranges = tuple(ranges)
        self.classes = tuple(classes)
        # A range whose endpoints are not both ASCII is ordered by collation, so even an
        # ASCII character's membership is a locale question.
        self.unprovable = unprovable or any(not (low.isascii() and high.isascii()) for low, high in self.ranges)
        # Classes and ranges are locale data; plain members are decided by equality, which
        # is the same everywhere.
        self.locale_bound = bool(self.classes or self.ranges)

    def matches(self, char: str) -> bool:
        if self.unprovable or (self.locale_bound and not char.isascii()):
            raise _Unprovable
        member = char in self.chars or any(low <= char <= high for low, high in self.ranges) or any(test(char) for test in self.classes)
        return member != self.negate


def _parse_class(pattern: str, index: int) -> tuple[Callable[[str], bool] | None, int, bool] | None:
    """Parse ``[:name:]``, ``[=c=]`` or ``[.c.]`` at ``index``.

    Returns the predicate (``None`` for a construct with no members), the index after it,
    and whether the construct puts the whole set beyond what can be proven. ``None`` when
    this is not one of those constructs at all.
    """
    if index + 1 >= len(pattern) or pattern[index] != "[" or pattern[index + 1] not in ":.=":
        return None
    kind = pattern[index + 1]
    close = pattern.find(f"{kind}]", index + 2)
    if close == -1:
        return None
    name = pattern[index + 2 : close]
    after = close + 2
    if kind == ":":
        # An unknown class contributes no members, exactly as bash does -- and the
        # surrounding set, negation included, still applies.
        return _CLASSES.get(name), after, False
    # Equivalence and collating classes are collation data. For an ASCII character the only
    # member that can be justified is the character itself, which is what glibc answers
    # here; a non-ASCII one could equate accented forms in some locale, so the set is
    # abandoned rather than guessed.
    return (lambda c, want=name: c == want), after, not name.isascii()


def _parse_set(pattern: str, index: int) -> tuple[_Set, int] | None:
    """Parse a bracket expression whose ``[`` is at ``index - 1``.

    ``None`` when it is not terminated, which makes the ``[`` an ordinary character.
    """
    negate = False
    if index < len(pattern) and pattern[index] in "!^":
        negate = True
        index += 1

    chars: list[str] = []
    ranges: list[tuple[str, str]] = []
    classes: list[Callable[[str], bool]] = []
    unprovable = False
    first = True

    while True:
        if index >= len(pattern):
            return None  # unterminated
        char = pattern[index]
        if char == "]" and not first:
            bracket = _Set(negate=negate, chars="".join(chars), ranges=ranges, classes=classes, unprovable=unprovable)
            return bracket, index + 1
        first = False

        parsed = _parse_class(pattern, index)
        if parsed is not None:
            predicate, index, needs_locale = parsed
            unprovable = unprovable or needs_locale
            if predicate is not None:
                classes.append(predicate)
            continue

        if char == "\\" and index + 1 < len(pattern):
            index += 1
            char = pattern[index]
        index += 1

        # A "-" is a range only between two members; last before "]" it is a member itself.
        if index + 1 < len(pattern) and pattern[index] == "-" and pattern[index + 1] != "]":
            index += 1
            if pattern[index] == "\\" and index + 1 < len(pattern):
                index += 1
            high = pattern[index]
            index += 1
            # A reversed range has no members in bash, and does not poison the rest of the set.
            if char <= high:
                ranges.append((char, high))
        else:
            chars.append(char)


@lru_cache(maxsize=256)
def _compile(pattern: str) -> tuple[_Item, ...] | None:
    """Split a pattern into matchable items. Cached: the same globs run over many paths.

    ``None`` when the pattern uses an extended glob, which means it matches nothing.
    """
    items: list[_Item] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        index += 1
        if char in _EXTGLOB_LEADERS and index < len(pattern) and pattern[index] == "(":
            return None
        if char == "*":
            # Collapsed, so "**" cannot double the backtracking work for no extra meaning.
            if not items or items[-1][0] != _STAR:
                items.append((_STAR, None))
        elif char == "?":
            items.append((_ANY, None))
        elif char == "\\":
            if index < len(pattern):
                items.append((_LIT, pattern[index]))
                index += 1
            else:
                items.append((_LIT, "\\"))  # a trailing backslash is a literal one
        elif char == "[":
            parsed = _parse_set(pattern, index)
            if parsed is None:
                items.append((_LIT, "["))
            else:
                bracket, index = parsed
                items.append((_SET, bracket))
        else:
            items.append((_LIT, char))
    return tuple(items)


def _item_matches(item: _Item, char: str) -> bool:
    kind, payload = item
    if kind == _ANY:
        return True
    if kind == _LIT:
        return char == payload
    assert isinstance(payload, _Set)
    return payload.matches(char)


def matches(text: str, pattern: str) -> bool:
    """Is ``text`` matched by ``pattern``, under bash's ``[[ … == … ]]`` rules?

    The whole string must match, and ``*`` and ``?`` cross ``/`` -- ``[[ … ]]`` compares
    strings and knows nothing about path components.

    False whenever the answer cannot be proven, which is the direction that reviews rather
    than skips. See the module docstring for the two constructs that take that path.
    """
    items = _compile(pattern)
    if items is None:
        return False
    try:
        return _match_items(items, text)
    except _Unprovable:
        return False


def _match_items(items: tuple[_Item, ...], text: str) -> bool:
    index = 0
    position = 0
    star = -1
    resume = 0

    while position < len(text):
        if index < len(items) and items[index][0] != _STAR and _item_matches(items[index], text[position]):
            index += 1
            position += 1
        elif index < len(items) and items[index][0] == _STAR:
            # Remember where to resume if the rest of the pattern fails from here.
            star = index
            resume = position
            index += 1
        elif star != -1:
            resume += 1
            index = star + 1
            position = resume
        else:
            return False

    while index < len(items) and items[index][0] == _STAR:
        index += 1
    return index == len(items)
