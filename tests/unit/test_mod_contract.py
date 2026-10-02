"""The display module never decides: the static half of its contract.

``hooks/register.js`` runs inside Claude Code, where a module that fails is skipped and the
call proceeds. That is safe only while the module has no say in any verdict, so this pins
what it may hook, what it may call, and what it may answer, by reading its source -- no
``claude`` binary needed. ``make test-mod`` is the behavioural half. See
``docs/design/mod.md``.
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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "hooks" / "register.js"


def source() -> str:
    """The module without comments, so a forbidden name in prose does not count -- or hide."""
    text = MODULE.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def test_hooks_json_loads_exactly_this_module_beside_the_command_hooks() -> None:
    document = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert document["modules"] == ["./register.js"]
    # The gate itself stays on command hooks: a module cannot be the gate (docs/design/mod.md).
    assert set(document["hooks"]) == {"PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "UserPromptSubmit", "SessionStart"}


def test_the_module_hooks_exactly_the_display_events() -> None:
    hooked = re.findall(r"\bon\(\s*'([^']+)'", source())
    assert sorted(hooked) == sorted(["session.start", "turn.start", "turn.complete", "tool.call", "ui.render"])
    assert "on('ui.render', { component: 'AbovePrompt' }" in source()
    assert "on('tool.call', { tool: 'Bash' }" in source()


@pytest.mark.parametrize(
    "forbidden",
    [
        r"tool\.check",
        r"classic\.",
        r"command\.",
        r"prompt\.",
        r"\.catch\s*\(",
        r"\$\.fs\b",
        r"\$\.store\b",
        r"\$\.state\b",
        r"\$\.env\b",
        r"\$\.http\b",
        r"\$\.model\b",
        r"\$\.agent\b",
        r"\$\.tool\b",
        r"\$\.ui\.toast\b",
        r"\$\.process\.spawn\b",
        r"\bimport\b",
    ],
)
def test_the_module_never_reaches_for_anything_that_could_decide_or_write(forbidden: str) -> None:
    assert not re.search(forbidden, source()), forbidden


@pytest.mark.parametrize("key", ["deny", "decision", "result", "block", "permissionDecision"])
def test_the_module_answers_no_event_itself(key: str) -> None:
    """Every hook passes ``next(e)`` the event unchanged and returns its result; none answers one."""
    assert not re.search(rf"\b{key}\s*:", source()), key


def test_every_handled_event_is_passed_on_unchanged() -> None:
    body = source()
    assert "next({" not in body, "an event rewritten on its way down"
    # session.start and turn.start return next(e); turn.complete and tool.call return its result.
    assert body.count("return next(e)") == 2
    assert body.count("const result = await next(e)") == 2
    assert body.count("return result") == 2
    # ui.render draws what lies below it, with or without its own line above.
    assert body.count("const below = await next(e)") == 1
    assert body.count("return below") == 1
    assert body.count("next(e)") == 5


def test_its_only_process_is_the_shim_asking_for_this_sessions_status() -> None:
    body = source()
    assert body.count("$.process.run(") == 1
    assert "$.process.run([$.plugin.root + '/scripts/arl.sh', 'status', '--json', '--session', session]" in body
