"""CLI tests. The `report` path is unguarded — it needs only the renderer. The
`scan`/`lint` paths exercise the real deterministic pipeline and are guarded so
this suite stays green if that pipeline is missing.
"""

from __future__ import annotations
from pathlib import Path

import json

import pytest
from typer.testing import CliRunner

from skillabel.cli import app
from skillabel.schema import SkillLabel

runner = CliRunner()


def test_cli_imports_and_help_lists_commands():
    """Importing the CLI must not depend on sibling agents' modules."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("scan", "report", "lint"):
        assert command in result.output


def test_report_writes_html_from_prewritten_labels(tmp_path, sample_label_dict, monkeypatch):
    """UNGUARDED: pre-write a label JSON, then `report` renders the HTML. With
    no --out it goes to ~/skillabel-reports, outside the scanned folder."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    skillabel_dir = tmp_path / ".skillabel"
    skillabel_dir.mkdir()
    # Round-trip through the schema so the artifact matches what `scan` writes.
    label = SkillLabel.model_validate(sample_label_dict)
    (skillabel_dir / "wellformed.json").write_text(
        label.model_dump_json(indent=2), encoding="utf-8"
    )

    result = runner.invoke(app, ["report", str(tmp_path)])
    assert result.exit_code == 0, result.output

    out_file = home / "skillabel-reports" / "skillabel-report.html"
    assert out_file.exists()
    assert not (tmp_path / "skillabel-report.html").exists()
    html = out_file.read_text(encoding="utf-8")
    assert "<!doctype html>" in html.lower()
    for panel in ("Fires when", "Needs", "Does", "Produces", "Touches", "Costs"):
        assert panel in html
    assert label.name in html


def test_report_custom_out_and_title(tmp_path, sample_label_dict):
    """UNGUARDED: `--out` and `--title` honored; empty labels still render."""
    (tmp_path / ".skillabel").mkdir()
    out_file = tmp_path / "audit.html"
    result = runner.invoke(
        app, ["report", str(tmp_path), "--out", str(out_file), "--title", "Audit X"]
    )
    assert result.exit_code == 0, result.output
    assert out_file.exists()
    assert "<title>Audit X</title>" in out_file.read_text(encoding="utf-8")


# --- Guarded on the deterministic pipeline ------------------------------------


def test_lint_fixtures_exits_1_on_errors(fixtures_dir):
    """lintfarm carries error-severity lints, so `lint` must exit 1."""
    pytest.importorskip("skillabel.deterministic")
    pytest.importorskip("skillabel.parse")

    result = runner.invoke(app, ["lint", str(fixtures_dir)])
    assert result.exit_code == 1, result.output


def test_lint_clean_skill_exits_0(wellformed_dir):
    """The well-formed fixture fires zero lints -> exit 0."""
    pytest.importorskip("skillabel.deterministic")
    pytest.importorskip("skillabel.parse")

    result = runner.invoke(app, ["lint", str(wellformed_dir)])
    assert result.exit_code == 0, result.output


def test_scan_writes_json_artifacts(tmp_path, wellformed_dir):
    """`scan --out <tmp>` writes <tmp>/.skillabel/<slug>.json."""
    pytest.importorskip("skillabel.deterministic")
    pytest.importorskip("skillabel.parse")

    result = runner.invoke(app, ["scan", str(wellformed_dir), "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output

    artifacts = list((tmp_path / ".skillabel").glob("*.json"))
    assert artifacts, "no JSON artifacts written"
    # Each artifact must validate against the label schema.
    for artifact in artifacts:
        SkillLabel.model_validate(json.loads(artifact.read_text(encoding="utf-8")))


def test_scan_ai_without_keys_degrades_keyless(tmp_path, wellformed_dir, monkeypatch):
    """`--ai` with no provider key warns once and still writes deterministic labels."""
    pytest.importorskip("skillabel.deterministic")
    pytest.importorskip("skillabel.parse")
    pytest.importorskip("skillabel.ai")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SKILLABEL_AI_PROVIDER", "auto")

    result = runner.invoke(app, ["scan", str(wellformed_dir), "--ai", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert list((tmp_path / ".skillabel").glob("*.json")), "no artifacts written"
