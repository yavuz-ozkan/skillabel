"""Multi-source scan + drift detection (v0.2).

Covers: `scan` across several roots without label-file clobbering on same-slug
skills, and the report's cross-source drift vs identical-duplicate detection.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from skillabel.cli import app
from skillabel.deterministic import build_label
from skillabel.report import drift_summary, render
from skillabel.schema import SkillLabel

runner = CliRunner()

_SKILL = """\
---
name: {name}
description: {desc}
---

# {name}

When to use: {body}
"""


def _plant(root: Path, name: str, *, desc: str = "Convert things. Use when asked.", body: str = "always.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        _SKILL.format(name=name, desc=desc, body=body), encoding="utf-8"
    )
    return skill_dir


def test_scan_multiple_sources_no_filename_clobber(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _plant(root_a, "shared-skill")
    _plant(root_b, "shared-skill")
    out = tmp_path / "out"

    result = runner.invoke(app, ["scan", str(root_a), str(root_b), "--out", str(out)])
    assert result.exit_code == 0, result.output

    artifacts = sorted((out / ".skillabel").glob("*.json"))
    # Same slug under two sources -> two distinct namespaced files, no clobber.
    assert len(artifacts) == 2, [p.name for p in artifacts]

    labels = [SkillLabel.model_validate(json.loads(p.read_text(encoding="utf-8"))) for p in artifacts]
    assert all(lb.name == "shared-skill" for lb in labels)
    sources = {lb.source for lb in labels}
    assert sources == {str(root_a), str(root_b)}, sources
    # Every artifact carries a content hash for drift detection.
    assert all(lb.content_hash for lb in labels)


def test_single_path_scan_still_works(tmp_path):
    root = tmp_path / "solo"
    _plant(root, "only-skill")
    out = tmp_path / "out"

    result = runner.invoke(app, ["scan", str(root), "--out", str(out)])
    assert result.exit_code == 0, result.output
    artifacts = list((out / ".skillabel").glob("*.json"))
    assert len(artifacts) == 1
    label = SkillLabel.model_validate(json.loads(artifacts[0].read_text(encoding="utf-8")))
    assert label.source == str(root)


def test_drift_detected_between_diverging_sources(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _plant(root_a, "write-a-skill", body="the good, complete version with steps.")
    _plant(root_b, "write-a-skill", body="broken.")  # diverged body -> different hash

    label_a = build_label(root_a / "write-a-skill", source=str(root_a))
    label_b = build_label(root_b / "write-a-skill", source=str(root_b))
    assert label_a.content_hash != label_b.content_hash

    summary = drift_summary([label_a, label_b])
    assert summary["drift_groups"] == 1
    assert summary["duplicate_groups"] == 0

    html = render([label_a, label_b])
    assert "DRIFT" in html
    assert "differs across sources" in html
    assert "body length" in html  # the diff summary names what differs


def test_identical_duplicate_across_sources(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _plant(root_a, "tdd")
    _plant(root_b, "tdd")  # byte-identical content

    label_a = build_label(root_a / "tdd", source=str(root_a))
    label_b = build_label(root_b / "tdd", source=str(root_b))
    assert label_a.content_hash == label_b.content_hash

    summary = drift_summary([label_a, label_b])
    assert summary["duplicate_groups"] == 1
    assert summary["drift_groups"] == 0

    html = render([label_a, label_b])
    assert "duplicate (identical)" in html
    # No per-card drift: the "differs across sources:" diff line only renders on
    # drifted cards (the word DRIFT itself also appears once in the legend).
    assert "differs across sources:" not in html


def test_same_slug_single_source_is_not_flagged(tmp_path):
    # Same slug but the SAME source is not a cross-source duplicate.
    root = tmp_path / "a"
    _plant(root, "dup")
    label = build_label(root / "dup", source=str(root))
    summary = drift_summary([label, label])
    assert summary["duplicate_groups"] == 0
    assert summary["drift_groups"] == 0
