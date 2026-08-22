"""Configuration resolution.

Precedence, lowest to highest::

    defaults  <  user config  <  repo .opencode-review-loop.json  <  OCRL_* environment

Repository config is attacker-controlled input: it travels with the tree under review. It
may select a model or widen an ignore list, but nothing here can execute code or turn a
failure into an approval -- every value is read as data by a typed accessor.

``-I`` implies ``-E``, which suppresses ``PYTHON*`` variables only; ``OCRL_*`` is unaffected
and still reaches the gate.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

__all__ = ["BOOL_KEYS", "CONFIG_KEYS", "DEFAULTS", "INT_KEYS", "Config", "load", "severity_rank", "user_config_path"]

#: Every supported key, with its default.
DEFAULTS: Final[dict[str, Any]] = {
    "model": "openai/gpt-5.6-sol",
    "variant": "",
    "block_severity": "low",
    "timeout_sec": 900,
    "max_failures": 2,
    "max_stop_blocks": 3,
    "max_defers": 3,
    "verify_cmd": "",
    "pure": True,
    "disable_project_config": False,
    "chunk_diff_bytes": 400000,
    "hard_diff_ceiling": 8388608,
    "max_file_bytes": 16777216,
    "max_reason_bytes": 32768,
    "max_findings": 200,
    "max_findings_bytes": 65536,
    "allow_dirty": False,
    "ttl_hours": 24,
    "ignore_globs": [],
}

BOOL_KEYS: Final = ("pure", "disable_project_config", "allow_dirty")

INT_KEYS: Final = (
    "timeout_sec",
    "max_failures",
    "max_stop_blocks",
    "max_defers",
    "chunk_diff_bytes",
    "hard_diff_ceiling",
    "max_file_bytes",
    "max_reason_bytes",
    "max_findings",
    "max_findings_bytes",
    "ttl_hours",
)

LIST_KEYS: Final = ("ignore_globs",)

#: The keys the environment may override, in the shell's order.
CONFIG_KEYS: Final = tuple(DEFAULTS)

#: Values the shell accepted as true. Deliberately exact, including the case variants:
#: anything else is false, so a typo never silently enables something.
_TRUE_VALUES: Final = frozenset({"1", "true", "TRUE", "yes", "on"})

REPO_CONFIG_NAME: Final = ".opencode-review-loop.json"


def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME")
    base = xdg if xdg else f"{env.get('HOME', '')}/.config"
    return Path(base) / "opencode-review-loop" / "config.json"


def from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Overrides built only from ``OCRL_*`` variables that are **set**, empty included."""
    env = os.environ if environ is None else environ
    out: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        name = f"OCRL_{key.upper()}"
        if name not in env:
            continue
        raw = env[name]
        if key in BOOL_KEYS:
            out[key] = raw in _TRUE_VALUES
        elif key in INT_KEYS:
            # A non-numeric value is skipped entirely rather than coerced, so a typo
            # leaves the previous layer's value standing instead of becoming zero.
            if raw.isdigit():
                out[key] = int(raw)
        elif key in LIST_KEYS:
            # Comma-separated in the environment, a list everywhere else.
            out[key] = [part for part in raw.split(",") if part]
        else:
            out[key] = raw
    return out


class Config:
    """A merged configuration, read through typed accessors.

    The shell read every value through one ``jq`` filter whose ``// ""`` blanked any false
    boolean -- ``ocrl_cfg pure`` returned an empty string rather than ``"false"``. Every
    call site happened to compare against ``"true"``, so the bug was inert, but it is not
    reproduced here: the accessors below answer with the value's real type.
    """

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values: dict[str, Any] = dict(values)

    def __contains__(self, key: str) -> bool:
        return key in self.values

    def as_str(self, key: str) -> str:
        """Scalar value as text, with a list joined by commas (the shell's rendering)."""
        value = self.values.get(key)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    def as_int(self, key: str) -> int:
        """Integer value, falling back to the default when the config holds nonsense."""
        value = self.values.get(key)
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            fallback = DEFAULTS.get(key, 0)
            return int(fallback) if isinstance(fallback, int) else 0

    def as_bool(self, key: str) -> bool:
        value = self.values.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value in _TRUE_VALUES
        return bool(DEFAULTS.get(key, False))

    def as_list(self, key: str) -> list[str]:
        value = self.values.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str) and value:
            return [part for part in value.split(",") if part]
        return []


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load(
    repo: str,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Merge every layer into one configuration.

    Precedence, lowest to highest: defaults < user config < repo config < ``overrides`` <
    environment. ``overrides`` is a per-activation overlay (``state.json``'s ``overrides``
    field, e.g. a ``--model`` given to ``implement`` or ``resume``) -- it beats the config
    files but not ``OCRL_*``, so the environment still has the final word. Only keys already
    in :data:`DEFAULTS` are accepted from it; anything else is dropped, because the overlay
    is written into state and state is not a trust boundary the config layer should widen.

    A config file that cannot be read or parsed discards the *whole* file layer rather than
    applying half of it: the shell slurped all files through a single ``jq``, so one bad
    file left the defaults standing. That is preserved deliberately -- a partially applied
    security-relevant config is worse than an ignored one. A file that parses but is not a
    JSON object is skipped on its own, which is also what ``jq`` did.
    """
    env_overrides = from_env(environ)

    files: list[Path] = []
    user = user_config_path(environ)
    if user.is_file():
        files.append(user)
    if repo:
        repo_config = Path(repo) / REPO_CONFIG_NAME
        if repo_config.is_file():
            files.append(repo_config)

    merged: dict[str, Any] = dict(DEFAULTS)
    try:
        documents = [_read_json(path) for path in files]
    except (OSError, ValueError, RecursionError):
        merged = dict(DEFAULTS)
    else:
        for document in documents:
            if isinstance(document, dict):
                merged.update(document)

    if overrides:
        merged.update({key: value for key, value in overrides.items() if key in DEFAULTS})

    merged.update(env_overrides)
    return Config(merged)


def severity_rank(label: str) -> int:
    """Rank a reviewer-supplied severity label.

    An unrecognised label ranks as the most severe. A severity the gate cannot parse must
    never be a way to slip past it (Rule 1).
    """
    match label.lower():
        case "info" | "trivial" | "nit":
            return 1
        case "low" | "minor":
            return 2
        case "medium" | "moderate" | "major":
            return 3
        case "high" | "serious":
            return 4
        case _:
            return 5
