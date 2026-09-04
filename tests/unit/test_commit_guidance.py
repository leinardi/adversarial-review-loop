"""One commit rule, stated the same way on every surface that states it at all.

A commit **body** is written as repeated message options, one paragraph each -- ``-m``, or the
attached ``--message="…"``, which the allowlist accepts too. The two alternatives a model
actually reaches for are both refused: a real newline anywhere in the command dies in
``_deny_shell_grammar`` before the parser runs, and ``-F``/``--file`` is off the allowlist.

That rule used to live in ``skills/implement/SKILL.md`` and nowhere else. A session resumed
through ``/adversarial-review-loop:resume`` does not have that body, and one duly tried
``git commit -F <path>``, was told only that ``-F`` was "not on the allowlist", tried a
newline inside ``-m``, and lost a second round -- both denials correct, neither naming the
form that works.

The runtime surfaces each assert their own emission (``test_commands_pretool`` for the denial,
``test_commands_arm`` and ``test_commands_resume`` for the two banners, ``test_commands_session``
for ``reorient``). This module covers what those cannot: the skill bodies, which are files
rather than output, and the shared constant the two banners splice. It is a text check by
necessity -- there is no interface to interrogate -- so it asserts the two load-bearing
fragments rather than a whole paragraph, which would fail on rewording that changed nothing.

What no test in this repository can cover is a *stale installed copy* of a skill body reaching
the model: an install serves ``skills/*/SKILL.md`` from the plugin cache, refreshed only on a
version bump, while everything under ``scripts/`` is served from the working tree (AGENTS.md,
"The install cache"). That is why the banners carry the rule too, and why the manual check
stays in the plan.
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

import pytest
from conftest import PLUGIN_ROOT

from arl.commands import arm

#: The form that works, and the two that do not. Both must appear wherever the rule is stated:
#: naming the refusals without the recipe is exactly the denial that cost a real run two rounds.
RECIPE = '`-m "subject" -m "body"`'
REFUSALS = "`-F`/`--file`, are both refused"


@pytest.mark.parametrize("skill", ["implement", "resume"])
def test_the_skill_bodies_carry_the_commit_body_rule(skill: str) -> None:
    """``implement`` states the rules for a fresh activation, ``resume`` for a continued one.

    ``resume``'s section used to defer to ``implement``'s by reference -- which is precisely the
    body its reader does not have, so the deferral resolved to nothing.
    """
    text = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")

    assert RECIPE in text
    assert REFUSALS in text


def test_the_two_banners_state_it_from_one_source() -> None:
    """``arm`` and ``resume`` splice the same constant, so the rule cannot drift between them.

    Asserted on the constant as well as on each banner's output: the per-banner tests prove the
    text is emitted, this proves there is one text to emit rather than two that agree today.
    """
    assert RECIPE in arm.COMMIT_CONSTRAINTS
    assert REFUSALS in arm.COMMIT_CONSTRAINTS
