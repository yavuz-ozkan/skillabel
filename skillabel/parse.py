"""Deterministic SKILL.md parser.

Reads a Claude-style skill: YAML frontmatter + markdown body + bundled files.

A skill folder may be written by a stranger, so everything is read through
:mod:`skillabel.safeio`: no link or junction is followed, only regular files
are opened, and every read has a size cap. What was not read is recorded in
``ParsedSkill.unscanned`` with the reason, and a finding about it goes in
``ParsedSkill.problems``. When SKILL.md itself is not read (a link, too big,
not text), the skill has no text at all and nothing may be claimed about it.

Contract / conventions:
- Files are read as bytes then decoded UTF-8 with ``errors="replace"``; a
  leading UTF-8 BOM is dropped and every ``\\r`` is stripped (Windows hosts) so
  line numbers are stable regardless of checkout EOL style. A SKILL.md with a
  UTF-16 BOM is decoded as UTF-16. A SKILL.md that still holds NUL bytes after
  decoding is not text and is not read.
- Frontmatter is the YAML block delimited by a leading ``---`` line and the next
  ``---`` line. Missing / empty / non-mapping frontmatter tolerates to ``{}``.
  YAML aliases are refused (they can expand to huge values).
- ``lines`` is the full file split on ``\\n`` such that ``line N == lines[N-1]``.
- ``body_start_line`` is the 1-indexed line where the body begins (the line just
  after the closing ``---``; line 1 when there is no frontmatter).
- ``resources`` are the regular files under the skill directory except SKILL.md,
  as relative POSIX paths, sorted.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .safeio import ENV_FILE_REASON, ReadRefused, is_env_file, is_link, read_capped, walk_files
from .schema import Lint, ParsedSkill, UnscannedFile

SKILL_FILENAME = "SKILL.md"

# Caps. A skill bigger than these is not a skill an agent would load whole.
MAX_SKILL_MD_BYTES = 1 << 20  # 1 MiB
MAX_FRONTMATTER_CHARS = 64 << 10  # 64 KiB
MAX_RESOURCE_BYTES = 1 << 20  # each bundled file read
MAX_SKILL_READ_BYTES = 8 << 20  # all bundled files of one skill together
MAX_FILES = 2000  # bundled files per skill
MAX_SCAN_LINES = 200_000  # lines of bundled text scanned per skill
MAX_DEPTH = 12  # bundled folder levels
LONG_LINE = 10_000  # characters


def read_text_lenient(path: str | Path) -> str:
    """Read a file as UTF-8 (errors replaced), BOM-stripped, ``\\r``-stripped.
    Only a regular file that is not a link, up to the per-file cap."""
    return _decode(read_capped(path, MAX_RESOURCE_BYTES))


def _decode(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r", "")


def is_skill_file_name(name: str) -> bool:
    """The skill file in any case (`SKILL.md`, `skill.md`, ...): a file that a
    case-insensitive system would load must not go unlabelled."""
    return name.lower() == "skill.md"


def _skill_md_path(path: str | Path) -> Path:
    """Resolve *path* (a skill dir or a SKILL.md file) to the SKILL.md path."""
    p = Path(path)
    if p.is_file() and not p.is_dir():
        return p
    try:
        names = sorted(os.listdir(p))
    except OSError:
        names = []
    matches = [n for n in names if is_skill_file_name(n)]
    if SKILL_FILENAME in matches:
        return p / SKILL_FILENAME
    return p / (matches[0] if matches else SKILL_FILENAME)


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses aliases (``*name``), so a small frontmatter
    cannot expand into a huge value."""

    def compose_node(self, parent, index):  # noqa: D401 - PyYAML hook
        if self.check_event(yaml.AliasEvent):
            raise _AliasRefused()
        return super().compose_node(parent, index)


class _AliasRefused(Exception):
    pass


