"""`lint` output is never cut short (decision 119). A stranger's first run on
a narrow Windows console saw lint messages cut off and "?" in place of Rich's
"..." mark and the dash for a missing line. On a narrow terminal, and in a CI
log, each finding is one plain line; on a wide one it is a table whose
messages wrap. Built from the bundled fixtures; no AI, no network."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from skillabel import cli
from skillabel.deterministic import safe_build
from skillabel.terminal import plain

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def _messages() -> list[tuple[str, str, str]]:
    out = []
    for skill_md in sorted(FIXTURES.glob("*/SKILL.md")):
        label, _ = safe_build(skill_md.parent)
        out += [(plain(label.name), x.rule, plain(x.message)) for x in label.lints]
    return out


@pytest.mark.parametrize("width", [60, 80, 119])
def test_narrow_lint_prints_every_message_whole_on_its_own_line(monkeypatch, width):
    monkeypatch.setattr(cli.console, "_width", width)
    r = runner.invoke(cli.app, ["lint", str(FIXTURES)])
    assert r.exit_code == 1, r.output  # the samples are broken on purpose
    assert "Skillabel lints" not in r.output and "…" not in r.output
    lines = r.output.splitlines()
    for name, rule, message in _messages():
        assert any(ln.endswith(message) and f"{name}: {rule}" in ln for ln in lines), (name, rule)
    assert any(ln.startswith("error: ") for ln in lines)
    assert any(ln.startswith("warning: ") for ln in lines)


def test_a_finding_with_no_line_has_no_line_part(monkeypatch):
    """The fixtures hold findings with and without a line number."""
    monkeypatch.setattr(cli.console, "_width", 80)
    r = runner.invoke(cli.app, ["lint", str(FIXTURES)])
    assert "(line " in r.output
    assert "(line -)" not in r.output and "(line None)" not in r.output


def test_wide_lint_prints_a_table_with_nothing_cut_short(monkeypatch):
    monkeypatch.setattr(cli.console, "_width", 200)
    r = runner.invoke(cli.app, ["lint", str(FIXTURES)])
    assert r.exit_code == 1, r.output
    assert "Skillabel lints" in r.output and "…" not in r.output
    flat = " ".join(r.output.split())
    for _name, rule, _message in _messages():
        assert rule in flat


def test_clean_lint_says_so_in_plain_ascii(monkeypatch):
    monkeypatch.setattr(cli.console, "_width", 80)
    r = runner.invoke(cli.app, ["lint", str(FIXTURES / "wellformed")])
    assert r.exit_code == 0, r.output
    assert "No lint findings - clean." in r.output


def test_a_long_skill_name_is_not_cut_in_the_table(tmp_path, monkeypatch):
    """The old table cut the skill column with "..."; it now wraps."""
    from test_hostile import skill

    name = "a-very-long-skill-name-that-would-not-fit-in-a-narrow-table-column-at-all"
    skill(tmp_path / "s", name, "Hello.")
    monkeypatch.setattr(cli.console, "_width", 120)
    r = runner.invoke(cli.app, ["lint", str(tmp_path)])
    assert "Skillabel lints" in r.output and "…" not in r.output
    # Read the Skill column (the fourth cell of each row) down the table.
    # The header row's bars differ by platform, so it is skipped by its text.
    cells = [ln.split("│")[4].strip() for ln in r.output.splitlines() if ln.count("│") == 6]
    assert name in "".join(c for c in cells if c != "Skill"), cells


@pytest.mark.parametrize("width", [80, 200])
def test_hostile_text_in_a_message_is_shown_not_obeyed(tmp_path, monkeypatch, width):
    """A finding message quotes skill text (here a dead link's path). Its
    control characters and Rich markup print as text, in both layouts."""
    from test_hostile import skill

    skill(tmp_path / "s", "s", "See [the guide](\x1b[2Jfake[bold]x[/bold].md).")
    monkeypatch.setattr(cli.console, "_width", width)
    r = runner.invoke(cli.app, ["lint", str(tmp_path)])
    assert "dead-reference" in r.output, r.output
    assert "\x1b" not in r.output and "\\x1b" in r.output.replace("\n", "").replace("│", "").replace(" ", "")
    assert "[bold]" in r.output.replace("\n", "").replace("│", "").replace(" ", "")
