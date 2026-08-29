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

__all__ = [
    "BOOL_KEYS",
    "CONFIG_KEYS",
    "DEFAULTS",
    "INT_KEYS",
    "LIST_KEYS",
    "REPO_CONFIG_NAME",
    "SEVERITY_KEYS",
    "SEVERITY_LABELS",
    "Config",
    "from_env",
    "late_threshold_rank",
    "load",
    "severity_rank",
    "threshold_rank",
    "user_config_path",
]

#: Every supported key, with its default.
#:
#: ``model`` defaults to the empty string rather than to a name, because a provider-qualified
#: id that is meaningful to one reviewer CLI is meaningless to another: the real default is
#: per-harness (``ocrl.harness.Harness.default_model``) and is resolved through
#: ``ocrl.harness.model``, the one reader every command shares. An empty ``model`` therefore
#: means "whatever this harness calls its default", never "no model".
#:
#: ``harness`` defaults to ``claude-code``: the gate runs as a Claude Code plugin, so that is
#: the one reviewer CLI every user of it already has installed. ``opencode`` -- which the
#: project is named after and which is still fully supported -- is one config key away, and
#: the name never falls back: see ``ocrl.harness.UnknownHarness``.
DEFAULTS: Final[dict[str, Any]] = {
    "harness": "claude-code",
    "model": "",
    "variant": "",
    "block_severity": "medium",
    "late_block_severity": "high",
    "timeout_sec": 900,
    "max_failures": 2,
    "max_transient_failures": 5,
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
    "max_clarifications": 2,
    "stall_rounds": 3,
    "max_session_rounds": 3,
    "allow_dirty": False,
    "ttl_hours": 24,
    "ignore_globs": [],
    "final_review": False,
    "cold_confirm": False,
}

BOOL_KEYS: Final = ("pure", "disable_project_config", "allow_dirty", "final_review", "cold_confirm")

INT_KEYS: Final = (
    "timeout_sec",
    "max_failures",
    "max_transient_failures",
    "max_stop_blocks",
    "max_defers",
    "chunk_diff_bytes",
    "hard_diff_ceiling",
    "max_file_bytes",
    "max_reason_bytes",
    "max_findings",
    "max_findings_bytes",
    "max_clarifications",
    "stall_rounds",
    "max_session_rounds",
    "ttl_hours",
)

LIST_KEYS: Final = ("ignore_globs",)

#: The keys whose value is a severity label, validated and ranked through ``threshold_rank``.
SEVERITY_KEYS: Final = ("block_severity", "late_block_severity")

#: The keys the environment may override, in the shell's order.
CONFIG_KEYS: Final = tuple(DEFAULTS)

#: Values the shell accepted as true. Deliberately exact, including the case variants:
#: anything else is false, so a typo never silently enables something.
_TRUE_VALUES: Final = frozenset({"1", "true", "TRUE", "yes", "on"})

REPO_CONFIG_NAME: Final = ".opencode-review-loop.json"

#: Every label ``severity_rank``/``threshold_rank`` recognise, with its rank. The single
#: source both functions read from, so a label added to one can never drift from the other.
#: ``critical`` is a real, fifth tier -- the reviewer contract's own ``FINDING`` regex
#: (``reviewer.py``, ``severity=(?P<severity>info|low|medium|high|critical)``) accepts
#: exactly these five words and no others, so this dict has to agree with that regex, not
#: invent its own vocabulary.
_SEVERITY_RANK: Final[dict[str, int]] = {
    "info": 1,
    "trivial": 1,
    "nit": 1,
    "low": 2,
    "minor": 2,
    "medium": 3,
    "moderate": 3,
    "major": 3,
    "high": 4,
    "serious": 4,
    "critical": 5,
}

#: The labels a `block_severity` setting may be.
SEVERITY_LABELS: Final[frozenset[str]] = frozenset(_SEVERITY_RANK)


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

    An unrecognised label ranks as the most severe (5) -- the highest rank a real label ever
    gets is 4, so this guarantees an unparsable label clears any configured threshold. A
    severity the gate cannot parse must never be a way to slip past it (Rule 1).

    Not the right function for ranking ``block_severity`` itself -- see ``threshold_rank``.
    """
    return _SEVERITY_RANK.get(label.lower(), 5)


def threshold_rank(label: str) -> int:
    """Rank a configured ``block_severity`` threshold.

    ``severity_rank``'s "unrecognised ranks highest" rule is fail-closed for a *value being
    compared* -- an unparsable finding severity must clear any threshold. Applied to the
    *threshold itself* it is fail-open: ranking an unknown ``block_severity`` at 5 (above
    every real label) would make almost nothing meet it, so a typo'd or unrecognised
    threshold would silently block far less than the default, not more. Rule 1 requires the
    opposite direction here, so an unrecognised threshold ranks at 1 -- the floor a finding's
    rank clears most easily -- making the gate stricter on bad input, never looser.
    """
    return _SEVERITY_RANK.get(label.lower(), 1)


def late_threshold_rank(config: Config) -> int:
    """Rank the ``late_block_severity`` threshold, clamped up to ``block_severity``'s rank.

    From the second review round of a phase on, a finding that is new *and* outside the paths
    changed since the previous round blocks only when it reaches this rank
    (``reviewer.parse``). It can only ever **defer** a finding that ``block_severity`` would
    have blocked, never widen the blocking set: a ``late_block_severity`` set below
    ``block_severity`` is meaningless in that direction, so it is read as ``block_severity``
    itself. Both labels rank through :func:`threshold_rank`, so an unrecognised value ranks at
    the floor -- and the clamp then lifts it to ``block_severity``, restoring the ordinary rule
    rather than inventing a laxer one.
    """
    return max(threshold_rank(config.as_str("late_block_severity")), threshold_rank(config.as_str("block_severity")))
