"""Tests for the deterministic parser (skillabel.parse)."""

from __future__ import annotations

from pathlib import Path

from skillabel.parse import find_skills, parse_skill


def test_parse_frontmatter_and_body(wellformed_dir):
    parsed = parse_skill(wellformed_dir)
    assert parsed.frontmatter["name"] == "md-to-docx"
    assert parsed.frontmatter["description"].startswith("Convert Markdown files")
    # Body begins right after the closing '---' (line 5 in the fixture).
    assert parsed.body_start_line == 5
    assert parsed.body.lstrip().startswith("# Markdown")
    # line N == lines[N-1]; line 1 is the opening frontmatter fence.
    assert parsed.lines[0] == "---"
    assert parsed.lines[8].startswith("- Fires on:")


def test_parse_accepts_skill_md_file_directly(wellformed_dir):
    parsed = parse_skill(wellformed_dir / "SKILL.md")
    assert parsed.frontmatter["name"] == "md-to-docx"
    assert Path(parsed.skill_md_path).name == "SKILL.md"


def test_resource_discovery_excludes_skill_md(wellformed_dir):
    parsed = parse_skill(wellformed_dir)
    assert parsed.resources == ["reference.md", "scripts/convert.py"]
    assert "SKILL.md" not in parsed.resources
    # Relative POSIX paths, even on Windows.
    assert all("\\" not in r for r in parsed.resources)


def test_missing_frontmatter_tolerated(tmp_path):
    skill = tmp_path / "bare"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Just a body\n\nNo frontmatter here.\n", encoding="utf-8")
    parsed = parse_skill(skill)
    assert parsed.frontmatter == {}
    assert parsed.body_start_line == 1
    assert parsed.body.startswith("# Just a body")


def test_crlf_is_stripped(tmp_path):
    skill = tmp_path / "crlf"
    skill.mkdir()
    (skill / "SKILL.md").write_bytes(b"---\r\nname: x\r\n---\r\n\r\nBody line\r\n")
    parsed = parse_skill(skill)
    assert parsed.frontmatter == {"name": "x"}
    assert all("\r" not in line for line in parsed.lines)


def test_find_skills_finds_all_four_fixtures(fixtures_dir):
    found = find_skills(fixtures_dir)
    names = {p.name for p in found}
    assert {"wellformed", "lintfarm", "scary", "wolf"} <= names
    assert len(found) == 4  # tests/fixtures/expected has no SKILL.md
