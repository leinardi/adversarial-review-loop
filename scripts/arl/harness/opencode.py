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
from arl.harness import ClarifySpec, Command, PayloadError, ReviewSpec, Usage, model

__all__ = [
    "AGENT",
    "DEFAULT_MODEL",
    "HARNESS",
    "OpenCodeHarness",
    "config_content",
    "isolation_argv",
    "isolation_env",
    "permission",
    "review_argv",
]

#: ``model``'s default under this harness.
DEFAULT_MODEL: Final = "openai/gpt-5.6-sol"


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
    # `HARNESS`, not the configured harness: an OpenCode argv carries OpenCode's default model
    # even when this is called directly.
    chosen = model(config, HARNESS)
    variant = config.as_str("variant")
    if variant and "#" in chosen:
        raise PayloadError(f"model {chosen!r} already names a variant; unset variant ({variant!r}) or drop it from model")
    argv += ["-m", f"{chosen}#{variant}" if variant else chosen]
    # `--archived`: a review is not a session the person goes back to, so it stays out of their list.
    argv += ["--agent", AGENT, "--title", title, "--archived"]
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
    default_model: Final = DEFAULT_MODEL

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

    def clarify_command(self, spec: ClarifySpec) -> Command:
        """A clarify run: the same shape as a review, reading the same bundle."""
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
        """``raw``, unchanged: ``opencode run`` writes the answer and nothing around it.

        There is no wrapper to unwrap and no second channel of failure to inspect -- what this
        CLI reports about the run, it reports through its exit status, which
        :func:`arl.runner.invoke` has already checked by the time this is called.
        """
        return raw

    def usage(self, raw: bytes) -> Usage | None:
        """``None``: the transcript is the answer text, with no accounting around it.

        Not a gap to be filled by estimating from the payload -- a guessed cost printed beside
        a measured one in the same report is worse than no figure at all. If this CLI grows a
        machine-readable output mode, this is where reading it belongs.
        """
        del raw
        return None


#: The single instance the registry hands out. Stateless, so one is enough.
HARNESS: Final = OpenCodeHarness()
