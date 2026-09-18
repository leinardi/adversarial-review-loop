"""One spelling rule for ``set-phases``, stated before the descriptions are written.

``set-phases`` is the only command permitted while the phase list is unfrozen, so the gate reads
it with the commit tokenizer, which refuses a ``$``, a backtick and a newline anywhere in the
command. Those are exactly the characters prose about code reaches for -- a phase description
naming a file wants backticks around it, and a long phase list wants to wrap across lines.

The rule used to appear in one place only: the denial a refused freeze gets. Everything the model
reads *before* writing descriptions -- both banners, the "phases are not frozen" denials, the two
skill bodies -- printed the bare command template and said nothing about how to spell it, so the
first attempt failed by construction and the freeze routinely took two rounds or more.

The runtime surfaces assert their own emission (``test_commands_pretool`` for the denials,
``test_commands_phases`` for the replan fence). This module covers what those cannot: the skill
bodies, which are files rather than output, and the shared constant every surface splices. It is
a text check by necessity, so it asserts the load-bearing fragments rather than whole sentences,
which would fail on rewording that changed nothing.

A stale installed copy of a skill body is the failure no test here can reach (AGENTS.md, "The
install cache"), which is why the banners carry the rule too -- ``scripts/`` is served from the
working tree either way.
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

from arl.commands import hooks

#: The three characters the tokenizer refuses. Stating two of them without the newline is what
#: the old single-cause denial did, and a wrapped command is the failure that exposed it.
SINGLE_LINE = "single line"
BACKTICK = "backtick"
DOLLAR = "`$`"


@pytest.mark.parametrize("skill", ["implement", "resume"])
def test_the_skill_bodies_carry_the_phase_spelling_rule(skill: str) -> None:
    """``implement`` states it for a fresh activation, ``resume`` for a continued one.

    ``resume`` needs its own copy for the same reason it needs its own commit rule: a resumed
    session may freeze or redefine phases without ever having read ``implement``'s body.
    """
    text = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")

    assert SINGLE_LINE in text
    assert BACKTICK in text
    assert DOLLAR in text


def test_every_surface_states_it_from_one_source() -> None:
    """The banners and the denials splice one constant, so the rule cannot drift between them."""
    assert SINGLE_LINE in hooks.PHASE_CONSTRAINTS
    assert BACKTICK in hooks.PHASE_CONSTRAINTS
    assert DOLLAR in hooks.PHASE_CONSTRAINTS


def test_the_constraints_name_only_what_the_tokenizer_actually_refuses() -> None:
    """Three rules, no more.

    A quoted ``;``, ``|`` or ``*`` inside a description is accepted, and so is an escaped quote
    (``test_a_phase_description_may_quote_something``). Listing them here would refuse in the
    prompt what the gate allows, and push the model into rewording descriptions that were fine.
    """
    body = hooks.PHASE_CONSTRAINTS

    assert "semicolon" not in body
    assert "glob" not in body
    assert "escaped quote" not in body
