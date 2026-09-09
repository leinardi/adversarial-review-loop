"""The small shared helpers in ``arl.util``."""

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

from arl.util import format_at


def test_format_at_renders_a_plain_epoch_as_utc() -> None:
    assert format_at(1_757_000_000) == "2025-09-04T15:33:20Z"
    assert format_at("1757000000") == "2025-09-04T15:33:20Z"


@pytest.mark.parametrize(
    "value",
    [
        2**62,  # OverflowError / OSError out of the platform's C library, not ValueError
        -(2**62),
        10**30,
        True,  # `int(True) == 1`, so the bool must be refused before the coercion
        None,
        {"at": 1},
        ["1757000000"],
        "not a time",
    ],
)
def test_format_at_answers_for_anything_a_state_document_can_hold(value: object) -> None:
    """``state.json`` is not a trust boundary, so this must never raise.

    Raising inside a hook is a fail-closed denial that says nothing useful; a report naming
    an unreadable time is still a report. ``fromtimestamp`` raises ``OverflowError`` or
    ``OSError`` -- not only ``TypeError``/``ValueError`` -- for an out-of-range value, which
    is exactly what an edited document supplies.
    """
    assert format_at(value) == "(unknown time)"
