"""Proof that the Python toolchain is wired up: ruff, mypy and pytest all see real input."""

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

import sys

import arl


def test_python_floor() -> None:
    """The port targets Ubuntu 24.04's system Python and never anything older."""
    assert sys.version_info >= (3, 12)


def test_plugin_root_is_the_checkout() -> None:
    assert (arl.PLUGIN_ROOT / "scripts" / "arl.sh").is_file()
    assert arl.PACKAGE_ROOT.parent.name == "scripts"


def test_prompt_path_resolves_shipped_prompts() -> None:
    for name in ("reviewer-phase", "reviewer-final"):
        assert arl.prompt_path(name).is_file()
