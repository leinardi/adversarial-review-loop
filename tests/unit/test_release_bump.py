"""Tests for scripts/release_bump.py, the make release-bump writer of plugin.json."""

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

import json
import subprocess
import sys
from pathlib import Path

import pytest
import release_bump
from conftest import PLUGIN_ROOT

SCRIPT = PLUGIN_ROOT / "scripts" / "release_bump.py"
PLUGIN_JSON = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, check=False)


def test_the_real_plugin_json_bumps_on_one_line() -> None:
    text = PLUGIN_JSON.read_text(encoding="utf-8")
    new = release_bump.bump(text, "9.8.7")
    assert json.loads(new)["version"] == "9.8.7"
    changed = [(a, b) for a, b in zip(text.splitlines(), new.splitlines(), strict=True) if a != b]
    assert len(changed) == 1
    assert changed[0][1] == '  "version": "9.8.7",'


@pytest.mark.parametrize("version", ["1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "v1.2.3", "1.2.3-rc.1", "1.2.3+b", " 1.2.3", ""])
def test_rejects_a_version_that_is_not_strict(version: str) -> None:
    with pytest.raises(ValueError, match=r"X\.Y\.Z"):
        release_bump.bump('{\n  "version": "0.1.0"\n}\n', version)


def test_only_the_top_level_version_changes() -> None:
    text = '{\n  "name": "p",\n  "version": "0.1.0",\n  "author": {\n    "version": "keep"\n  }\n}\n'
    new = release_bump.bump(text, "0.2.0")
    assert json.loads(new) == {"name": "p", "version": "0.2.0", "author": {"version": "keep"}}


@pytest.mark.parametrize(
    "text",
    [
        '{\n  "name": "p"\n}\n',
        '{\n  "version": "0.1.0",\n  "version": "0.1.0"\n}\n',
        '{"version": "0.1.0"}\n',
    ],
)
def test_needs_exactly_one_top_level_version_line(text: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        release_bump.bump(text, "1.0.0")


def test_fails_when_the_result_is_not_json() -> None:
    with pytest.raises(ValueError):
        release_bump.bump('{\n  "version": "0.1.0",\n}\n', "1.0.0")


def test_the_command_writes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "plugin.json"
    path.write_text('{\n  "name": "p",\n  "version": "0.1.0"\n}\n', encoding="utf-8")
    result = run("0.2.0", str(path))
    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == '{\n  "name": "p",\n  "version": "0.2.0"\n}\n'


def test_the_command_fails_without_change_or_with_a_bad_version(tmp_path: Path) -> None:
    path = tmp_path / "plugin.json"
    original = '{\n  "version": "0.2.0"\n}\n'
    path.write_text(original, encoding="utf-8")
    assert run("0.2.0", str(path)).returncode == 1
    assert run("0.2", str(path)).returncode == 1
    assert run().returncode == 2
    assert path.read_text(encoding="utf-8") == original
