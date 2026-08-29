"""The OpenCode harness: how this gate spells a review for ``opencode run``.

Everything here was previously inline in :mod:`ocrl.reviewer` and is moved verbatim --
the argv shapes, the ``OPENCODE_PERMISSION`` document, and the isolation flags. Its
docstrings carry the reasons each one is shaped the way it is, several of which record
live bugs; they are the argument for the code and travel with it.

**Attachments reach OpenCode through ``-f``, which inlines them.** That is load-bearing
for the evidence boundary :mod:`ocrl.reviewer` documents: a ``context/`` attachment is
inlined into the prompt rather than handed over as a path, so no invocation can re-open
one by name, and a cold confirmation -- passed none of them -- structurally cannot have
seen model-authored prose.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from ocrl import reviewer_probe
from ocrl.config import Config
from ocrl.harness import ClarifySpec, Command, ReviewSpec

__all__ = [
    "HARNESS",
    "OpenCodeHarness",
    "clarify_argv",
    "isolation_argv",
    "isolation_env",
    "permission",
    "review_argv",
]

#: ``model``'s default under this harness.
DEFAULT_MODEL: Final = "openai/gpt-5.6-sol"


def isolation_argv(config: Config) -> list[str]:
    """The flags that keep any reviewer-adjacent OpenCode call structurally isolated.

    Shared by :func:`review_argv` and ``reviewer._list_sessions``, so a unit test can assert
    the two cannot drift apart -- see that test's own docstring for why this matters: a
    ``session list`` call missing these flags would load the repository under review's own
    OpenCode plugins and project config while running *from inside* that repository, which is
    exactly the boundary the reviewer's own isolation exists to hold.
    """
    return ["--pure"] if config.as_bool("pure") else []


def isolation_env(config: Config, base: dict[str, str]) -> dict[str, str]:
    """``base``, plus the isolation env vars, iff configured. Never mutates ``base``."""
    env = dict(base)
    if config.as_bool("disable_project_config"):
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    return env


def review_argv(repo: str, title: str, *, config: Config, session_id: str = "", attachments: Sequence[Path] = ()) -> list[str]:
    """The flags that follow the prompt.

    The prompt is **not** routed through here. ``-f`` is a yargs *array* option, so it keeps
    swallowing arguments: a prompt placed after the attachments would be read as one more
    attachment path. It goes immediately after ``run`` instead.

    ``--title`` and ``-s`` are mutually exclusive: ``-s <session_id>`` continues a remembered
    session and is passed alone; a fresh run passes ``--title`` instead, and only a fresh run
    -- re-passing a newer-sequence title on a continuation would rename the row the stored id
    was matched against. See ``session_ref``.

    ``attachments`` is the complete, ordered ``-f`` list, passed in and **never derived here**
    -- not by glob, not by existence check. Two separate reasons, and both were live bugs:

    - a glob attaches whatever happens to be sitting in the directory, so a planted
      ``changes.99.diff`` symlink rode into the provider prompt. The list now comes from
      ``bundle_manifest``, which is driven by the bundle's own ``chunks`` count and
      rejects extras;
    - "what was attached" must be **one** value, decided once. ``execute`` gates its cold
      confirmation on whether model-derived context was among these, and a second, later
      derivation from the filesystem could disagree with the first.

    See ``reviewer.Invocation``, which carries both this list and the subset of it that is
    model-derived.
    """
    argv: list[str] = [*isolation_argv(config)]
    argv += ["--dir", repo]
    argv += ["-m", config.as_str("model")]
    variant = config.as_str("variant")
    if variant:
        argv += ["--variant", variant]
    if session_id:
        argv += ["-s", session_id]
    else:
        argv += ["--title", title]
    for attachment in attachments:
        argv += ["-f", str(attachment)]
    return argv


def clarify_argv(repo: str, attachments: Sequence[Path], question_file: Path, title: str, *, config: Config) -> list[str]:
    """The bounded argv for a clarify run.

    Deliberately narrower than :func:`review_argv`: exactly ``attachments`` -- the stored
    bundle's ``range.txt`` then its ``changes.NN.diff`` chunks, **as a caller-supplied list,
    never a directory glob here** -- then the one question file. No plan revisions, no
    ``prior-rounds.txt``, no ``verify.txt``, and above all **no ``-s``**. A clarify never
    continues a session (see ``commands/clarify.py`` for why binding it to the continuity
    pointer would be wrong) and never captures one, so ``--title`` is passed purely because
    ``opencode run`` wants one -- the row it names is never matched against later.

    The attachment list comes from ``commands.clarify._bundle_attachments``, which builds it
    from the bundle's own ``chunks`` manifest and refuses any extra or symlinked
    ``changes.*.diff`` -- so a file dropped into ``bundles/<seq>/`` cannot be inlined to the
    provider through ``-f`` by riding a glob.
    """
    argv: list[str] = [*isolation_argv(config)]
    argv += ["--dir", repo]
    argv += ["-m", config.as_str("model")]
    variant = config.as_str("variant")
    if variant:
        argv += ["--variant", variant]
    argv += ["--title", title]
    for path in attachments:
        argv += ["-f", str(path)]
    argv += ["-f", str(question_file)]
    return argv


def permission(bundle_dir: Path, *, cold: bool = False) -> str:
    """``OPENCODE_PERMISSION`` for a structurally read-only reviewer.

    The bundle lives outside the repository (Rule 3), so ``external_directory`` is denied
    everywhere except the bundles root -- ``bundle_dir.parent``, not the activation directory,
    which also holds ``state.json``, ``plan.frozen.md`` and the reports. Widened from a single
    bundle to the whole bundles root so a continued reviewer can re-open paths it remembers
    from an earlier round's bundle; every one of them is still gate-generated evidence only,
    never model output -- see ``reviewer``'s module docstring, "bundles/ holds gate-generated
    evidence only". Patterns are last-match-wins, which is why the broad deny is written first
    -- and why the key order below is load-bearing rather than cosmetic.

    ``cold`` narrows the allow to *this one bundle* (``bundle_dir/**``). The wildcard above
    exists so a *continued* reviewer can re-open paths it remembers from an earlier round; a
    cold invocation remembers nothing and needs none of it. Defence in depth behind the
    ``context/`` boundary -- the ``context/`` directory is a sibling of ``bundles/`` and
    outside either allow regardless.
    """
    allowed = bundle_dir if cold else bundle_dir.parent
    document = {
        "*": "deny",
        "read": "allow",
        "grep": "allow",
        "glob": "allow",
        "list": "allow",
        "external_directory": {"*": "deny", f"{allowed}/**": "allow"},
    }
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False)


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
                spec.prompt_text,
                *review_argv(spec.repo, spec.title, config=spec.config, session_id=spec.session_id, attachments=spec.attachments),
            ],
            env=isolation_env(spec.config, {"OPENCODE_PERMISSION": permission(spec.bundle_dir, cold=spec.cold)}),
        )

    def clarify_command(self, spec: ClarifySpec) -> Command:
        """A clarify run: always the bundle-scoped (``cold``) permission document."""
        return Command(
            argv=[
                self.binary,
                "run",
                spec.prompt_text,
                *clarify_argv(spec.repo, spec.attachments, spec.question_file, spec.title, config=spec.config),
            ],
            env=isolation_env(spec.config, {"OPENCODE_PERMISSION": permission(spec.bundle_dir, cold=True)}),
        )

    def probe_models(self, timeout: float) -> list[str] | None:
        """``opencode models``. Raises ``reviewer_probe.ProbeFailed`` if it does not answer."""
        return reviewer_probe.list_models(timeout)


#: The single instance the registry hands out. Stateless, so one is enough.
HARNESS: Final = OpenCodeHarness()
