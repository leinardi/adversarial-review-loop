"""``config``: show the configuration a run here would use, or set or unset one key.

The arguments arrive as the text the person typed after ``/arl-config``, on stdin, and are split
here the way a shell would (``shlex``). With none, every key is listed with its value and the layer
that set it. ``<key> <value...>`` writes the user config (``--repo``: the launch directory's
``.adversarial-review-loop.json``); ``<key> --unset`` removes it from that layer. A value that
does not parse for its key is refused, never coerced: a typo must not quietly become a setting.
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
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from arl import atomic, config, harness
from arl.state import RunError, RunPaths

__all__ = ["USAGE", "cmd_config", "effort_level"]

USAGE: Final = (
    "usage: /arl-config                         every key, its value and the layer that set it\n"
    "       /arl-config <key> <value> [--repo]  set a key in the user config (--repo: this directory's)\n"
    "       /arl-config <key> --unset [--repo]  remove a key from that layer\n"
    "Put -- before a value that holds its own --flags: /arl-config verify_cmd -- pytest --maxfail=1"
)

_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value) if value else "(empty)"
    return str(value) if value != "" else '""'


def _coerce(key: str, raw: str) -> Any:
    if key in config.BOOL_KEYS:
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise RunError("config", f"{raw!r} is not a boolean for {key}: use true or false")
    if key in config.INT_KEYS:
        if not (raw.isascii() and raw.isdigit()):
            raise RunError("config", f"{raw!r} is not a non-negative integer for {key}")
        return int(raw)
    if key in config.LIST_KEYS:
        return [part for part in raw.split(",") if part]
    if key in config.SEVERITY_KEYS:
        if raw.lower() not in config.SEVERITY_LABELS:
            raise RunError("config", f"{raw!r} is not a severity: use one of info, low, medium, high, critical")
        return raw.lower()
    if key == "harness" and raw not in harness.names():
        raise RunError("config", f"{raw!r} is not a harness this build has: use one of {', '.join(harness.names())}")
    if key == "effort" and raw not in ("", *config.EFFORT_LEVELS):
        raise RunError("config", f"{raw!r} is not an effort level: use one of {', '.join(config.EFFORT_LEVELS)}")
    return raw


def _target(launch_dir: Path, *, repo: bool) -> Path:
    return launch_dir / config.REPO_CONFIG_NAME if repo else config.user_config_path()


def _read(path: Path) -> dict[str, Any]:
    """The JSON object at ``path``, ``{}`` when there is none; one that does not parse is refused,
    not overwritten: it may hold settings a one-key document would lose."""
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunError("config", f"{path} cannot be read as JSON ({exc}); fix or remove it first") from exc
    if not isinstance(document, dict):
        raise RunError("config", f"{path} does not hold a JSON object; fix or remove it first")
    return document


def _write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_atomic(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def _layers(launch_dir: Path) -> dict[str, str]:
    """The layer that last set each key, in ``config.load``'s order; a file that does not parse
    sets nothing, as ``load`` drops the whole file layer then."""
    source = dict.fromkeys(config.DEFAULTS, "default")
    try:
        files = [(name, _read(path)) for name, path in (("user", config.user_config_path()), ("repo", launch_dir / config.REPO_CONFIG_NAME))]
    except RunError:
        files = []
    for name, document in files:
        for key in document:
            if key in source:
                source[key] = name
    for key in config.from_env():
        source[key] = "env"
    return source


def _show(launch_dir: Path) -> str:
    resolved = config.load(str(launch_dir))
    source = _layers(launch_dir)
    width = max(len(key) for key in config.DEFAULTS)
    lines = [f"adversarial-review-loop configuration for {launch_dir}"]
    for key in sorted(config.DEFAULTS):
        value = harness.display_model(resolved) if key == "model" else resolved.values.get(key)
        lines.append(f"  {key.ljust(width)}  {_display(value)}  ({source[key]})")
    lines.append(f"User config: {config.user_config_path()}; this directory's: {launch_dir / config.REPO_CONFIG_NAME}")
    return "\n".join(lines)


def _parse(tokens: list[str]) -> tuple[str, list[str], bool, bool]:
    """``(key, value tokens, repo, unset)``; flags come before a bare ``--``, which ends them."""
    key = ""
    values: list[str] = []
    repo = unset = ended = False
    for token in tokens:
        if ended:
            values.append(token)
        elif token == "--":
            ended = True
        elif token == "--repo":
            repo = True
        elif token == "--unset":
            unset = True
        elif token.startswith("--"):
            raise RunError("config", f"unknown flag {token}\n{USAGE}")
        elif not key:
            key = token
        else:
            values.append(token)
    if key not in config.DEFAULTS:
        raise RunError("config", f"unknown key {key!r}; the keys are {', '.join(sorted(config.DEFAULTS))}")
    if unset and values:
        raise RunError("config", f"--unset takes no value\n{USAGE}")
    if not unset and not values:
        raise RunError("config", f"{key} needs a value (or --unset)\n{USAGE}")
    return key, values, repo, unset


def effort_level(effort: str) -> str:
    """The effort the reviewer is registered with: ``effort`` when it is a level, else empty."""
    return effort if effort in config.EFFORT_LEVELS else ""


def _effort(launch_dir: Path) -> str:
    return effort_level(config.load(str(launch_dir)).as_str("effort"))


def cmd_config(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    raw = args.get("args")
    if not isinstance(raw, str):
        raise RunError("config", "config reads its arguments as text on stdin")
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise RunError("config", f"cannot split the arguments: {exc}") from exc
    launch_dir = paths.launch_dir
    if not tokens:
        return {"ok": True, "text": _show(launch_dir), "effort": _effort(launch_dir)}
    key, values, repo, unset = _parse(tokens)
    target = _target(launch_dir, repo=repo)
    document = _read(target)
    before = config.load(str(launch_dir)).values.get(key)
    if unset:
        if key not in document:
            return {
                "ok": True,
                "text": f"{key} is not set in {target}; nothing changed.",
                "key": key,
                "effort": _effort(launch_dir),
            }
        document.pop(key)
        _write(target, document)
        written = "(unset)"
    else:
        # Several words are put back as the shell would read them: `pytest -k "a or b"` keeps its quotes.
        value = _coerce(key, values[0] if len(values) == 1 else shlex.join(values))
        document[key] = value
        _write(target, document)
        written = _display(value)
    after = config.load(str(launch_dir)).values.get(key)
    text = f"{key}: {_display(before)} -> {_display(after)} (wrote {written} to {target})"
    winner = _layers(launch_dir)[key]
    layer = "repo" if repo else "user"
    if not unset and winner != layer:
        text += f"\nNote: the {winner} layer overrides it, so {_display(after)} is what a run uses."
    return {"ok": True, "text": text, "key": key, "effort": _effort(launch_dir)}
