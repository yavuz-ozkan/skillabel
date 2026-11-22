"""Safe file reads and writes for folders that may be written by strangers.

Skillabel is run on skill folders and repos written by other people. Such a
folder can hold a link (a symlink, or a junction on Windows) where skillabel
saves a file, so that the write lands somewhere else, such as a Python start-up
file. Every file skillabel writes goes through :func:`safe_write_text`, which
refuses a target that is a link, and a folder on the way to it that is a link.

Reads go the same way: :func:`walk_files` never enters a link or junction and
lists what it skipped, and :func:`read_capped` reads only a regular file that is
not a link, up to a size cap, so text from outside the skill never gets in.

The checks are lexical: they look at each path part with ``lstat`` and never
follow a link to see where it goes.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

# Windows reparse tags that make a path point somewhere else. Other reparse
# points (cloud placeholders, dedup files) are ordinary files to us.
_LINK_TAGS = frozenset({
    0xA000000C,  # IO_REPARSE_TAG_SYMLINK
    0xA0000003,  # IO_REPARSE_TAG_MOUNT_POINT (junction)
    0xA000001D,  # IO_REPARSE_TAG_LX_SYMLINK (a WSL symlink)
})


ENV_FILE_REASON = "a .env file, never read"


def is_env_file(name: str) -> bool:
    """A `.env` file (`.env`, `.env.local`, ... in any letter case). Skillabel
    never opens, stats or reads one, wherever it is (decision 88): it may hold
    someone's keys, and the AI settings come only from the environment."""
    n = name.lower()
    return n == ".env" or n.startswith(".env.")


class UnsafePathError(Exception):
    """A write was refused because the path goes through a link."""


