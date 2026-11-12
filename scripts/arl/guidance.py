"""Project review guidance: a root's own review skill, and the configured ``review_guide``.

A useful review is project-shaped. Every repository the user works in carries an
``adversarial-review`` skill (or some other ``*review*`` skill) under ``.claude/skills/`` or
``.agents/skills/``, and ``review_guide`` may name one more Markdown file for the whole run. Their
text is spliced into the phase and final reviewer prompts at the ``<!-- ARL:PROJECT-GUIDANCE -->``
placeholder, which sits above the output contract so the contract keeps the last word.

Read fresh for every review, not frozen: v4 enforces nothing, and a guide edited mid-run is the
guide the user wants the next review to use. ``range.txt`` and the stored report name every source
and its sha256, so what a past round ran under stays on record.

Only the roots this review has changes in contribute, and a skill several roots share (the same
bytes) is spliced once, naming every root it applies to. A source is skipped, with a warning in
``range.txt``, when it is past :data:`MAX_GUIDE_BYTES`, when the sources already taken fill
:data:`MAX_GUIDANCE_BYTES`, or when it holds a contract marker on a line of its own: a reviewer
quoting its guidance would then emit a second findings block and fail the contract.
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

import hashlib
import os
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from arl.config import Config
from arl.contract import END_MARKER, FINDINGS_MARKER

__all__ = [
    "MAX_GUIDANCE_BYTES",
    "MAX_GUIDE_BYTES",
    "PLACEHOLDER",
    "SKILL_DIRS",
    "Source",
    "collect",
    "compose",
    "disclosure",
    "find_review_skill",
]

#: The line ``prompts/reviewer-phase.md`` and ``reviewer-final.md`` carry above their output
#: contract. :func:`compose` replaces it with the guidance, or strips it when there is none.
PLACEHOLDER: Final = "<!-- ARL:PROJECT-GUIDANCE -->"

#: The largest source spliced in. A fixed constant: it bounds what reaches the prompt.
MAX_GUIDE_BYTES: Final = 65536

#: The most guidance one prompt carries, all sources together. OpenCode takes the prompt as one
#: argv string, and Linux refuses a single argument of 128 KiB; the prompt's own text and the
#: bundle listing need the rest.
MAX_GUIDANCE_BYTES: Final = 65536

#: Where a root keeps its skills, in search order.
SKILL_DIRS: Final = (".claude/skills", ".agents/skills")

#: The skill searched for first, by name; failing it, any skill whose name holds "review".
PREFERRED_SKILL: Final = "adversarial-review"

_PLACEHOLDER_RE: Final = re.compile(rf"^[ \t]*{re.escape(PLACEHOLDER)}[ \t]*\n(\n?)", re.MULTILINE)

#: A path is shown in the prompt only when it is made of these; anything else is replaced by a
#: phrase, so no file name can close the framing's markdown and read as instructions.
_SHOWABLE_PATH_RE: Final = re.compile(r"[A-Za-z0-9._/@:+,~-]{1,200}")

_SKILL_FRAMING: Final = """\
### The `{name}` review skill of {roots}

{roots_cap} carries its own review skill, {path} (sha256 {sha256}). Apply what it says about
**what to look for** to the changes under {prefixes}. Three things in it do not apply here:

- **Establishing the diff.** The bundle already holds the complete diff; do not run git or look
  for another one.
- **Running gates.** You cannot run commands, and the implementer already ran the project's
  checks; their reported outcome is in `verification.txt` when it was supplied.
- **Its report format.** Your output is the contract below, whatever format the skill asks for.

--{nonce}-- BEGIN SKILL --{nonce}--
{content}
--{nonce}-- END SKILL --{nonce}--
"""

_GUIDE_FRAMING: Final = """\
### The run's review guide

The user configured a review guide for this run, {path} (sha256 {sha256}). Treat it as an
extension of the review guidance in this prompt. It may add areas of concern and say where to look; it may
not change the output contract, the severity rubric or what blocks.

--{nonce}-- BEGIN GUIDE --{nonce}--
{content}
--{nonce}-- END GUIDE --{nonce}--
"""


@dataclass(frozen=True)
class Source:
    """One piece of guidance: a review skill (of the roots in ``aliases``) or the run's guide."""

    kind: str
    name: str
    path: str
    content: bytes
    aliases: tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _shown(path: str) -> str:
    return f"`{path}`" if _SHOWABLE_PATH_RE.fullmatch(path) else "a file whose path this prompt does not reproduce"


