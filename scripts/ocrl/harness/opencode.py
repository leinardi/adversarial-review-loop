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

**Session continuity here is discovery, not assignment** (:class:`DiscoveredSessions`):
``opencode run`` creates the session itself and offers no way to name it in advance, so the
gate passes a unique ``--title`` and reads the id back out of ``opencode session list``. The
listing, its cap and its timeout therefore belong to this module rather than to the gate --
a harness that pre-assigns its sessions makes no such call at all, and the gate's claim
leases are sized from :attr:`DiscoveredSessions.capture_timeout_sec` for exactly that reason.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ocrl import reviewer_probe
from ocrl.atomic import FILE_MODE, ensure_private_dir
from ocrl.config import Config
from ocrl.harness import Captured, CaptureSpec, ClarifySpec, Command, ReviewSpec
from ocrl.paths import state_root
from ocrl.util import log

__all__ = [
    "HARNESS",
    "SESSIONS",
    "SESSION_ID_RE",
    "SESSION_LIST_MAX",
    "SESSION_LIST_TIMEOUT_SEC",
    "DiscoveredSessions",
    "OpenCodeHarness",
    "clarify_argv",
    "isolation_argv",
    "isolation_env",
    "permission",
    "review_argv",
]

#: ``model``'s default under this harness.
DEFAULT_MODEL: Final = "openai/gpt-5.6-sol"

#: How long a ``session list`` call is given. Metadata, not a model call -- bounded well below
#: the review timeout.
SESSION_LIST_TIMEOUT_SEC: Final = 60

#: How many sessions ``session list`` is asked for. The continuity pointer must appear inside
#: this window or continuity silently drops, so :meth:`DiscoveredSessions.verify` logs the row
#: count whenever a listing fails to match -- a count equal to this cap is what distinguishes
#: "the session is gone" from "it fell off the end of the list", which are the same silent
#: fresh review today.
SESSION_LIST_MAX: Final = 50

#: A canonical OpenCode session id. Matched before a stored or listed id is ever compared,
#: joined, or shown to a reviewer -- see ``reviewer._pointer_structurally_usable``,
#: ``reviewer.capture_session`` and ``reviewer.continuity_summary``, all of which reach it
#: through :meth:`DiscoveredSessions.is_session_id`.
#:
#: **Anchored with ``\\Z``, not ``$``, and that is the whole point of the anchor.** Python's ``$``
#: also matches immediately before a single trailing newline, so ``"ses_abcdefgh\\n"`` satisfied
#: ``^ses_[A-Za-z0-9]{8,64}$`` -- an id read out of ``state.json``, which is not a trust boundary.
#: Nothing could be smuggled *after* the break (a second newline, or any trailing text, already
#: failed), but a stored id ending in one still rendered a line break into `continuity_summary`'s
#: status line and travelled as a session id everywhere else. ``\\Z`` matches only at the true end
#: of the string, so every one of the call sites tightens together -- which is what keeps the
#: summary exactly as strict as the gate rather than more so.
SESSION_ID_RE: Final = re.compile(r"^ses_[A-Za-z0-9]{8,64}\Z")


