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

from ocrl import paths
from ocrl.errors import UnsafePathError

__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "ensure_private_dir",
    "locked",
    "private_dir_fd",
    "read_verified_file",
    "verified_file",
    "write_atomic",
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


def _walk_to_parent(parts: tuple[str, ...], root: Path) -> int:
    """A descriptor for the directory holding ``parts[-1]``, opened without following symlinks.

    Raises ``OSError`` if any component below ``root`` is missing, is not a directory, or is a
    symlink. The caller owns the returned descriptor.
    """
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError:
        os.close(fd)
        raise
    return fd


def read_verified_file(path: Path, *, root: Path) -> bytes | None:
    """The bytes of ``path``, read through the very descriptors that validated it.

    :func:`verified_file` answers a *question*, and by the time a caller acts on the answer
    the path can mean something else -- it validates, closes its descriptors, and hands back a
    pathname somebody else opens later. For a check whose whole purpose is to decide what gets
    uploaded to a third party, that gap matters: the file or any directory above it can become
    a symlink in between, and the bytes that leave are then not the bytes that were checked.

    This closes that half completely. The leaf is opened ``O_NOFOLLOW`` under the same
    descriptor walk that verified its parents, ``fstat``-ed through the open descriptor rather
    than by name, and read from it. There is no window between the check and the read, because
    they are the same operation on the same inode.

    It does **not**, on its own, make what a *different process* later opens by pathname safe;
    see :func:`ocrl.reviewer.stage_attachments` for what is done about that and what remains.

    Answers ``None`` for anything it cannot establish, exactly as :func:`verified_file` does.
    """
    fd = _open_verified(path, root)
    if fd is None:
        return None
    # `os.fdopen` takes ownership of `fd` and closes it on the way out of the `with`.
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _open_verified(path: Path, root: Path) -> int | None:
    """A read-only descriptor for ``path``, or ``None``. Caller owns the descriptor.

    The whole containment argument lives here: components checked against
    :func:`ocrl.paths.is_safe_component` (so no ``..`` walks out lexically), parents opened
    ``O_NOFOLLOW`` one level at a time, the leaf opened ``O_NOFOLLOW`` too, and its type
    settled by ``fstat`` on the open descriptor rather than by name.
    """
    try:
        parts = _relative_parts(path, root)
    except UnsafePathError:
        return None
    if not parts or not all(paths.is_safe_component(part) for part in parts):
        return None
    try:
        parent_fd = _walk_to_parent(parts, root)
    except OSError:
        return None
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        return None
    finally:
        os.close(parent_fd)
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def verified_file(path: Path, *, root: Path) -> bool:
    """``True`` iff ``path`` is a regular file reached from ``root`` without following a symlink.

    The read-side counterpart to :func:`private_dir_fd`, and it creates and modifies nothing.
    It exists because ``Path.is_file()`` is not a safe test for any path that will be handed to
    *another process*: ``is_file`` resolves symlinks, so a planted ``bundles/<seq>`` directory
    link, or a ``context/<seq>-prior-rounds.txt`` link, passes it happily and the file on the
    far side is what rides into the reviewer's ``-f`` argv -- an arbitrary local file uploaded
    to the provider. Checking ``not path.is_symlink()`` on the final component alone does not
    close it either: it says nothing about the directories above, and it is the *directory*
    link that is the easier plant.

    So every component below ``root`` is opened with ``O_NOFOLLOW``, one level at a time, and
    the last is ``lstat``-ed rather than ``stat``-ed. Containment under ``root`` and every
    intermediate link are both decided here, rather than by whatever the path happens to
    resolve to at the moment some other process opens it.

    ``root`` itself is opened normally, for the same reason :func:`private_dir_fd` does it: the
    state root's own location is the user's configuration (``OCRL_STATE_DIR``,
    ``XDG_STATE_HOME``) and symlinking it is legitimate. The boundary starts at the components
    the gate creates for itself.

    Answers ``False`` for everything it cannot establish -- outside ``root``, missing, a
    symlink, a directory, a special file -- and never raises for an ordinary filesystem
    failure: every caller's correct response to any of them is the same, to leave the
    attachment out.

    **Every component is checked with :func:`ocrl.paths.is_safe_component` first, and a ``..``
    is why.** :func:`_relative_parts` is lexical, and ``root/../outside`` is lexically *under*
    ``root``: it relativises to ``("..", "outside")``, and the walk below would then open
    ``..`` happily, because ``..`` is a directory and not a symlink. Refusing symlinks is not
    on its own a containment proof; refusing ``..`` alongside it is what makes it one.
    """
    try:
        parts = _relative_parts(path, root)
    except UnsafePathError:
        return False
    if not parts or not all(paths.is_safe_component(part) for part in parts):
        return False
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return False
    try:
        for name in parts[:-1]:
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                return False
            os.close(fd)
            fd = child
        try:
            info = os.lstat(parts[-1], dir_fd=fd)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode)
    finally:
        os.close(fd)


def write_private_atomic(path: Path, text: str, *, root: Path, errors: str = "strict") -> None:
    """Publish ``text`` at ``path`` atomically, mode ``0600``.

    The temporary file is created in the destination directory -- ``os.replace`` is only
    atomic within a filesystem -- and with an explicit mode, so the result does not depend
    on the caller's umask. If anything fails before the rename, the previous contents are
    still there, byte for byte.

    ``errors`` is ``"strict"`` for everything the gate composes itself, because an
    unencodable character there is a bug worth failing on. Reports pass
    ``"surrogateescape"``: they embed the reviewer's own output, which is not obliged to be
    valid UTF-8, and losing the whole report to one stray byte would throw away the evidence
    for a denial.
    """
    tmp_name = f"{path.name}.tmp.{os.getpid()}"
    with private_dir_fd(path.parent, root=root) as dir_fd:
        # O_EXCL so the write can never follow a symlink planted at the temp name.
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors=errors) as handle:
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


def write_atomic(path: Path, text: str) -> None:
    """Publish ``text`` at ``path`` atomically, touching no directory's mode.

    Used by ``config`` for both of its targets -- the user config file and, via an explicit
    ``--repo``, ``.opencode-review-loop.json`` *inside* the repository under review.
    ``write_private_atomic`` would be wrong for either: ``private_dir_fd`` walks every
    component from its root down and ``fchmod``s each one ``0700``, so rooting a write at the
    repository would tighten the repository directory itself and leave the tracked file
    ``0600`` -- a permission change on a shared checkout that no one asked for. This writer
    creates no directory and chmods none; it only ever touches the destination file and its
    already-existing parent.

    A replace must not widen the file: the temporary file is created at the umask default
    (correct for a genuinely new destination), but when the destination already exists its
    mode is read first and stamped onto the temporary file before the rename, so a config a
    user deliberately left at ``0600`` -- it can hold a ``verify_cmd`` -- comes back ``0600``,
    not the umask default.
    """
    directory = path.parent
    tmp_path = directory / f"{path.name}.tmp.{os.getpid()}"
    try:
        existing_mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        existing_mode = None

    # O_EXCL: even here, a temp name must never be a symlink this process was tricked into
    # writing through.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        if existing_mode is not None:
            os.fchmod(fd, existing_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise

    os.replace(tmp_path, path)
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
