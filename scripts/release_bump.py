#!/usr/bin/env python3
"""Write a release version into .claude-plugin/plugin.json (make release-bump).

Only the value of the top-level "version" key changes: the rest of the file keeps its bytes, so the
bump commit is a one-line diff and the hand-kept formatting (the one-line "keywords" array) stays.
Development tooling: not part of the gate and never imported by it. Called by the Release workflow (gh-reusable-workflows' version-bump-pr) as
`make release-bump VERSION="$RELEASE_VERSION"`; it never commits.
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

import json
import re
import sys
from pathlib import Path

# A strict X.Y.Z release version: no leading zeros, no pre-release, no build metadata.
VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
# The version line as plugin.json writes it: two spaces of indentation marks the top level.
LINE_RE = re.compile(r'^(  "version": ")([^"\n]*)(",?)$', re.MULTILINE)
DEFAULT_PATH = Path(".claude-plugin/plugin.json")


def bump(text: str, version: str) -> str:
    """Return text with the top-level version set to version, or raise ValueError."""
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"version must be X.Y.Z without leading zeros, got {version!r}")
    matches = LINE_RE.findall(text)
    if len(matches) != 1:
        raise ValueError(f'expected exactly one top-level "version" line, found {len(matches)}')
    new = LINE_RE.sub(lambda m: m.group(1) + version + m.group(3), text)
    data = json.loads(new)
    if not isinstance(data, dict) or data.get("version") != version:
        raise ValueError("the rewritten file does not carry the new version at the top level")
    return new


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: release_bump.py VERSION [PLUGIN_JSON]", file=sys.stderr)
        return 2
    path = Path(argv[2]) if len(argv) == 3 else DEFAULT_PATH
    try:
        text = path.read_text(encoding="utf-8")
        new = bump(text, argv[1])
    except (OSError, ValueError) as error:
        print(f"release_bump.py: {path}: {error}", file=sys.stderr)
        return 1
    if new == text:
        print(f"release_bump.py: {path} already carries {argv[1]}", file=sys.stderr)
        return 1
    path.write_text(new, encoding="utf-8")
    print(f"{path}: version {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