def _read_skill_md(skill_md: Path, problems: list[Lint]) -> tuple[str | None, str | None]:
    """Return (text, None) or (None, reason it was not read)."""
    if is_link(skill_md):
        problems.append(Lint(rule="linked-file", severity="error", line=None,
                             message=f"`{skill_md.name}` is a link or junction. Skillabel does not follow "
                             "links, so this skill was not checked."))
        return None, "a link or junction, not followed"
    try:
        data = read_capped(skill_md, MAX_SKILL_MD_BYTES)
    except ReadRefused as exc:
        problems.append(Lint(rule="could-not-read", severity="error", line=None,
                             message=f"`{skill_md.name}` was not read ({exc.reason}), so this skill was not checked."))
        return None, exc.reason
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace").replace("\r", "")
        if text.startswith("﻿"):
            text = text[1:]
        problems.append(Lint(rule="utf16-text", severity="warning", line=None,
                             message=f"`{skill_md.name}` is UTF-16 text. It was checked, but an agent may "
                             "not read it as text."))
    else:
        text = _decode(data)
        if "�" in text:
            problems.append(Lint(rule="not-utf8", severity="warning", line=None,
                                 message=f"`{skill_md.name}` holds bytes that are not valid UTF-8; they "
                                 "were replaced before checking."))
    if "\x00" in text:
        problems.append(Lint(rule="not-text", severity="error", line=None,
                             message=f"`{skill_md.name}` holds NUL bytes, so it is not plain text. "
                             "It was not checked."))
        return None, "not plain text (holds NUL bytes)"
    return text, None


def _frontmatter(lines: list[str], problems: list[Lint]) -> tuple[dict, int]:
    frontmatter: dict = {}
    body_start_line = 1
    if lines and lines[0].strip() == "---":
        close_idx: int | None = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                close_idx = i
                break
        if close_idx is not None:
            fm_text = "\n".join(lines[1:close_idx])
            loaded = None
            if len(fm_text) > MAX_FRONTMATTER_CHARS:
                problems.append(Lint(rule="frontmatter-too-big", severity="error", line=1,
                                     message=f"Frontmatter is {len(fm_text)} characters (over "
                                     f"{MAX_FRONTMATTER_CHARS}); it was not read."))
            else:
                try:
                    loaded = yaml.load(fm_text, Loader=_NoAliasLoader)  # noqa: S506 - safe loader subclass
                except _AliasRefused:
                    problems.append(Lint(rule="yaml-alias", severity="error", line=1,
                                         message="Frontmatter uses YAML aliases (`*name`). Skillabel does not "
                                         "expand them, so the frontmatter was not used."))
                except (yaml.YAMLError, RecursionError, ValueError):
                    loaded = None
            if isinstance(loaded, dict):
                frontmatter = loaded
            body_start_line = close_idx + 2  # first line after the closing '---'
    return frontmatter, body_start_line


def _walk_lints(skipped: list[tuple[str, str]], problems: list[Lint]) -> None:
    for rel, reason in skipped:
        if reason == ENV_FILE_REASON:
            problems.append(Lint(rule="env-file", severity="info", line=None,
                                 message=f"`{rel}` is a .env file. Skillabel never opens, stats or reads .env files, "
                                 "so it was not checked."))
            continue
        if "link" in reason:
            rule, msg = "linked-file", f"`{rel}` is a link or junction; skillabel did not follow it."
        elif "nested deeper" in reason:
            rule, msg = "nested-too-deep", f"`{rel}` is nested too deep; skillabel did not read below it."
        elif "more file" in reason:
            rule, msg = "too-many-files", f"The skill bundles too many files: {reason}."
        elif "not a regular file" in reason:
            rule, msg = "not-regular-file", f"`{rel}` is not a regular file (a device, pipe or socket); not read."
        else:
            rule, msg = "could-not-read", f"`{rel}` could not be read."
        problems.append(Lint(rule=rule, severity="warning", line=None, message=msg))


def _read_resources(skill_dir: Path, files: list[str], sizes: dict[str, int], problems: list[Lint],
                    unscanned: list[UnscannedFile], texts: dict[str, str], chars: dict[str, int],
                    read_texts: bool) -> None:
    budget = MAX_SKILL_READ_BYTES
    lines_left = MAX_SCAN_LINES
    for rel in files:
        size = sizes.get(rel, 0)
        chars[rel] = size  # until read: bytes stand in for characters
        if not read_texts:
            continue
        if size > MAX_RESOURCE_BYTES:
            unscanned.append(UnscannedFile(path=rel, reason=f"too big to read ({size} bytes)"))
            problems.append(Lint(rule="too-big", severity="warning", line=None,
                                 message=f"Bundled file `{rel}` is {size} bytes; skillabel did not read it."))
            continue
        if size > budget:
            unscanned.append(UnscannedFile(path=rel, reason="over the read budget for one skill"))
            continue
        try:
            data = read_capped(skill_dir / rel, MAX_RESOURCE_BYTES)
        except ReadRefused as exc:
            unscanned.append(UnscannedFile(path=rel, reason=exc.reason))
            continue
        budget -= len(data)
        text = _decode(data)
        chars[rel] = len(text)
        if "\x00" in text:
            unscanned.append(UnscannedFile(path=rel, reason="not text"))
            continue
        n = text.count("\n") + 1
        if n > lines_left:
            unscanned.append(UnscannedFile(path=rel, reason=f"over the {MAX_SCAN_LINES} line budget for one skill"))
            continue
        lines_left -= n
        texts[rel] = text


