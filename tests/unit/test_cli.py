"""The dispatcher: which name reaches which command, and what an unknown name does."""

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

from arl import cli


@pytest.mark.parametrize("argv", [[], ["help"], ["-h"], ["--help"]])
def test_help_is_the_only_thing_a_bare_invocation_does(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == cli.USAGE


def test_an_unknown_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """Two, the usage-error status, with the usage on stderr and nothing on stdout."""
    assert cli.main(["not-a-subcommand"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"arl: unknown subcommand 'not-a-subcommand'\n{cli.USAGE}"