def is_link(path: str | os.PathLike) -> bool:
    """True when *path* itself is a symlink or a junction. Never follows it."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


def _abs(path: str | os.PathLike) -> Path:
    return Path(os.path.abspath(path))


def _same(a: Path | str, b: Path | str) -> bool:
    """Same path text, with the letter case folded where the file system
    ignores it (Windows), so ``C:\\REPO\\SUB`` is the same folder as ``c:\\repo\\sub``."""
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _is_under(path: Path, anchor: Path) -> bool:
    try:
        return _same(os.path.commonpath([str(path), str(anchor)]), anchor)
    except ValueError:  # different drives
        return False


def _folders_to_check(target: Path, anchor: Path | None) -> list[Path]:
    """The folders on the way to *target* that must not be links: every folder
    below *anchor* (the folder being scanned, which the user named) when the
    target is inside it. A folder outside the scanned folder is the user's own
    (such as ``~/skillabel-reports``), so it may be a link."""
    parent = target.parent
    out: list[Path] = []
    if anchor is not None:
        anchor = _abs(anchor)
        if _is_under(parent, anchor):
            p = parent
            while not _same(p, anchor) and p.parent != p:
                out.append(p)
                p = p.parent
        else:
            # The same folder can be spelled another way on Windows
            # (`\\?\C:\...`, `\\localhost\C$\...`). Walk up the target's own
            # folders and ask the file system whether one of them is the
            # scanned folder; if so, the folders below it must not be links.
            out = _below_same_folder(parent, anchor)
    return out


def is_inside(path: str | os.PathLike, folder: str | os.PathLike) -> bool:
    """Is *path* inside *folder*, or *folder* itself, however either is
    spelled? First by the text (letter case folded on Windows), then by
    asking the file system whether *path* or a folder above it is *folder*."""
    p, f = _abs(path), _abs(folder)
    if _same(p, f) or _is_under(p, f):
        return True
    try:
        a = os.stat(f)
    except (OSError, ValueError):
        return False
    q = p
    while True:
        try:
            st = os.stat(q)
            if (st.st_dev, st.st_ino) == (a.st_dev, a.st_ino) and st.st_ino:
                return True
        except (OSError, ValueError):
            pass
        if q.parent == q:
            return False
        q = q.parent


def _below_same_folder(parent: Path, anchor: Path) -> list[Path]:
    """The folders from *parent* up to, not including, the first one that is
    the same folder as *anchor* (by the file system's own id). Empty when no
    folder on the way is *anchor*."""
    try:
        a = os.stat(anchor)
    except (OSError, ValueError):
        return []
    chain: list[Path] = []
    p = parent
    while True:
        try:
            st = os.stat(p)
            if (st.st_dev, st.st_ino) == (a.st_dev, a.st_ino) and st.st_ino:
                return chain
        except (OSError, ValueError):
            pass
        chain.append(p)
        if p.parent == p:
            return []
        p = p.parent


def check_write_target(target: str | os.PathLike, anchor: str | os.PathLike | None = None) -> None:
    """Raise :class:`UnsafePathError` when writing *target* could land elsewhere."""
    t = _abs(target)
    for folder in _folders_to_check(t, Path(anchor) if anchor is not None else None):
        if is_link(folder):
            raise UnsafePathError(f"{folder} is a link or junction, so skillabel will not write into it.")
    if is_link(t):
        raise UnsafePathError(f"{t} is a link or junction, so skillabel will not write to it.")
    if os.path.lexists(t) and not stat.S_ISREG(os.lstat(t).st_mode):
        raise UnsafePathError(f"{t} exists and is not a regular file, so skillabel will not write to it.")


def safe_mkdir(folder: str | os.PathLike, anchor: str | os.PathLike | None = None) -> Path:
    """Make *folder* (and its parents), refusing when it, or a folder between
    *anchor* and it, is a link."""
    f = _abs(folder)
    if anchor is None:  # the user's own folder, outside anything scanned
        f.mkdir(parents=True, exist_ok=True)
        return f
    checks = [f] + _folders_to_check(f, Path(anchor))
    for p in checks:
        if is_link(p):
            raise UnsafePathError(f"{p} is a link or junction, so skillabel will not write into it.")
    f.mkdir(parents=True, exist_ok=True)
    if is_link(f) or not f.is_dir():
        raise UnsafePathError(f"{f} is not a plain folder, so skillabel will not write into it.")
    return f


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def safe_write_text(target: str | os.PathLike, text: str, anchor: str | os.PathLike | None = None) -> Path:
    """Write *text* (UTF-8) to *target* without following a link.

    Refuses a target that is a link or not a regular file, and a folder on the
    way that is a link (see :func:`check_write_target`). The text goes to a new
    temp file in the same folder first and is then moved onto the target, so a
    link that appears at the last moment is replaced, never written through.
    """
    t = _abs(target)
    check_write_target(t, anchor)
    fd, tmp = tempfile.mkstemp(dir=str(t.parent), prefix=".skillabel-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:  # same line endings as Path.write_text
            fh.write(text)
        os.chmod(tmp, 0o666 & ~_umask())
        os.replace(tmp, t)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    return t


# --- reading -----------------------------------------------------------------


class ReadRefused(Exception):
    """A read was refused. ``reason`` is a short plain phrase for the label."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def read_capped(path: str | os.PathLike, cap: int) -> bytes:
    """Read a regular file that is not a link, at most *cap* bytes.

    Raises :class:`ReadRefused` for a link or junction, for anything that is
    not a regular file (a FIFO or a device is never opened), and for a file
    bigger than *cap*.
    """
    if is_link(path):
        raise ReadRefused("a link or junction, not followed")
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        raise ReadRefused("could not be read") from None
    if not stat.S_ISREG(st.st_mode):
        raise ReadRefused("not a regular file")
    if st.st_size > cap:
        raise ReadRefused(f"too big to read ({st.st_size} bytes, over {cap} bytes)")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ReadRefused("could not be read") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReadRefused("not a regular file")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise ReadRefused(f"too big to read (over {cap} bytes)")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        raise ReadRefused("could not be read") from None
    finally:
        os.close(fd)


class Walk:
    """What :func:`walk_files` found under a folder, as relative POSIX paths."""

    def __init__(self) -> None:
        self.files: list[str] = []
        self.folders: list[str] = []
        self.sizes: dict[str, int] = {}
        self.skipped: list[tuple[str, str]] = []  # (path, reason)


def walk_files(top: str | os.PathLike, *, max_files: int, max_depth: int, skip_names: frozenset[str] = frozenset()) -> Walk:
    """List the regular files under *top*, in name order, without entering a
    link or junction. At most *max_files* files and *max_depth* folder levels;
    what is left out is listed in ``skipped`` with the reason."""
    top = Path(top)
    w = Walk()
    stack: list[tuple[Path, str, int]] = [(top, "", 0)]
    over = 0
    while stack:
        folder, rel, depth = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (OSError, ValueError):
            w.skipped.append((rel or ".", "folder could not be read"))
            continue
        subdirs = []
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            if rel == "" and e.name in skip_names:
                continue
            if is_env_file(e.name):  # judged by its name alone: never stat'ed
                w.skipped.append((r, ENV_FILE_REASON))
                continue
            if is_link(e.path):
                w.skipped.append((r, "a link or junction, not followed"))
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except (OSError, ValueError):
                w.skipped.append((r, "could not be read"))
                continue
            if stat.S_ISDIR(st.st_mode):
                if depth + 1 > max_depth:
                    w.skipped.append((r + "/", f"folders nested deeper than {max_depth} levels, not read"))
                else:
                    subdirs.append((Path(e.path), r, depth + 1))
            elif stat.S_ISREG(st.st_mode):
                if len(w.files) >= max_files:
                    over += 1
                    continue
                w.files.append(r)
                w.sizes[r] = st.st_size
            else:
                w.skipped.append((r, "not a regular file"))
        for sd in subdirs:
            w.folders.append(sd[1])
        stack.extend(reversed(subdirs))
    if over:
        w.skipped.append(("…", f"{over} more file(s) not read (cap {max_files} files)"))
    w.files.sort()
    return w