def isolation_argv(config: Config) -> list[str]:
    """The flags that keep any reviewer-adjacent OpenCode call structurally isolated.

    Shared by :func:`review_argv` and :func:`_list_sessions`, so a unit test can assert
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


def _same_repo(directory: object, repo: str) -> bool:
    """Canonicalised on both sides, so a symlinked path reads as neither a false mismatch
    nor a false match."""
    return isinstance(directory, str) and os.path.realpath(directory) == os.path.realpath(repo)


def _list_sessions(*, repo: str, config: Config, act_dir: Path, seq: str) -> list[Any] | None:
    """``opencode session list --format json -n 50``, parsed -- or ``None`` on anything else.

    Written to ``act_dir/tmp/session-list-<seq>.json`` and deleted in a ``finally``, never
    inside the bundle: the bundle is what a continued reviewer can read back
    (:func:`permission`), and this listing names every other OpenCode session in the
    repository -- ids, titles and all, including the user's own unrelated work.

    ``OCRL_SESSION_LIST_CMD`` is the test seam, parallel to ``OCRL_REVIEWER_CMD``: a stand-in
    that writes the same JSON shape to stdout. When the reviewer seam is active and this one
    is not, the call is skipped entirely rather than reaching a real ``opencode`` -- exactly
    like every other reviewer-adjacent call under that seam.
    """
    from ocrl.reviewer import run_bounded  # noqa: PLC0415 - the gate imports this module at module scope; a top-level import back would be a cycle

    reviewer_cmd = os.environ.get("OCRL_REVIEWER_CMD", "")
    session_list_cmd = os.environ.get("OCRL_SESSION_LIST_CMD", "")
    if reviewer_cmd and not session_list_cmd:
        return None

    if session_list_cmd:
        argv = [session_list_cmd]
        env = dict(os.environ)
    else:
        argv = ["opencode", *isolation_argv(config), "session", "list", "--format", "json", "-n", str(SESSION_LIST_MAX)]
        env = isolation_env(config, dict(os.environ))

    listing_path = act_dir / "tmp" / f"session-list-{seq}.json"
    try:
        ensure_private_dir(listing_path.parent, root=state_root())
        fd = os.open(listing_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
        with os.fdopen(fd, "wb") as sink:
            status = run_bounded(argv, stdout=sink, timeout_sec=SESSION_LIST_TIMEOUT_SEC, env=env, cwd=repo)
        if status != 0:
            log(f"session list exited with status {status}")
            return None
        data = json.loads(listing_path.read_bytes())
        if not isinstance(data, list):
            log("session list output was not a JSON list")
            return None
    except Exception as exc:
        log(f"session list failed: {exc}")
        return None
    finally:
        listing_path.unlink(missing_ok=True)
    return data


class DiscoveredSessions:
    """OpenCode's session continuity: created by the run, found afterwards by title.

    ``opencode run`` gives no way to name the session it is about to create, so the gate
    passes a ``--title`` unique enough to match on (``reviewer._unique_title``) and reads the
    id back out of ``opencode session list`` once the run is over. That is why
    :meth:`capture_timeout_sec` is a full listing rather than zero, and why :meth:`mint`
    answers ``""``.

    **Every match is required to be exactly one row**, on both the capture and the verify
    side. Two rows carrying our title means something else created one, and continuing a
    session the gate is only guessing at is worse than starting fresh -- a fresh review costs
    tokens, a wrong one costs the round.
    """

    @property
    def capture_timeout_sec(self) -> int:
        # Read at call time, not bound at class definition: the module constant is what tests
        # shrink to drive the both-deadlines-expire path end to end.
        return SESSION_LIST_TIMEOUT_SEC

    def is_session_id(self, value: object) -> bool:
        return isinstance(value, str) and SESSION_ID_RE.match(value) is not None

    def mint(self) -> str:
        """``""``: ``opencode run`` has no flag that pre-assigns a session id."""
        return ""

    def verify(self, pointer: Mapping[str, Any], *, repo: str, config: Config, act_dir: Path, seq: str) -> bool:
        """Is the remembered session still exactly one row of a live ``session list``?

        Checks the id **and** the title, ``created`` and directory recorded with it, not the
        id alone: an id is only a name, and the row it names has to be the same session, in
        the same repository, that the pointer was written from.
        """
        session_id = pointer.get("id")
        rows = _list_sessions(repo=repo, config=config, act_dir=act_dir, seq=seq)
        if rows is None:
            # `_list_sessions` has already logged *why*; the caller logs the consequence.
            return False
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("id") == session_id
            and row.get("title") == pointer.get("title")
            and row.get("created") == pointer.get("created")
            and _same_repo(row.get("directory"), repo)
        ]
        if len(matches) == 1:
            return True
        # The row count, and whether it saturated the cap, is what separates the two failure
        # modes this branch merges: a session that is genuinely gone, and one that is merely
        # past `SESSION_LIST_MAX` in a busy project. Only the second is worth raising the cap
        # for, and without this line neither is distinguishable from the other.
        saturated = " -- the listing is saturated, so the session may simply be past the cap" if len(rows) >= SESSION_LIST_MAX else ""
        log(
            f"session continuity: {session_id} did not match exactly one listed session ({len(rows)} rows returned, cap {SESSION_LIST_MAX}{saturated})"
        )
        return False

    def capture(self, spec: CaptureSpec) -> Captured:
        """The session ``opencode session list`` shows for this fresh run's title, or falsy.

        Every failure -- non-zero exit, timeout, unparseable JSON, a row predating this run,
        no match -- is :func:`log` plus the empty result; this must never be able to fail a
        review. ``spec.new_session_id`` is always ``""`` here: nothing was pre-assigned, so
        there is nothing to hand back and the listing is the only source.
        """
        rows = _list_sessions(repo=spec.repo, config=spec.config, act_dir=spec.act_dir, seq=spec.seq)
        if rows is None:
            return Captured()
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("title") == spec.title
            and self.is_session_id(row.get("id"))
            and isinstance(row.get("created"), (int, float))
            and row["created"] >= spec.started_ms
            and _same_repo(row.get("directory"), spec.repo)
        ]
        if len(matches) != 1:
            if matches:
                log(f"capture_session: {len(matches)} rows matched title {spec.title!r} in the window; not guessing")
            return Captured()
        row = matches[0]
        return Captured(session_id=str(row["id"]), created=int(row["created"]))


#: The single instance :class:`OpenCodeHarness` hands out. Stateless, so one is enough.
SESSIONS: Final = DiscoveredSessions()


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

    def sessions(self) -> DiscoveredSessions:
        """Post-hoc discovery by unique title. See :class:`DiscoveredSessions`."""
        return SESSIONS

    def probe_models(self, timeout: float) -> list[str] | None:
        """``opencode models``. Raises ``reviewer_probe.ProbeFailed`` if it does not answer."""
        return reviewer_probe.list_models(timeout)


#: The single instance the registry hands out. Stateless, so one is enough.
HARNESS: Final = OpenCodeHarness()
