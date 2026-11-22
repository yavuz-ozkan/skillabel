"""Report renderer tests. Uses ONLY the sample_label_dict fixture — no
dependency on the scanner or the AI layer. Golden smoke, empty state, keyless note.
"""

from __future__ import annotations

import re

from skillabel.report import render
from skillabel.schema import SkillLabel

SIX_PANELS = ["Fires when", "Needs", "Does", "Produces", "Touches", "Costs"]


def _label(sample_label_dict) -> SkillLabel:
    return SkillLabel.model_validate(sample_label_dict)


def test_render_returns_self_contained_html(sample_label_dict):
    html = render([_label(sample_label_dict)])
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "</html>" in html


def test_render_contains_six_panel_names(sample_label_dict):
    html = render([_label(sample_label_dict)])
    for panel in SIX_PANELS:
        assert panel in html, f"missing panel: {panel}"


def test_render_has_inferred_tag_and_grade_chips(sample_label_dict):
    html = render([_label(sample_label_dict)])
    # The inferred tag concept is always present (legend + inline when applicable).
    assert "inferred" in html
    assert "tag-inferred" in html
    # Report-card grade chips render each dimension's grade.
    assert "grade" in html
    assert "g-A" in html


def test_render_has_runs_shell_risk_badge(sample_label_dict):
    html = render([_label(sample_label_dict)])
    assert 'data-kind="runs_shell"' in html
    # The wellformed sample has runs_shell present -> a present (red) badge.
    assert re.search(r'class="badge present"[^>]*data-kind="runs_shell"', html)


def test_render_no_external_resource_loads(sample_label_dict):
    """Zero network: no http(s):// in src/link-href structural resources.

    The sample's own data carries no URLs, so the only place an http(s):// could
    appear is a resource load — which must not exist (offline-openable).
    """
    html = render([_label(sample_label_dict)])
    assert "http://" not in html
    assert "https://" not in html
    assert "//cdn" not in html
    # No external resource-loading attributes at all.
    assert not re.search(r'src\s*=\s*"https?://', html)
    assert not re.search(r'<link[^>]+href\s*=\s*"https?://', html)


def test_render_evidence_chips_carry_quote_on_hover(sample_label_dict):
    html = render([_label(sample_label_dict)])
    # L9 chip from the trigger evidence, with the quote in a title attribute.
    assert ">L9<" in html
    assert "title=" in html


def test_render_empty_state(sample_label_dict):
    html = render([])
    assert "<!doctype html>" in html.lower()
    assert "No skills found" in html


def test_render_keyless_shows_placeholder_note(sample_label_dict):
    keyless = _label(sample_label_dict).model_copy(
        update={"ai_enriched": False, "report_card": None}
    )
    html = render([keyless])
    assert "--ai" in html  # the keyless placeholder note
    assert "No report card was generated in keyless mode." in html


def test_render_title_is_used(sample_label_dict):
    html = render([_label(sample_label_dict)], title="My Skills Audit")
    assert "<title>My Skills Audit</title>" in html
