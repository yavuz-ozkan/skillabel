"""Auto-discovery tests (v0.2 `skillabel auto` engine).

Plant a fake HOME (and project cwd) via ``tmp_path``, drop skill dirs into the
curated roots — including a plugin glob hit — and assert ``discover_roots``
finds exactly the roots that exist and hold a ``*/SKILL.md``, deduped, while a
nonexistent root is silently skipped.
"""

from __future__ import annotations

from pathlib import Path

from skillabel.discover import candidate_roots, discover_roots

_SKILL_MD = """\
---
name: {name}
description: A planted skill for discovery tests. Use when testing.
---

# {name}

When to use: exercised by the discovery test suite.
"""


def _plant_skill(root: Path, name: str) -> None:
    """Create <root>/<name>/SKILL.md (the standard skill layout)."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD.format(name=name), encoding="utf-8")


def test_discovers_primary_roots_and_plugin_glob(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"

    # Two primary roots + the project root, each with a skill.
    _plant_skill(home / ".claude" / "skills", "alpha")
    _plant_skill(home / ".agents" / "skills", "beta")
    _plant_skill(project / ".claude" / "skills", "gamma")
    # A plugin-installed skill reached only through the plugin glob.
    _plant_skill(home / ".claude" / "plugins" / "myplugin" / "skills", "delta")

    roots = discover_roots(home=home, cwd=project)
    resolved = {r.resolve() for r in roots}

    assert (home / ".claude" / "skills").resolve() in resolved
    assert (home / ".agents" / "skills").resolve() in resolved
    assert (project / ".claude" / "skills").resolve() in resolved
    assert (home / ".claude" / "plugins" / "myplugin" / "skills").resolve() in resolved


def test_nonexistent_root_is_skipped(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    # Only .claude/skills exists; .agents/skills is never created.
    _plant_skill(home / ".claude" / "skills", "alpha")

    roots = discover_roots(home=home, cwd=project)
    resolved = {r.resolve() for r in roots}

    assert (home / ".claude" / "skills").resolve() in resolved
    assert (home / ".agents" / "skills").resolve() not in resolved
    assert (project / ".claude" / "skills").resolve() not in resolved


def test_empty_directory_without_skill_md_is_not_a_root(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    # The directory exists but holds no */SKILL.md -> not a qualifying root.
    (home / ".claude" / "skills" / "not-a-skill").mkdir(parents=True)

    roots = discover_roots(home=home, cwd=project)
    assert roots == []


def test_deep_plugin_glob_is_deduped_with_shallow(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    # <plugins>/myplugin/skills matches BOTH plugins/*/skills and plugins/**/skills;
    # it must be returned exactly once.
    _plant_skill(home / ".claude" / "plugins" / "myplugin" / "skills", "one")

    roots = discover_roots(home=home, cwd=project)
    target = (home / ".claude" / "plugins" / "myplugin" / "skills").resolve()
    assert [r.resolve() for r in roots].count(target) == 1


def test_no_roots_returns_empty_and_candidates_lists_checked_paths(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"

    assert discover_roots(home=home, cwd=project) == []
    # The candidate list (used by the CLI to report "checked exactly these") must
    # include the three primary roots even when nothing exists on disk.
    candidates = candidate_roots(home, project)
    assert home / ".claude" / "skills" in candidates
    assert home / ".agents" / "skills" in candidates
    assert project / ".claude" / "skills" in candidates


def test_codex_skills_root_discovered(tmp_path):
    root = tmp_path / ".codex" / "skills" / "my-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: my-skill\ndescription: x\n---\nbody", encoding="utf-8")
    found = discover_roots(home=tmp_path, cwd=tmp_path / "elsewhere")
    assert tmp_path / ".codex" / "skills" in found


def test_config_extra_roots_discovered_and_bad_config_ignored(tmp_path):
    repo = tmp_path / "team-skills" / "orchestrator"
    repo.mkdir(parents=True)
    (repo / "SKILL.md").write_text("---\nname: orchestrator\ndescription: x\n---\nbody", encoding="utf-8")
    cfg = tmp_path / ".skillabel.toml"
    cfg.write_text(
        f'[discovery]\nextra_roots = ["{(tmp_path / "team-skills").as_posix()}", 42]\n',
        encoding="utf-8",
    )
    found = discover_roots(home=tmp_path, cwd=tmp_path / "elsewhere")
    assert tmp_path / "team-skills" in found

    cfg.write_text("not [valid toml", encoding="utf-8")
    found = discover_roots(home=tmp_path, cwd=tmp_path / "elsewhere")  # must not raise
    assert tmp_path / "team-skills" not in found
