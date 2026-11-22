"""Tests for the composed deterministic label (skillabel.deterministic)."""

from __future__ import annotations

from skillabel.deterministic import build_label
from skillabel.schema import SkillLabel


def test_build_label_wellformed(wellformed_dir):
    label = build_label(wellformed_dir)
    assert label.name == "md-to-docx"
    assert label.ai_enriched is False
    assert label.report_card is None
    assert label.lints == []
    assert label.cost is not None and label.cost.body_tokens > 0
    assert label.resources == ["reference.md", "scripts/convert.py"]
    assert any(f.kind == "runs_shell" and f.present for f in label.risk)


def test_name_falls_back_to_directory_name(lintfarm_dir):
    # lintfarm frontmatter has no `name` field.
    label = build_label(lintfarm_dir)
    assert label.name == "lintfarm"


def test_build_label_round_trips(scary_dir):
    label = build_label(scary_dir)
    restored = SkillLabel.model_validate(label.model_dump())
    assert restored == label
    # Round-trip through JSON too (the on-disk artifact form).
    from_json = SkillLabel.model_validate_json(label.model_dump_json())
    assert from_json == label


def test_wolf_label_carries_regex_risk(wolf_dir):
    label = build_label(wolf_dir)
    by_kind = {f.kind: f for f in label.risk}
    assert by_kind["runs_shell"].present is True
    assert by_kind["network"].present is True
    assert label.ai_enriched is False
