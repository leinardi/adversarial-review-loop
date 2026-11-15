"""The OpenCode harness: how the engine spells a review for ``opencode run``.

The reviewer reads the bundle by path: the composed prompt names every file in it, and the
permission document, passed in ``OPENCODE_CONFIG_CONTENT``, allows reads of the bundle and the
roots and denies everything else. Every round is a fresh run with its own ``--title``, started in
the repository (``cwd``); nothing is continued.

Written against OpenCode 2.2.7, the first release this harness supports. Since 2.x, ``opencode
run`` hands the message to a shared background service by default, which reads its own
environment and config, not the run's: **without** ``--standalone`` the permission document is
never seen and the reviewer gets the user's permissions (measured: it wrote a file and ran a
shell command). 2.x also dropped ``--pure``, ``--dir``, ``--variant`` and
``OPENCODE_PERMISSION``.

The run writes JSON events (``--format json``): measured on 2.3.0 (tests/STEP0.md, L4), a refused
model or credential exits 1 like a network failure, and only the ``error`` event's ``type`` tells
them apart. :meth:`OpenCodeHarness.transcript` joins the ``text`` events into the answer.
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
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from arl import reviewer_probe
from arl.config import Config
from arl.harness import Command, PayloadError, ReviewSpec, TranscriptError, Usage, model

__all__ = [
    "AGENT",
    "HARNESS",
    "MODEL_REFUSALS",
    "TRANSIENT_ERRORS",
    "OpenCodeHarness",
    "config_content",
    "events",
    "isolation_argv",
    "isolation_env",
    "permission",
    "review_argv",
]


def isolation_argv(config: Config) -> list[str]:
    """``--standalone``: a private server, so the run's ``OPENCODE_CONFIG_CONTENT`` applies.

    The shared background service would ignore it; see the module docstring.
    """
    del config
    return ["--standalone"]


def isolation_env(config: Config, base: dict[str, str]) -> dict[str, str]:
    """``base``, plus the isolation env vars, iff configured. Never mutates ``base``."""
    env = dict(base)
    if config.as_bool("disable_project_config"):
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    return env


def review_argv(title: str, *, config: Config) -> list[str]:
    """The flags that follow the prompt, which goes immediately after ``run``.

    The repository is the run's working directory (``Command.cwd``): 2.x has no ``--dir``. A
    ``variant`` is spelled into the model, ``provider/model#variant``.
    """
    argv: list[str] = [*isolation_argv(config)]
    # `HARNESS`, not the configured harness: an OpenCode argv carries OpenCode's tier models
    # even when this is called directly.
    chosen = model(config, HARNESS)
    variant = config.as_str("variant")
    if variant and "#" in chosen:
        raise PayloadError(f"model {chosen!r} already names a variant; unset variant ({variant!r}) or drop it from model")
    argv += ["-m", f"{chosen}#{variant}" if variant else chosen]
    # `--archived`: a review is not a session the person goes back to, so it stays out of their list.
    # `--format json`: the only output that says why a run failed (see the module docstring).
    argv += ["--agent", AGENT, "--title", title, "--archived", "--format", "json"]
    return argv


def permission(bundle_dir: Path, roots: Sequence[str] = ()) -> str:
    """The permission document for a read-only reviewer of one bundle.

    Reading, grepping, globbing and listing are allowed; everything else (edits, shell) is
    denied. Outside the launch directory only ``bundle_dir`` and the run's ``roots`` may be read.
    Patterns are last-match-wins, which is why the broad deny is written first.
    """
    external = {"*": "deny"}
    for directory in (*roots, str(bundle_dir)):
        external[f"{directory.rstrip('/')}/**"] = "allow"
    document = {
        "*": "deny",
        "read": "allow",
        "grep": "allow",
        "glob": "allow",
        "list": "allow",
        "external_directory": external,
    }
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False)


#: The agent every review runs as, named with ``--agent`` so a project's ``default_agent`` cannot
#: pick another one.
AGENT: Final = "build"


def config_content(bundle_dir: Path, roots: Sequence[str] = ()) -> str:
    """``OPENCODE_CONFIG_CONTENT``: :func:`permission`'s document, top level and on :data:`AGENT`.

    The inline config is resolved after the user's and the project's, so its top-level rules come
    after theirs. **That alone is not enough**: 2.2.7 appends an agent's own permission rules after
    the top-level ones, last match wins, so a project's ``agent.build.permission`` allowing edits
    would widen the reviewer again. The same document on the agent comes after those too (measured
    with ``opencode debug agents --standalone``).
    """
    document = json.loads(permission(bundle_dir, roots))
    content = {"permission": document, "agent": {AGENT: {"permission": document}}}
    return json.dumps(content, separators=(",", ":"), ensure_ascii=False)


#: ``error.type`` values that refuse the model itself: an unknown model or a provider with no
#: credentials (``provider.no-route``), a key the provider refused (``provider.auth``).
MODEL_REFUSALS: Final = frozenset({"provider.no-route", "provider.auth"})

#: ``error.type`` values worth another try: the provider could not be reached.
TRANSIENT_ERRORS: Final = frozenset({"provider.transport"})


def events(raw: bytes, *, strict: bool = False) -> list[dict[str, object]]:
    """The JSON events of a run's output, one per line; a line that does not open with ``{`` (a log
    on stderr) is skipped.

    ``strict``: a line that opens with ``{`` or ``[`` and is not a whole JSON object raises
    :class:`TranscriptError`. A damaged event may be the error that ends the run, so an answer is
    never read past one. Without it (reading why a run failed, from a tail that may start mid-line)
    such a line is skipped.
    """
    found: list[dict[str, object]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{") and not (strict and stripped.startswith("[")):
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            event = None
        if isinstance(event, dict):
            found.append(event)
        elif strict:
            raise TranscriptError(f"opencode wrote an event that is not a JSON object: {stripped[:200]!r}")
    return found


def _error_of(event: dict[str, object]) -> tuple[str, str]:
    error = event.get("error")
    if not isinstance(error, dict):
        return "", ""
    kind, message = error.get("type"), error.get("message")
    return (kind if isinstance(kind, str) else "", message if isinstance(message, str) else "")


def _with_system_prompt(prompt_text: str, system_prompt: str) -> str:
    """The prompt with the plugin's working guidance appended, or unchanged when there is none.

    **This CLI has no system-prompt flag** (verified against ``opencode run --help``: ``--agent``
    selects a persona, and nothing appends to the system prompt), so the guidance goes where the
    only other instructions go. It is appended rather than prepended so the review instructions
    still open the message, and it is the plugin's own text either way -- the same standing
    as the prompt it is joined to, and nothing repo- or model-derived is involved.
    """
    if not system_prompt:
        return prompt_text
    return f"{prompt_text}\n\n{system_prompt}"


class OpenCodeHarness:
    """``opencode run`` as the reviewer. See the module docstring."""

    name: Final = "opencode"
    binary: Final = "opencode"

    def review_command(self, spec: ReviewSpec) -> Command:
        """``opencode run <prompt> …`` plus the permission document as an env override."""
        return Command(
            argv=[
                self.binary,
                "run",
                _with_system_prompt(spec.prompt_text, spec.system_prompt),
                *review_argv(spec.title, config=spec.config),
            ],
            env=isolation_env(spec.config, {"OPENCODE_CONFIG_CONTENT": config_content(spec.bundle_dir, spec.roots)}),
            cwd=spec.repo,
        )

    def probe_models(self, timeout: float) -> list[str] | None:
        """``opencode models``. Raises ``reviewer_probe.ProbeFailed`` if it does not answer."""
        return reviewer_probe.list_models(timeout)

    def transcript(self, raw: bytes) -> bytes:
        """The ``text`` events' text, joined: the answer :func:`arl.contract.parse` reads.

        An ``error`` event in a run that exited 0 is refused (:class:`TranscriptError`): whatever
        text came before it is not a whole review; :func:`arl.runner.invoke` then reads it through
        :meth:`refusal`. So is an event that does not parse (``events(strict=True)``). No text at all
        is an empty answer, which the engine counts as a reviewer that said nothing.
        """
        texts: list[str] = []
        for event in events(raw, strict=True):
            if event.get("type") == "error":
                kind, message = _error_of(event)
                raise TranscriptError(f"opencode reported an error ({kind or 'unknown'}): {message or 'no message'}")
            part = event.get("part")
            if event.get("type") == "text" and isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(str(part["text"]))
        return "\n".join(texts).encode("utf-8") + (b"\n" if texts else b"")

    def refusal(self, raw: bytes) -> tuple[str, str] | None:
        """The last ``error`` event's verdict on the run: see :data:`MODEL_REFUSALS`."""
        for event in reversed(events(raw)):
            if event.get("type") != "error":
                continue
            kind, message = _error_of(event)
            reason = f"{message or 'no message'} ({kind or 'no type'})"
            if kind in MODEL_REFUSALS:
                return ("model", reason)
            if kind in TRANSIENT_ERRORS:
                return ("transient", reason)
            return None
        return None

    def usage(self, raw: bytes) -> Usage | None:
        """``None``: not read yet. The ``step_finish`` events carry token counts; reading them
        belongs here, and a guessed cost printed beside a measured one is worse than none.
        """
        del raw
        return None


#: The single instance the registry hands out. Stateless, so one is enough.
HARNESS: Final = OpenCodeHarness()
