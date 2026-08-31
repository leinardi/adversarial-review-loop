#!/usr/bin/env python3
"""Propagate the pins in ``requirements-dev.txt`` into ``.pre-commit-config.yaml``.

``requirements-dev.txt`` is the single source of truth for a dev dependency's version. A few
of those versions are necessarily duplicated in the ``additional_dependencies`` of the hooks
in ``.pre-commit-config.yaml`` -- pytest is there so mypy can see its ``py.typed`` marker --
and nothing keeps the copies in step: Dependabot's ``pip`` ecosystem rewrites the requirements
file only, and its ``pre-commit`` ecosystem bumps ``rev:`` only.

This is the fix-up half of that story; ``tests/unit/test_pins.py`` is the check half, and both
read the files through the functions below so they cannot disagree about what a pin is. Run it
after a Dependabot bump::

    make sync-pins

Only ``name==version`` entries are touched. npm pins (``prettier@3.7.4``) have no counterpart
in the requirements file, so there is nothing to sync them against, and they are left alone.

Development tooling: this module is not part of the gate and is never imported by it.
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

import re
import sys
from pathlib import Path

__all__ = ["hook_pins", "requirement_pins", "sync"]

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PLUGIN_ROOT / "requirements-dev.txt"
PRE_COMMIT = PLUGIN_ROOT / ".pre-commit-config.yaml"

#: A whole line of ``requirements-dev.txt``, once its comment has been stripped.
REQUIREMENT = re.compile(r"^([A-Za-z0-9._-]+)==([A-Za-z0-9._+-]+)$")

#: A YAML list entry pinning a package. In ``.pre-commit-config.yaml`` only
#: ``additional_dependencies`` entries take this shape, which is why this reads the file by
#: line instead of parsing it: pyyaml is not a dev dependency, and adding one in order to
#: police the dev dependencies would be its own kind of drift.
HOOK_PIN = re.compile(r"^\s*-\s*([A-Za-z0-9._-]+)==([A-Za-z0-9._+-]+)\s*(?:#.*)?$")


def requirement_pins(text: str) -> dict[str, str]:
    """``{name: version}`` for every line of a requirements file.

    Raises ``ValueError`` on a line that is not an exact pin -- a range or a bare name would
    make the version this script propagates depend on the day it ran.
    """
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = REQUIREMENT.match(line)
        if match is None:
            raise ValueError(f"not an exact pin: {raw!r}")
        pins[match.group(1)] = match.group(2)
    return pins


def hook_pins(text: str) -> list[tuple[int, str, str]]:
    """``(line number, name, version)`` for every pinned dependency in a pre-commit config.

    A list rather than a mapping: the same package is pinned by more than one hook, and the
    duplicates are exactly what the caller is here for.
    """
    found: list[tuple[int, str, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        match = HOOK_PIN.match(raw)
        if match is not None:
            found.append((number, match.group(1), match.group(2)))
    return found


def sync(requirements_text: str, pre_commit_text: str) -> tuple[str, list[str]]:
    """Return the rewritten pre-commit config, and one description per line changed.

    Rewriting the matched substring of the line, rather than rebuilding the line, keeps the
    indentation and any trailing comment exactly as they were -- prettier and yamllint both
    run over this file, and a reformat here would be indistinguishable from a real change.
    """
    pins = requirement_pins(requirements_text)
    lines = pre_commit_text.splitlines(keepends=True)
    changes: list[str] = []
    for number, name, version in hook_pins(pre_commit_text):
        wanted = pins.get(name)
        if wanted is None or wanted == version:
            continue
        lines[number - 1] = lines[number - 1].replace(f"{name}=={version}", f"{name}=={wanted}", 1)
        changes.append(f"line {number}: {name}=={version} -> {name}=={wanted}")
    return "".join(lines), changes


def main(argv: list[str]) -> int:
    if argv:
        print(f"usage: {Path(__file__).name}  (no arguments)", file=sys.stderr)
        return 2
    try:
        updated, changes = sync(REQUIREMENTS.read_text(), PRE_COMMIT.read_text())
    except ValueError as error:
        print(f"sync-pins: {REQUIREMENTS.name}: {error}", file=sys.stderr)
        return 1
    if not changes:
        print(f"sync-pins: {PRE_COMMIT.name} already matches {REQUIREMENTS.name}")
        return 0
    PRE_COMMIT.write_text(updated)
    for change in changes:
        print(f"sync-pins: {PRE_COMMIT.name} {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
