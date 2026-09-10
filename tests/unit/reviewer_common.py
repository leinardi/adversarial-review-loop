"""Fixtures and helpers shared by the ``test_reviewer_*`` modules.

Split out of the former single ``test_reviewer.py`` so each subsystem gets its own file
without copying the bundle builders, the scripted-reviewer drivers and the isolated
environment into eight of them.
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
import os
import subprocess
import time
from pathlib import Path

from conftest import FAKE_REVIEWER, config_with, git

from arl import gitsnap, reviewer, state
from arl.config import Config
from arl.harness import opencode as opencode_harness
from arl.reviewer import Invocation, Review, Target

#: What the ANSI-stripping test expects once the escapes are gone.
PLAIN_VERDICT = b"VERDICT APPROVED\n"


#: Every stand-in reviewer mode, with the verdict the gate must reach. Nothing here is
#: ``APPROVED`` unless the reviewer both said so and left no actionable finding behind.
MODE_VERDICTS = [
    ("approve", "APPROVED"),
    ("approve-with-nit", "APPROVED"),
    ("changes", "CHANGES_REQUIRED"),
    ("approve-with-critical", "CHANGES_REQUIRED"),
    ("critical-nonactionable", "APPROVED"),
    ("malformed", "OP_FAILURE"),
    ("no-verdict", "OP_FAILURE"),
    ("empty", "OP_FAILURE"),
    ("big-prose", "CHANGES_REQUIRED"),
    ("many", "CHANGES_REQUIRED"),
    # Blocks the contract does not allow. Each carries the reviewer's own APPROVED, and
    # each of them used to get it.
    ("bad-actionable", "OP_FAILURE"),
    ("bad-severity", "OP_FAILURE"),
    ("mangled-finding", "OP_FAILURE"),
    ("stray-end", "OP_FAILURE"),
    ("two-blocks", "OP_FAILURE"),
    ("two-verdicts", "OP_FAILURE"),
    ("chatty-block", "OP_FAILURE"),
    ("inline-start-marker", "OP_FAILURE"),
    ("suffixed-end-marker", "OP_FAILURE"),
    ("nul-byte", "OP_FAILURE"),
]


#: How long the spawner's descendant waits before touching its marker, and how long past
#: that a test waits before declaring it never did.
DESCENDANT_DELAY_SEC = 2.0


DESCENDANT_MARGIN_SEC = 1.0


def assert_descendant_never_ran(marker: Path, *, started: float) -> None:
    """Wait until the descendant's own deadline is well past, then require its silence.

    Timed from when the reviewer was *launched*, not from when the kill returned: what has
    to elapse is the descendant's ``sleep``, and sleeping a flat interval afterwards paid for
    the timeout and the kill grace twice over.
    """
    remaining = started + DESCENDANT_DELAY_SEC + DESCENDANT_MARGIN_SEC - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    assert not marker.exists(), "a descendant of the reviewer outlived the deadline"


def discovery_config(**overrides: object) -> Config:
    """:func:`config_with`, pinned to the harness whose sessions are *discovered*.

    Every test that drives ``ARL_SESSION_LIST_CMD``, plants a ``ses_…`` pointer or asserts on
    a listing is exercising ``arl.harness.opencode.DiscoveredSessions`` in particular, not
    whichever harness ``config.DEFAULTS`` happens to name -- the other strategy pre-assigns its
    ids and makes no listing call at all, so those tests would be asserting on a code path that
    never runs. Pinning it here is what keeps them about the strategy, and what lets the default
    harness move without rewriting them; ``AssignedSessions`` has its own coverage in
    ``test_harness_claudecode.py``.
    """
    return config_with(harness=opencode_harness.HARNESS.name, **overrides)


def fake_reviewer_output(tmp_path: Path, mode: str, **env: str) -> Path:
    """Run the stand-in reviewer and keep its output, as ``invoke`` would have."""
    out = tmp_path / f"reviewer-{mode}.out"
    with out.open("wb") as sink:
        subprocess.run(
            [str(FAKE_REVIEWER), str(tmp_path), "prompt"],
            stdout=sink,
            stderr=subprocess.STDOUT,
            env={**os.environ, "ARL_FAKE_MODE": mode, **env},
            check=False,
        )
    return out


def dirty(repo: Path, text: str = "phase one\n") -> str:
    (repo / "a.txt").write_text(text)
    return gitsnap.snapshot(str(repo)).tree


def _intact_bundle(root: Path, *, chunks: int = 1, revisions: int = 0, context: bool = False, verify: bool = False) -> tuple[Path, str]:
    """A bundle shaped exactly as ``build_bundle`` writes one, plus its manifest digest.

    ``root`` doubles as the state root and the activation directory, which is all the manifest
    needs to resolve its ``bundle``/``context`` rows.
    """
    seq = "002"
    bundle = root / "bundles" / seq
    bundle.mkdir(parents=True)
    (bundle / "range.txt").write_text("r")
    for index in range(chunks):
        (bundle / f"changes.{index:02d}.diff").write_text(f"chunk {index}")
    (bundle / "chunks").write_text(str(chunks))
    # `> 1`, exactly as `build_bundle` writes them: an unrevised plan is carried by `range.txt`
    # alone, so there is no `plan.rev0.md` for the manifest to name. Writing one here anyway
    # would make this helper's bundle a shape the real code never produces.
    if revisions > 1:
        for index in range(revisions):
            (bundle / f"plan.rev{index}.md").write_text(f"revision {index}")
    if context:
        (root / "context").mkdir(exist_ok=True)
        (root / "context" / f"{seq}-prior-rounds.txt").write_text("history")
    digest = _seal(bundle, root, total=chunks, revisions=revisions, verify=verify)
    return bundle, digest


def _seal(bundle: Path, root: Path, *, total: int, revisions: int, verify: bool = False) -> str:
    """Hash and write a manifest the way ``build_bundle`` does: canonical evidence first, then
    ``verify.txt``'s own row appended after (it is ``verify_cmd``'s output, so it can only be
    hashed once that has run)."""
    rows = reviewer._hashed_rows(reviewer._manifest_rows(bundle, root, total=total, revisions=revisions))
    if verify:
        (bundle / "verify.txt").write_text("v")
        rows.append(reviewer._hashed_row(("bundle", "verify.txt", bundle / "verify.txt")))
    return reviewer._write_manifest(bundle, rows)


def target_for(repo: Path, *, scope: str = "phase", phase: int = 1) -> Target:
    """A review of the current working state against HEAD's tree."""
    return Target(repo=str(repo), base=git(repo, "rev-parse", "HEAD^{tree}"), head=dirty(repo), scope=scope, phase=phase)