def parse_skill(path: str | Path) -> ParsedSkill:
    """Parse the skill at *path* (a directory containing SKILL.md, or the
    SKILL.md file itself) into a :class:`ParsedSkill`."""
    skill_md = _skill_md_path(path)
    skill_dir = skill_md.parent
    problems: list[Lint] = []
    unscanned: list[UnscannedFile] = []

    text, not_read = _read_skill_md(skill_md, problems)
    if text is None:
        unscanned.append(UnscannedFile(path=skill_md.name, reason=not_read or "not read"))
        lines: list[str] = []
        frontmatter: dict = {}
        body_start_line = 1
        body = ""
    else:
        lines = text.split("\n")
        frontmatter, body_start_line = _frontmatter(lines, problems)
        body = "\n".join(lines[body_start_line - 1 :]) if body_start_line <= len(lines) else ""
        long = next((i for i, ln in enumerate(lines) if len(ln) > LONG_LINE), None)
        if long is not None:
            problems.append(Lint(rule="long-line", severity="warning", line=long + 1,
                                 message=f"Line {long + 1} is {len(lines[long])} characters long. It was "
                                 "checked, but very long lines are a common place to hide text."))

    walk = walk_files(skill_dir, max_files=MAX_FILES, max_depth=MAX_DEPTH, skip_names=frozenset({skill_md.name}))
    _walk_lints(walk.skipped, problems)
    for rel, reason in walk.skipped:
        unscanned.append(UnscannedFile(path=rel, reason=reason))
    texts: dict[str, str] = {}
    chars: dict[str, int] = {}
    _read_resources(skill_dir, walk.files, walk.sizes, problems, unscanned, texts, chars, read_texts=text is not None)

    return ParsedSkill(
        path=str(skill_dir),
        skill_md_path=str(skill_md),
        frontmatter=frontmatter,
        body=body,
        body_start_line=body_start_line,
        lines=lines,
        resources=walk.files,
        problems=problems,
        unscanned=unscanned,
        folders=walk.folders,
        texts=texts,
        chars=chars,
    )


def discover_skills(root: str | Path, env_named: list[Path] | None = None) -> tuple[list[Path], list[Path]]:
    """Every folder under *root* that holds a SKILL.md (root itself counts),
    sorted, and the links or junctions that were not entered. *root* itself may
    be a link: the user named it. Nothing below it is followed. A folder named
    like a `.env` file is never entered; when *env_named* is a list, its path
    is added there."""
    root_path = Path(root)
    found: list[Path] = []
    skipped: list[Path] = []
    stack = [root_path]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (OSError, ValueError):
            continue
        subdirs = []
        has_skill = False
        for e in entries:
            if is_skill_file_name(e.name):
                has_skill = True
                continue
            if is_env_file(e.name):  # never entered or stat'ed (decision 88)
                try:
                    is_folder = e.is_dir(follow_symlinks=False)  # from the listing itself
                except OSError:
                    is_folder = False
                if is_folder and env_named is not None:
                    env_named.append(Path(e.path))
                continue
            if is_link(e.path):
                try:
                    if e.is_dir():  # a link to a folder; never entered
                        skipped.append(Path(e.path))
                except OSError:
                    skipped.append(Path(e.path))
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(Path(e.path))
            except OSError:
                continue
        if has_skill:
            found.append(folder)
        stack.extend(reversed(subdirs))
    return sorted(found, key=lambda p: p.as_posix()), skipped


def find_skills(root: str | Path) -> list[Path]:
    """Return every directory under *root* that contains a SKILL.md (root itself
    counts), sorted. Links and junctions below *root* are not entered."""
    return discover_skills(root)[0]
