"""Skill-root discovery (v0.2 — the `skillabel auto` engine).

The owner's real problem: "I don't know where my skills are installed and I
didn't know what to type." ``discover_roots`` answers it by probing a curated
list of the places agent skills actually live and returning only the roots that
exist and hold at least one skill.

Curated candidate roots (in order):
- ``<home>/.claude/skills``    — user-global Claude skills
- ``<home>/.agents/skills``    — user-global agent skills
- ``<home>/.codex/skills``     — user-global Codex skills
- ``<cwd>/.claude/skills``     — the current project's skills
- every path listed under ``[discovery] extra_roots`` in ``<home>/.skillabel.toml``
  (for skill collections that are checkouts, not installs — e.g. a team skills
  repo; same qualification rules apply)
- plugin-installed skills, via the globs ``<home>/.claude/plugins/*/skills``
  and ``<home>/.claude/plugins/**/skills`` (the deep glob subsumes the shallow
  one; overlapping matches are de-duplicated by resolved path).

A candidate qualifies only when it is an existing directory that contains at
least one ``*/SKILL.md`` (an immediate child directory holding a SKILL.md — the
standard skill layout). Order is preserved and duplicates removed by resolved
path, so a path reachable through several globs appears once.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

# Curated, in priority order. Plain roots first, then the plugin globs (the deep
# `**` glob is a superset of the shallow `*` one; dedupe handles the overlap).
_PLAIN_ROOTS: tuple[tuple[str, ...], ...] = (
    (".claude", "skills"),
    (".agents", "skills"),
    (".codex", "skills"),
)
_CONFIG_NAME = ".skillabel.toml"
_CWD_ROOT: tuple[str, ...] = (".claude", "skills")
_PLUGIN_GLOBS: tuple[str, ...] = ("plugins/*/skills", "plugins/**/skills")


def _has_skill_child(root: Path) -> bool:
    """True when *root* holds at least one ``*/SKILL.md`` (standard layout)."""
    try:
        children = list(root.iterdir())
    except OSError:
        return False
    from .parse import is_skill_file_name

    from .safeio import is_env_file

    for child in children:
        if is_env_file(child.name):  # never stat'ed (decision 88)
            continue
        if not child.is_dir():
            continue
        try:
            if any(is_skill_file_name(n) for n in os.listdir(child)):
                return True
        except OSError:
            continue
    return False


def _config_extra_roots(home: Path) -> list[Path]:
    """`[discovery] extra_roots` from ``<home>/.skillabel.toml`` — for skill
    collections that are checkouts rather than installs. Missing file, bad TOML,
    or wrong shapes are ignored: discovery must never fail because of config."""
    cfg_path = home / _CONFIG_NAME
    try:
        with open(cfg_path, "rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    raw = (cfg.get("discovery") or {}).get("extra_roots", [])
    if not isinstance(raw, list):
        return []
    return [Path(entry).expanduser() for entry in raw if isinstance(entry, str)]


def home_skill_roots(home: Path) -> list[Path]:
    """Your own three home skill folders, as written (never resolved). Only in
    these does `auto` follow a link that sits directly inside."""
    return [home.joinpath(*parts) for parts in _PLAIN_ROOTS]


def _plugin_skill_roots(plugins: Path) -> list[Path]:
    """Every folder named ``skills`` below *plugins*: first those exactly two
    levels down (``plugins/*/skills``), then all of them (``plugins/**/skills``),
    each group sorted, as the two globs gave before. A walk, not a glob, so a
    file or folder named ``.env`` is passed over by its name alone and never
    stat'ed (decision 88). Links are listed, not entered."""
    import os

    from .safeio import is_env_file

    shallow: list[Path] = []
    deep: list[Path] = []
    for top, dirs, _files in os.walk(plugins, followlinks=False):
        dirs[:] = [d for d in dirs if not is_env_file(d)]
        here = Path(top)
        if here == plugins:
            # os.walk does not enter a link; name its `skills` folder (if any)
            # so the link check below skips it out loud, as for a junction.
            for d in dirs:
                if d != "skills" and os.path.islink(here / d) and os.path.isdir(here / d / "skills"):
                    shallow.append(here / d / "skills")
                    deep.append(here / d / "skills")
        for d in dirs:
            if d == "skills":
                found = here / d
                deep.append(found)
                if here.parent == plugins:
                    shallow.append(found)
    return sorted(shallow) + sorted(deep)


def candidate_roots(home: Path, cwd: Path) -> list[Path]:
    """Every curated candidate path for *home*/*cwd*, before existence filtering."""
    candidates: list[Path] = [home.joinpath(*parts) for parts in _PLAIN_ROOTS]
    candidates.append(cwd.joinpath(*_CWD_ROOT))
    candidates.extend(_plugin_skill_roots(home / ".claude" / "plugins"))
    candidates.extend(_config_extra_roots(home))
    return candidates


def _through_link(candidate: Path, base: Path) -> bool:
    """True when *candidate*, or a folder between *base* and it, is a link or
    junction. Lexical: each part is looked at, never followed."""
    from .safeio import is_link

    try:
        rel = candidate.relative_to(base)
    except ValueError:
        return False
    p = base
    for part in rel.parts:
        p = p / part
        if is_link(p):
            return True
    return False


def discover_roots(
    home: Path | None = None, cwd: Path | None = None, skipped: list[Path] | None = None
) -> list[Path]:
    """Return the skill roots that exist and contain at least one ``*/SKILL.md``.

    Paths are de-duplicated by resolved path (first occurrence wins) so a root
    reachable through several curated entries is returned exactly once. *home*
    and *cwd* default to :meth:`Path.home` and :meth:`Path.cwd` — override them
    (e.g. a planted ``tmp_path`` in tests) for hermetic discovery.

    The project root ``<cwd>/.claude/skills`` and plugin roots under
    ``<home>/.claude/plugins`` come from files other people wrote, so such a
    root is left out when it, or a folder on the way to it, is a link or
    junction; it is added to *skipped* when a list is given. Roots under your
    home folder and ``extra_roots`` are your own and may be links.
    """
    home = home or Path.home()
    cwd = cwd or Path.cwd()

    roots: list[Path] = []
    seen: set[Path] = set()
    plugins = home / ".claude" / "plugins"
    project_root = cwd.joinpath(*_CWD_ROOT)
    at_home = os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(os.path.abspath(home))
    for candidate in candidate_roots(home, cwd):
        # Run from your home folder, the "project" root is your own ~/.claude/skills.
        project = candidate == project_root and not at_home and _through_link(candidate, cwd)
        if project or _through_link(candidate, plugins):
            if skipped is not None and candidate not in skipped:
                skipped.append(candidate)
            continue
        if not candidate.is_dir() or not _has_skill_child(candidate):
            continue
        try:
            key = candidate.resolve()
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    return roots
