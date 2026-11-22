"""`skillabel auto` CLI tests (v0.2 — the headline command).

Fake HOME/cwd via monkeypatch + tmp_path with planted skill roots (including a
plugin glob hit); assert one combined report is written, the stderr summary
names each root, and the no-roots case exits 1 listing the checked paths.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from skillabel.cli import app

runner = CliRunner()

_SKILL = """\
---
name: {name}
description: A planted skill. Use when the auto test runs.
---

# {name}

When to use: exercised by the auto CLI test.
"""


def _plant(root: Path, name: str, body_extra: str = "") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_SKILL.format(name=name) + body_extra, encoding="utf-8")


def _fake_home(monkeypatch, home: Path, cwd: Path) -> None:
    """Point discovery at planted tmp roots by patching home/cwd resolution."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("skillabel.discover.Path.cwd", lambda: cwd)
    monkeypatch.setattr("skillabel.cli.Path.cwd", lambda: cwd)


def test_auto_writes_combined_report_and_summarizes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    _plant(home / ".claude" / "skills", "alpha")
    _plant(home / ".agents" / "skills", "beta")
    _plant(home / ".claude" / "plugins" / "myplugin" / "skills", "gamma")
    _fake_home(monkeypatch, home, cwd)

    out = tmp_path / "report.html"
    result = runner.invoke(app, ["auto", "--no-open", "--out", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr

    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "<!doctype html>" in html.lower()
    for name in ("alpha", "beta", "gamma"):
        assert name in html

    # Plain-language stderr summary: each root + count, totals, report path.
    err = result.stderr
    assert ".claude" in err and ".agents" in err
    assert "myplugin" in err
    assert "3 skill(s) total" in err
    assert str(out) in err


def test_auto_no_roots_exits_1_listing_checked_paths(tmp_path, monkeypatch):
    home = tmp_path / "empty-home"
    cwd = tmp_path / "empty-proj"
    home.mkdir()
    cwd.mkdir()
    _fake_home(monkeypatch, home, cwd)

    result = runner.invoke(app, ["auto", "--no-open"])
    assert result.exit_code == 1, result.stdout + result.stderr
    err = result.stderr
    assert "No skill roots found" in err
    # Names exactly which paths were checked.
    assert str(home / ".claude" / "skills") in err
    assert str(home / ".agents" / "skills") in err
    assert str(cwd / ".claude" / "skills") in err


def test_auto_flags_drift_across_sources(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    # Same-named skill in two roots with diverging bodies -> drift.
    _plant(home / ".claude" / "skills", "write-a-skill", body_extra="\ncomplete version.\n")
    _plant(home / ".agents" / "skills", "write-a-skill", body_extra="\nbroken.\n")
    _fake_home(monkeypatch, home, cwd)

    out = tmp_path / "report.html"
    result = runner.invoke(app, ["auto", "--no-open", "--out", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "drifted across sources" in result.stderr
    assert "DRIFT" in out.read_text(encoding="utf-8")
