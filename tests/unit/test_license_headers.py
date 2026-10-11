"""``scripts/check-license-headers.sh``: what it governs, what it exempts, and that it can fail."""

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

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import PLUGIN_ROOT, git

SCRIPT = PLUGIN_ROOT / "scripts" / "check-license-headers.sh"

#: The notice body, exactly as the checker's own header carries it (its lines 3-18).
_NOTICE = [line.removeprefix("#").removeprefix(" ") for line in SCRIPT.read_text().splitlines()[2:18]]


def _header(prefix: str, year: str = "2026") -> str:
    lines = [line.replace("Copyright (c) 2026 ", f"Copyright (c) {year} ") for line in _NOTICE]
    return "".join(f"{prefix} {line}".rstrip() + "\n" for line in lines)


def _env() -> dict[str, str]:
    # A git hook exports GIT_DIR, GIT_INDEX_FILE and friends; the scratch repository must not
    # inherit them, or `git add` would write into the real index.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _run(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(cwd / "scripts" / "check-license-headers.sh")], cwd=cwd, env=_env(), capture_output=True, text=True, check=False)


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository whose governed files all carry the header, so the check passes on it."""
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".mk").mkdir()
    git(repo, "init", "-q")
    shutil.copy2(SCRIPT, repo / "scripts" / "check-license-headers.sh")
    (repo / "Makefile").write_text(_header("#") + "\nMK_COMMON_FILES := help.mk\n")
    (repo / ".mk" / "local.mk").write_text(_header("#", "2026-2027") + "\nall:\n")
    (repo / ".mk" / "help.mk").write_text("# make-common's own file\n")
    (repo / "scripts" / "bootstrap-mk-common.sh").write_text("#!/usr/bin/env bash\n")
    docstring = '"""A module docstring\n\n' + "that runs long.\n" * 40 + '"""\n\n'
    (repo / "mod.py").write_text(docstring + _header("# ") + "\nimport os\n")
    (repo / "tool.py").write_text("#!/usr/bin/env python3\n" + '"""One line."""\n\n' + _header("# "))
    (repo / "run.sh").write_text("#!/usr/bin/env bash\n#\n" + _header("#"))
    (repo / "mod.js").write_text(_header("//"))
    (repo / "config.yaml").write_text("key: value\n")
    (repo / "README.md").write_text("# Title\n")
    vendor = repo / "scripts" / "arl" / "_vendor"
    vendor.mkdir(parents=True)
    (vendor / "upstream.py").write_text('"""Upstream code."""\n')
    git(repo, "add", "-A")
    return repo


def test_the_repository_itself_passes() -> None:
    proc = _run(PLUGIN_ROOT)
    assert proc.returncode == 0, proc.stderr
    assert "all carry the GPL-3.0-or-later header" in proc.stdout


def test_a_tree_with_every_header_passes(scratch: Path) -> None:
    proc = _run(scratch)
    assert proc.returncode == 0, proc.stderr
    # Makefile, .mk/local.mk, the checker, mod.py, tool.py, run.sh and mod.js; nothing exempt.
    assert "7 files checked" in proc.stdout


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("missing.py", '"""No header at all."""\n\nimport os\n'),
        ("missing.sh", "#!/usr/bin/env bash\necho hi\n"),
        ("missing.ts", "export const x = 1;\n"),
        ("missing.cjs", "module.exports = {};\n"),
        (".mk/missing.mk", "all:\n"),
        # GPL-3.0-only: the "or (at your option) any later version" grant is gone.
        ("only.py", _header("# ").replace("(at your option) any later version.", "")),
        # Another project's notice with this project's copyright line.
        ("other.sh", _header("#").replace("adversarial-review-loop", "other-project")),
        ("holder.js", _header("//").replace("Roberto Leinardi", "Someone Else")),
        # Below the window: a header the checker would have to search the whole file for.
        ("late.sh", "#!/usr/bin/env bash\n" + "echo\n" * 30 + _header("#")),
    ],
)
def test_a_missing_or_wrong_header_fails_and_names_the_file(scratch: Path, name: str, content: str) -> None:
    (scratch / name).write_text(content)
    git(scratch, "add", "--", name)
    proc = _run(scratch)
    assert proc.returncode == 1
    assert f"  {name}\n" in proc.stderr


@pytest.mark.parametrize(
    "name",
    [
        "scripts/arl/_vendor/bashlex/parser.py",
        ".claude-plugin/types/claude-code/index.d.ts",
        ".mk/help.mk",
        "scripts/bootstrap-mk-common.sh",
        "notes.md",
        "data.json",
        "workflow.yaml",
    ],
)
def test_exempt_and_ungoverned_files_need_no_header(scratch: Path, name: str) -> None:
    path = scratch / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("no header\n")
    git(scratch, "add", "-f", "--", name)
    proc = _run(scratch)
    assert proc.returncode == 0, proc.stderr


def test_a_shared_fragment_is_exempt_only_while_the_makefile_names_it(scratch: Path) -> None:
    makefile = scratch / "Makefile"
    makefile.write_text(makefile.read_text().replace("MK_COMMON_FILES := help.mk", "MK_COMMON_FILES := go.mk"))
    proc = _run(scratch)
    assert proc.returncode == 1
    assert "  .mk/help.mk\n" in proc.stderr


def test_an_untracked_file_is_not_checked(scratch: Path) -> None:
    (scratch / "untracked.py").write_text("import os\n")
    proc = _run(scratch)
    assert proc.returncode == 0, proc.stderr


def test_an_argument_is_refused() -> None:
    proc = subprocess.run([str(SCRIPT), "--all"], cwd=PLUGIN_ROOT, env=_env(), capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "Usage:" in proc.stderr
