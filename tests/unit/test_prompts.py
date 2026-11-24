"""What every reviewer round prompt tells the reviewer about delivering its answer.

A reviewer in an auto-mode session hands its report back with ``SubagentHandback``, and the
engine reads the report it handed back. In a runhold run a reviewer handed back its prose
alone, twice: no findings block, so a review that had approved became a failed round.
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

import arl

#: The prompts a review round runs on; the repair prompt asks for the block alone.
ROUND_PROMPTS = ("reviewer-phase", "reviewer-phase-late", "reviewer-final")


@pytest.mark.parametrize("name", ROUND_PROMPTS)
def test_a_handed_back_report_must_carry_the_findings_block(name: str) -> None:
    """**Fails on the old prompts**, which said nothing about a handback."""
    text = arl.prompt_path(name).read_text(encoding="utf-8")
    contract = text.partition("## Output contract")[2]
    rule = contract.partition("<<<ARL-FINDINGS>>>")[0]
    assert "`SubagentHandback`" in rule
    assert "the prose, then this block" in rule


def test_the_phase_prompt_says_a_phase_with_no_change_is_reviewed_on_its_claim() -> None:
    """**Fails on the old prompt**, which never said what an empty ``changes.00.diff`` means."""
    text = arl.prompt_path("reviewer-phase").read_text(encoding="utf-8")
    given = text.partition("## What you have been given")[2].partition("\n## ")[0]
    assert "**A phase that changed nothing**" in given
    assert "approve only when every clause is already met" in given
