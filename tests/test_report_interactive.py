"""Interactive report + aggregate dashboard tests (v0.2).

Asserts on rendered HTML content only — the search box, sort dropdown, severity
filter chips, per-source filter, aggregate dashboard header, and drift/source
badges are all present. No browser needed.
"""

from __future__ import annotations

from skillabel.report import render
from skillabel.schema import ContextCost, Lint, RiskFlag, SkillLabel


def _label(name, *, source, body_tokens=100, desc_tokens=20, lints=None, risk=None, content_hash="h", description="Do a thing. Use when asked."):
    return SkillLabel(
        name=name,
        source_path=f"{source}/{name}",
        source=source,
        content_hash=content_hash,
        description=description,
        cost=ContextCost(description_tokens=desc_tokens, body_tokens=body_tokens, reference_files=[]),
        lints=lints or [],
        risk=risk or [],
    )


def test_search_and_sort_markup_present(sample_label_dict):
    html = render([SkillLabel.model_validate(sample_label_dict)])
    # Instant search box.
    assert 'id="q"' in html
    assert 'type="search"' in html
    # Sort dropdown with each documented sort key.
    assert 'id="sort"' in html
    for key in ('value="name"', 'value="cost"', 'value="errors"', 'value="risk"'):
        assert key in html, f"missing sort option {key}"
    # Severity filter chips.
    assert 'class="chip-filter' in html
    for sev in ('data-sev="all"', 'data-sev="errors"', 'data-sev="warnings"', 'data-sev="clean"'):
        assert sev in html, f"missing severity chip {sev}"
    # The client-side script that wires it up ships inline.
    assert "<script>" in html
    assert "addEventListener" in html


def test_cards_carry_filter_sort_data_attributes(sample_label_dict):
    html = render([SkillLabel.model_validate(sample_label_dict)])
    for attr in ("data-search=", "data-cost=", "data-errors=", "data-warnings=", "data-risk=", "data-severity="):
        assert attr in html, f"missing card attribute {attr}"
    # Collapsed-by-default cards use native <details> (keyboard accessible).
    assert "<details" in html
    assert "<summary" in html


def test_source_filter_only_when_multiple_sources():
    one = render([_label("a", source="/x/.claude/skills")])
    assert 'id="src"' not in one  # single source -> no per-source filter

    two = render([
        _label("a", source="/x/.claude/skills"),
        _label("b", source="/y/.agents/skills"),
    ])
    assert 'id="src"' in two
    # Each source is offered as an option and badged on its card.
    assert ".claude/skills" in two
    assert ".agents/skills" in two


def test_dashboard_header_aggregates():
    labels = [
        _label("heavy", source="/x/.claude/skills", body_tokens=5000,
               risk=[RiskFlag(kind="runs_shell", present=True)],
               lints=[Lint(rule="oversized-body", severity="warning", message="big")]),
        _label("light", source="/y/.agents/skills", body_tokens=10,
               risk=[RiskFlag(kind="network", present=True), RiskFlag(kind="writes_files", present=True)],
               lints=[Lint(rule="secret-ish-string", severity="error", message="leak")]),
    ]
    html = render(labels)
    # Aggregate figures + section labels.
    assert "skills" in html
    assert "sources" in html
    assert "est. tokens" in html
    # Risk-surface distribution surfaces the three tracked kinds.
    assert "runs shell" in html
    assert "network" in html
    assert "writes files" in html
    # Duplicate/drift counts.
    assert "Duplicates across sources" in html
    # Top-5 heaviest is a clickable jump to the heavy card.
    assert 'class="jump" href="#skill-heavy"' in html
    assert "Heaviest by context cost" in html


def test_drift_and_source_badges_render():
    labels = [
        _label("write-a-skill", source="/x/.claude/skills", content_hash="AAA", body_tokens=800),
        _label("write-a-skill", source="/y/.agents/skills", content_hash="BBB", body_tokens=50),
    ]
    html = render(labels)
    assert 'class="drift-badge drift"' in html
    assert "differs across sources:" in html
    assert 'class="src-badge"' in html


def test_unique_anchor_ids_for_same_slug_across_sources():
    labels = [
        _label("dup", source="/x/.claude/skills", content_hash="AAA"),
        _label("dup", source="/y/.agents/skills", content_hash="BBB"),
    ]
    html = render(labels)
    # Same slug under two sources must yield two DISTINCT anchor ids.
    assert 'id="skill-dup"' in html
    assert 'id="skill-dup-2"' in html


def test_dashboard_dedupes_identical_copies():
    """Identical copies across sources: counted once in token totals, one
    top-5 row with a multiplicity marker (regression: two real dual-installed
    skills each occupied two heaviest slots)."""
    html = render([
        _label("dupe-skill", source="/x/.claude/skills", body_tokens=300, content_hash="h1"),
        _label("dupe-skill", source="/y/.agents/skills", body_tokens=300, content_hash="h1"),
        _label("solo", source="/x/.claude/skills", body_tokens=100, content_hash="h2"),
    ])
    # one heaviest row for the duplicated skill, with the multiplicity marker
    assert html.count(">dupe-skill</a>") == 1
    assert "&times;2" in html
