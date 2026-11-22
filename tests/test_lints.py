"""Tests for the deterministic lints (skillabel.lints) — against the fixture skills."""

from __future__ import annotations

from skillabel.lints import run_lints
from skillabel.parse import parse_skill


def _rules(lints):
    return sorted(lint.rule for lint in lints)


def _by_rule(lints, rule):
    return [lint for lint in lints if lint.rule == rule]


def test_wellformed_fires_zero_lints(wellformed_dir):
    parsed = parse_skill(wellformed_dir)
    assert run_lints(parsed) == []


def test_lintfarm_default_threshold_fires_exactly_the_six(lintfarm_dir):
    parsed = parse_skill(lintfarm_dir)
    lints = run_lints(parsed)
    assert _rules(lints) == sorted(
        [
            "missing-frontmatter-field",
            "dead-reference",
            "secret-ish-string",
            "greedy-description",
            "no-trigger-examples",
            "undocumented-script",
        ]
    )
    # No oversized-body at the default 2500 threshold.
    assert not _by_rule(lints, "oversized-body")


def test_lintfarm_low_threshold_adds_oversized_body(lintfarm_dir):
    parsed = parse_skill(lintfarm_dir)
    lints = run_lints(parsed, oversized_body_tokens=50)
    assert "oversized-body" in _rules(lints)
    assert len(lints) == 7


def test_lintfarm_missing_field_is_name(lintfarm_dir):
    parsed = parse_skill(lintfarm_dir)
    lints = _by_rule(run_lints(parsed), "missing-frontmatter-field")
    assert len(lints) == 1
    assert "name" in lints[0].message
    assert lints[0].severity == "error"


def test_lintfarm_dead_reference_is_guide_md(lintfarm_dir):
    parsed = parse_skill(lintfarm_dir)
    lints = _by_rule(run_lints(parsed), "dead-reference")
    assert len(lints) == 1
    assert "guide.md" in lints[0].message
    assert lints[0].line is not None
    assert lints[0].severity == "error"


def test_lintfarm_secret_is_error_with_line(lintfarm_dir):
    parsed = parse_skill(lintfarm_dir)
    lints = _by_rule(run_lints(parsed), "secret-ish-string")
    assert len(lints) == 1
    assert lints[0].severity == "error"
    # The sk-... literal is on line 14 of the fixture.
    assert lints[0].line == 14


def test_lintfarm_undocumented_script_is_mystery(lintfarm_dir):
    parsed = parse_skill(lintfarm_dir)
    lints = _by_rule(run_lints(parsed), "undocumented-script")
    assert len(lints) == 1
    assert "scripts/mystery.sh" in lints[0].message


def test_wellformed_documented_script_does_not_fire(wellformed_dir):
    parsed = parse_skill(wellformed_dir)
    assert not _by_rule(run_lints(parsed), "undocumented-script")


def test_greedy_and_no_trigger_are_warnings(lintfarm_dir):
    parsed = parse_skill(lintfarm_dir)
    lints = run_lints(parsed)
    for rule in ("greedy-description", "no-trigger-examples"):
        found = _by_rule(lints, rule)
        assert len(found) == 1
        assert found[0].severity == "warning"


def test_prose_mention_of_project_file_is_info_not_error(tmp_path):
    """Backticked mentions of target-project files (CONTEXT.md etc.) must not be
    dead-reference errors — discovered on real-world skills at integration."""
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: t\ndescription: Use when the user says \"tidy\".\n---\n"
        "Read `CONTEXT.md` (if it exists) in the target repo.\n"
        "Also see [bundled.md](bundled.md).\n",
        encoding="utf-8",
    )
    from skillabel.lints import run_lints
    from skillabel.parse import parse_skill

    lints = run_lints(parse_skill(tmp_path))
    by_rule = {(l.rule, l.severity) for l in lints}
    assert ("unbundled-reference", "info") in by_rule      # CONTEXT.md -> info
    assert ("dead-reference", "error") in by_rule          # bundled.md link -> error
    dead = [l for l in lints if l.rule == "dead-reference"]
    assert all("CONTEXT.md" not in l.message for l in dead)