def build(activation: state.State, repo: Path, dest: Path, config: Config | None = None, *, warnings: str = "") -> Path:
    reviewer.build_bundle(target_for(repo), dest, state=activation, config=config or config_with(), warnings=warnings)
    return dest


def build_final(activation: state.State, repo: Path, dest: Path) -> Path:
    reviewer.build_bundle(target_for(repo, scope="final"), dest, state=activation, config=config_with())
    return dest


def invocation(tmp_path: Path, out_name: str = "reviewer.out") -> Invocation:
    return Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / out_name)


def spawner(tmp_path: Path, marker: Path, *, delay: float = 2.0, deaf: bool = False, name: str = "spawner.sh") -> Path:
    """A command that backgrounds a child outliving it, then blocks until killed.

    ``deaf`` makes the child ignore ``SIGTERM``, which is the case that survived a
    group-wide ``SIGTERM`` followed by a wait on the direct child: the parent dies on
    schedule and the descendant does not.
    """
    script = tmp_path / name
    trap = "trap '' TERM; " if deaf else ""
    script.write_text(f"#!/usr/bin/env bash\n( {trap}sleep {delay}; touch {marker!s} ) &\nsleep 30\n")
    script.chmod(0o755)
    return script


def contract(*lines: str) -> str:
    return "prose line\n\n" + reviewer.FINDINGS_MARKER + "\n" + "".join(f"{line}\n" for line in lines) + reviewer.END_MARKER + "\n"


def execute_fake(activation: state.State, repo: Path, mode: str, *, config: Config | None = None, scope: str = "phase") -> Review:
    os.environ["ARL_REVIEWER_CMD"] = str(FAKE_REVIEWER)
    os.environ["ARL_FAKE_MODE"] = mode
    return reviewer.execute(target_for(repo, scope=scope), state=activation, config=config or config_with())


def _scripted_reviewer(tmp_path: Path, name: str, contract: str) -> None:
    """Install a reviewer stand-in whose output is a fixed script. Split from
    :func:`_run_scripted` so a round that needs a non-default config can run ``execute``
    itself."""
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/usr/bin/env bash\nprintf '%b' '{contract}'\n")
    script.chmod(0o755)
    os.environ["ARL_REVIEWER_CMD"] = str(script)


def _run_scripted(activation: state.State, repo: Path, tmp_path: Path, name: str, contract: str) -> Review:
    """One round whose reviewer output is a fixed script -- ``execute_fake``'s canned modes
    all repeat the same finding every round, which cannot exercise a reversal."""
    _scripted_reviewer(tmp_path, name, contract)
    return reviewer.execute(target_for(repo), state=activation, config=config_with())


_ROUND_1 = "Looks off.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=warn.py:1 | needs warn-before\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n"


_ROUND_2 = "Different concern.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=other.py:1 | needs something else\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n"


_ROUND_3 = "Back to the first concern.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=warn.py:9 | needs warn-before after all\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n"


_APPROVES = "All good.\\n\\n<<<ARL-FINDINGS>>>\\nVERDICT APPROVED\\n<<<ARL-END>>>\\n"


_STUCK = "Same problem again.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=stuck.py:1 | still wrong\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n"


def continuity_reviewer(tmp_path: Path) -> Path:
    """Approves iff told it is continuing a session, via ``ARL_SESSION_ID`` -- the env hook
    ``invoke`` sets on the stub path when ``run.session_id`` is non-empty. Makes continuity
    observable in the verdict itself: a continued round approves, a fresh one does not.
    """
    script = tmp_path / "continuity-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [ -n "${ARL_SESSION_ID:-}" ]; then\n'
        "    printf 'Continuing.\\n\\n<<<ARL-FINDINGS>>>\\nVERDICT APPROVED\\n<<<ARL-END>>>\\n'\n"
        "else\n"
        "    printf 'Fresh.\\n\\n<<<ARL-FINDINGS>>>\\n"
        "FINDING severity=high actionable=yes file=a.txt:1 | still there\\n"
        "VERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n'\n"
        "fi\n"
    )
    script.chmod(0o755)
    return script


def session_list_script(tmp_path: Path, rows: list[dict[str, object]], *, name: str = "session-list.sh") -> Path:
    """A stand-in for ``opencode session list --format json``, wired via ``ARL_SESSION_LIST_CMD``."""
    script = tmp_path / name
    script.write_text(f"#!/usr/bin/env bash\ncat <<'JSON'\n{json.dumps(rows)}\nJSON\n")
    script.chmod(0o755)
    return script


def _future_ms() -> int:
    """A ``created`` timestamp guaranteed to be >= any ``started_ms`` computed during a test."""
    return int(time.time() * 1000) + 10_000_000
