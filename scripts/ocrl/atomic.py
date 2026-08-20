"""Durable, private writes for everything the gate persists.

Three properties, none of which the shell implementation had:

**A failed write must never destroy good state.** ``ocrl_state_save`` piped into ``jq`` and
then ran an unconditional ``mv -f``. With ``set -e`` off and the status unchecked, a ``jq``
failure truncated the temp file and the rename published the empty result over a perfectly
good ``state.json``. Here the rename happens only after the full document has been
serialised, written, flushed and fsynced -- and the containing directory is fsynced after
it, so a crash cannot resurrect the previous name.

**State is private, provably.** The state root holds frozen plans and review reports. The
shell set no ``umask`` and no mode anywhere, so it inherited the caller's -- typically
``0755`` directories and ``0644`` files. These helpers create ``0700``/``0600`` explicitly
and tighten anything already on disk, because ``os.makedirs(mode=...)`` is ignored for a
directory that already exists.

**Nothing is created before it has been checked.** Every component below the state root is
walked with descriptor-relative ``openat``/``mkdirat`` under ``O_NOFOLLOW``, one level at a
time. A plain ``mkdir(parents=True)`` would follow a planted symlink and *create the
directories on the far side of it* before any validation could run -- and checking
afterwards is too late, because the mutation has already happened. Descriptor-relative
traversal also closes the check/create race: the mode is set through the same descriptor
that was verified, so there is no window in which the path could be swapped.

The state root itself is opened normally rather than with ``O_NOFOLLOW``: its location is
the user's own configuration (``OCRL_STATE_DIR``, ``XDG_STATE_HOME``), symlinking it is
legitimate, and its parents are outside anything the repository under review can influence.
The boundary that matters starts at the components the gate creates for itself.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from ocrl.errors import UnsafePathError

__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "ensure_private_dir",
    "locked",
    "private_dir_fd",
    "write_private_atomic",
]

DIR_MODE = 0o700
FILE_MODE = 0o600


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    """Components of ``path`` below ``root``, refusing anything that is not under it.

    Lexical is enough here precisely because the walk that follows refuses symlinks: no
    component can mean somewhere other than where it reads.
    """
    try:
        return path.relative_to(root).parts
    except ValueError as exc:
        raise UnsafePathError(f"{path} is not inside the state root {root}") from exc


def _tighten(fd: int, what: Path) -> None:
    """Make the directory behind ``fd`` private, through the descriptor itself.

    Compared before being set, so a steady-state run pays one ``fstat`` and no ``fchmod``.
    Failures are **not** suppressed: carrying on would mean writing state the gate cannot
    show is private.
    """
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafePathError(f"{what} is not a directory")
    if stat.S_IMODE(info.st_mode) != DIR_MODE:
        os.fchmod(fd, DIR_MODE)


def _descend(parent_fd: int, name: str, whole: Path) -> int:
    """Create ``name`` under ``parent_fd`` if needed and open it, refusing symlinks."""
    # Already there is the normal case; whether it is *acceptable* is decided by the open
    # below, not here.
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, mode=DIR_MODE, dir_fd=parent_fd)
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        # Which errno arrives depends on what the component is *and* what it points at:
        # Linux answers ENOTDIR for a symlink to a directory under O_NOFOLLOW|O_DIRECTORY,
        # and ELOOP for one to anything else. Ask the filesystem rather than guess, so the
        # message names the real problem.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            if _is_symlink(parent_fd, name):
                raise UnsafePathError(f"{whole}: component {name!r} is a symlink; the gate will not write state through one") from exc
            raise UnsafePathError(f"{whole}: component {name!r} is not a directory") from exc
        raise


def _is_symlink(parent_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode)
    except OSError:
        return False


@contextlib.contextmanager
def private_dir_fd(path: Path, *, root: Path) -> Iterator[int]:
    """Yield a descriptor for ``path``, created and tightened without following symlinks.

    Every component from ``root`` down is created, verified and made private one level at a
    time. Callers do their file operations relative to the yielded descriptor, so the
    directory they write into is the same one that was checked.
    """
    parts = _relative_parts(path, root)

    # The root's own parents are outside the gate's boundary; create them plainly and do
    # not touch their modes.
    os.makedirs(root, mode=DIR_MODE, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _tighten(fd, root)
        for index, name in enumerate(parts):
            child = _descend(fd, name, path)
            os.close(fd)
            fd = child
            _tighten(fd, root.joinpath(*parts[: index + 1]))
        yield fd
    finally:
        os.close(fd)


def ensure_private_dir(path: Path, *, root: Path) -> None:
    """Create ``path`` and every component between it and ``root``, all mode ``0700``."""
    with private_dir_fd(path, root=root):
        pass


def write_private_atomic(path: Path, text: str, *, root: Path) -> None:
    """Publish ``text`` at ``path`` atomically, mode ``0600``.

    The temporary file is created in the destination directory -- ``os.replace`` is only
    atomic within a filesystem -- and with an explicit mode, so the result does not depend
    on the caller's umask. If anything fails before the rename, the previous contents are
    still there, byte for byte.
    """
    tmp_name = f"{path.name}.tmp.{os.getpid()}"
    with private_dir_fd(path.parent, root=root) as dir_fd:
        # O_EXCL so the write can never follow a symlink planted at the temp name.
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=dir_fd)
            raise
        # Only now is the new document known to be complete on disk.
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        # `os.replace` orders the file's contents but not the directory entry naming them.
        # Without this, a crash just after `pointer_write` returned could restore the
        # previous pointer -- and the hooks in a freshly armed worktree would then read the
        # old worktree and scope themselves out of the activation they were registered for.
        os.fsync(dir_fd)


@contextlib.contextmanager
def locked(lock_path: Path, *, root: Path) -> Iterator[None]:
    """Hold an exclusive lock across a read-modify-write of shared state.

    Atomic rename prevents a torn file but not a lost update: a ``PostToolUse`` hook and a
    user-run ``defer`` can interleave, and the loser's change -- an escalation, or a cleared
    pending approval -- would simply vanish.

    Failure to take the lock is deliberately **not** swallowed. It propagates to the
    entrypoint's fail-closed guard, which denies (Rule 1): a gate that cannot serialise its
    own state cannot show that it is enforcing anything.
    """
    with private_dir_fd(lock_path.parent, root=root) as dir_fd:
        fd = os.open(lock_path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE, dir_fd=dir_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            # Closing the descriptor releases the lock.
            os.close(fd)