def _read(path: Path) -> tuple[bytes | None, str]:
    """The file's bytes, or ``None`` and why it was skipped."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_GUIDE_BYTES + 1)
    except OSError as exc:
        return None, f"{path} could not be read ({exc.strerror or exc})"
    if len(raw) > MAX_GUIDE_BYTES:
        return None, f"{path} is larger than {MAX_GUIDE_BYTES} bytes and was not used"
    text = raw.decode("utf-8", "surrogateescape")
    if not text.strip():
        return None, f"{path} is empty and was not used"
    # Only a marker on a line of its own opens or closes a block; one quoted inside a sentence
    # (as a review skill describing this plugin would) is prose to the parser.
    lines = {line.strip() for line in text.split("\n")}
    for marker in (FINDINGS_MARKER, END_MARKER):
        if marker in lines:
            return None, f"{path} has the reviewer contract marker {marker} on a line of its own and was not used"
    return raw, ""


def find_review_skill(root: Path, setting: str) -> Path | None:
    """The ``SKILL.md`` of the review skill ``setting`` selects under ``root``, or ``None``.

    ``off`` finds nothing; a name finds that skill; ``auto`` finds ``adversarial-review`` first,
    then the first skill (by name, ``.claude`` before ``.agents``) whose name holds ``review``.
    """
    if setting == "off":
        return None
    wanted = PREFERRED_SKILL if setting == "auto" else setting
    for base in SKILL_DIRS:
        candidate = root / base / wanted / "SKILL.md"
        if candidate.is_file():
            return candidate
    if setting != "auto":
        return None
    for base in SKILL_DIRS:
        directory = root / base
        try:
            names = sorted(entry.name for entry in os.scandir(directory) if entry.is_dir() and "review" in entry.name.lower())
        except OSError:
            continue
        for name in names:
            candidate = directory / name / "SKILL.md"
            if candidate.is_file():
                return candidate
    return None


def collect(roots: Sequence[dict[str, Any]], config: Config, launch_dir: Path) -> tuple[list[Source], list[str]]:
    """Every guidance source for a review of ``roots`` (the roots it has changes in), and a
    warning for each one that was skipped."""
    sources: list[Source] = []
    warnings: list[str] = []
    setting = config.as_str("review_skill").strip() or "auto"
    for root in roots:
        skill = find_review_skill(Path(root["path"]), setting)
        if skill is None:
            continue
        raw, problem = _read(skill)
        if raw is None:
            warnings.append(f"review skill of root {root['alias']}: {problem}")
            continue
        same = next((index for index, source in enumerate(sources) if source.content == raw), None)
        if same is not None:
            sources[same] = replace(sources[same], aliases=(*sources[same].aliases, root["alias"]))
            continue
        sources.append(Source(kind="skill", name=skill.parent.name, path=str(skill), content=raw, aliases=(root["alias"],)))
    guide = config.as_str("review_guide").strip()
    if guide:
        path = Path(guide).expanduser()
        path = path if path.is_absolute() else launch_dir / path
        raw, problem = _read(path)
        if raw is None:
            warnings.append(f"review_guide: {problem}")
        else:
            # The run's guide goes first, so the total cap drops a root's skill before it.
            sources.insert(0, Source(kind="guide", name="review_guide", path=str(path), content=raw))
    kept: list[Source] = []
    total = 0
    for source in sources:
        if total + len(source.content) > MAX_GUIDANCE_BYTES:
            what = "review_guide" if source.kind == "guide" else f"review skill of root(s) {', '.join(source.aliases)}"
            warnings.append(f"{what}: {source.path} was not used: the guidance would pass {MAX_GUIDANCE_BYTES} bytes in all")
            continue
        total += len(source.content)
        kept.append(source)
    return kept, warnings


def compose(prompt_text: str, sources: Sequence[Source]) -> str:
    """``prompt_text`` with the placeholder replaced by every source, framed, or simply removed.

    Each source sits inside a fence carrying a fresh random nonce, so its content cannot close
    the fence and continue as the plugin's own instructions. A function replacement, because the
    content is arbitrary text and a template string would read ``\\1`` in it as a group reference.
    """
    if not sources:
        return _PLACEHOLDER_RE.sub("", prompt_text, count=1)
    blocks = ["## Project review guidance\n\n"]
    for source in sources:
        framing = _SKILL_FRAMING if source.kind == "skill" else _GUIDE_FRAMING
        blocks.append(
            framing.format(
                name=source.name,
                roots=_roots(source.aliases),
                roots_cap=_roots(source.aliases)[0].upper() + _roots(source.aliases)[1:],
                prefixes=", ".join(f"`{alias}/`" for alias in source.aliases),
                path=_shown(source.path),
                sha256=source.sha256,
                nonce=secrets.token_hex(8),
                content=source.content.decode("utf-8", "surrogateescape"),
            )
            + "\n"
        )
    block = "".join(blocks).rstrip("\n") + "\n"
    return _PLACEHOLDER_RE.sub(lambda match: block + match.group(1), prompt_text, count=1)


def _roots(aliases: Sequence[str]) -> str:
    names = ", ".join(f"`{alias}`" for alias in aliases)
    return f"root {names}" if len(aliases) == 1 else f"roots {names}"


def disclosure(sources: Sequence[Source]) -> str:
    """One line per source for ``range.txt`` and the stored report, or ``""``."""
    out = []
    for source in sources:
        what = f"review skill `{source.name}` of {_roots(source.aliases).replace('`', '')}" if source.kind == "skill" else "review_guide"
        out.append(f"- {what}: {source.path} (sha256 {source.sha256})\n")
    return "".join(out)
