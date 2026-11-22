"""Usage layer tests (v0.3) — collection from local session logs + report overlay.

Fixtures under ``fixtures/usage`` are hand-crafted JSONL covering both harnesses
and every edge the matching rule must get right:

  Claude (``claude/projects/proj-alpha/session1.jsonl``)
    - two ``Skill`` tool calls for a known skill (``tdd``) on different dates
    - a ``<command-name>/tdd</command-name>`` slash marker (known -> counts)
    - a plugin-prefixed slash ``/plugin:code-review`` (prefix stripped -> counts)
    - built-ins ``/clear`` and ``/rate-limit-options`` (must NOT count)
    - a ``Skill`` call for an unknown skill (``ghost-skill``) -> unmatched

  Codex (``codex/sessions/2026/07/**``)
    - sessions where the skills catalog is listed in the user message /
      ``user_instructions`` (must NOT count — that is every session's prompt)
    - ``shell_command`` function-calls that actually read a SKILL.md, incl. the
      same skill twice in one session (per-session dedup) and a Windows
      backslash-escaped path (``pdf-ocr``)
    - a read of an unknown skill (``ghost-codex``) -> unmatched

Roots are injected, so these never touch the real ``~/.claude`` / ``~/.codex``.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from skillabel.cli import app
from skillabel.report import render
from skillabel.schema import ContextCost, SkillLabel
from skillabel.usage import collect_usage

FIXTURES = Path(__file__).parent / "fixtures" / "usage"
CLAUDE_ROOT = FIXTURES / "claude" / "projects"
CODEX_ROOT = FIXTURES / "codex" / "sessions"
KNOWN = {"tdd", "code-review", "documents", "pdf-ocr", "never-used-fixture"}

runner = CliRunner()


def _collect():
    return collect_usage(claude_root=CLAUDE_ROOT, codex_root=CODEX_ROOT, known_slugs=KNOWN)


# --- collection ------------------------------------------------------------


def test_merges_both_harnesses_with_split_and_total():
    u = _collect()
    tdd = u.skills["tdd"]
    # 2 Skill tool calls + 1 /tdd slash = 3 claude; sessionA + sessionB = 2 codex.
    assert tdd.by_harness == {"claude": 3, "codex": 2}
    assert tdd.total_uses == 5


def test_last_used_is_the_most_recent_date_across_harnesses():
    u = _collect()
    # Latest signal is a Codex read on 2026-07-04, beating the 2026-07-03 Claude call.
    assert u.skills["tdd"].last_used == "2026-07-04"
    assert u.skills["pdf-ocr"].last_used == "2026-07-05"


def test_plugin_prefixed_slash_command_maps_to_known_slug():
    # "/plugin:code-review" -> strip leading slash + namespace -> "code-review".
    assert _collect().skills["code-review"].by_harness == {"claude": 1, "codex": 0}


def test_builtin_slash_commands_never_count_and_are_not_unmatched():
    u = _collect()
    for builtin in ("clear", "rate-limit-options", "model"):
        assert builtin not in u.skills
        assert builtin not in u.unmatched


def test_codex_catalog_listing_does_not_inflate_usage():
    # sessionA's user message + user_instructions both reference tdd/documents
    # SKILL.md, but only the two shell reads (tdd once/session, documents once)
    # count. If the catalog leaked in, these numbers would be larger.
    u = _collect()
    assert u.skills["tdd"].by_harness["codex"] == 2
    assert u.skills["documents"].by_harness["codex"] == 1


def test_codex_per_session_dedup():
    # sessionA reads tdd's SKILL.md twice -> one Codex use for that session.
    assert _collect().skills["tdd"].by_harness["codex"] == 2  # sessionA(1) + sessionB(1)


def test_windows_backslash_path_is_matched():
    # sessionC reads C:\proj\skills\pdf-ocr\SKILL.md via an escaped-backslash path.
    assert "pdf-ocr" in _collect().skills


def test_unknown_skills_go_to_unmatched_not_cards():
    u = _collect()
    assert set(u.unmatched) == {"ghost-skill", "ghost-codex"}
    assert u.unmatched_count() == 2
    assert "ghost-skill" not in u.skills
    assert "ghost-codex" not in u.skills


def test_never_used_known_skill_is_absent_from_usage():
    # A known slug never seen in any log simply has no usage record; the report
    # reads that absence as "never used".
    assert "never-used-fixture" not in _collect().skills


def test_missing_roots_are_handled_gracefully(tmp_path):
    u = collect_usage(claude_root=tmp_path / "nope", codex_root=tmp_path / "gone", known_slugs=KNOWN)
    assert u.skills == {}
    assert u.unmatched == {}


def test_usage_index_json_round_trips(tmp_path):
    from skillabel.usage import UsageIndex

    u = _collect()
    path = tmp_path / "usage.json"
    u.write(path)
    reloaded = UsageIndex.load(path)
    assert reloaded.skills["tdd"].total_uses == 5
    assert reloaded.unmatched == u.unmatched


# --- report overlay --------------------------------------------------------


def _label(name: str) -> SkillLabel:
    return SkillLabel(
        name=name,
        source_path=f"/x/.claude/skills/{name}",
        source="/x/.claude/skills",
        content_hash="h" + name,
        description="A skill. Use when asked.",
        cost=ContextCost(description_tokens=20, body_tokens=100),
    )


def test_report_renders_usage_markup_and_never_used_chip():
    labels = [_label(s) for s in ("tdd", "code-review", "documents", "pdf-ocr", "never-used-fixture")]
    html = render(labels, usage=_collect())

    # Per-card usage line and its harness split for tdd.
    assert 'class="usage-line"' in html
    assert "claude 3 / codex 2" in html
    # The loud "never used" signal + its filter chip.
    assert 'class="usage-never"' in html
    assert "never used" in html
    assert 'id="never-used-chip"' in html
    # Sort-by-usage option and card data attributes wiring the controls.
    assert 'value="usage"' in html
    assert "data-usage=" in html
    assert "data-used=" in html
    # Dashboard tiles: most-used + never-used count + harness split + unmatched footnote.
    assert "Most used" in html
    assert "never used" in html
    assert "unmatched" in html


def test_report_without_usage_is_unchanged():
    html = render([_label("tdd")])  # usage defaults to None
    assert 'class="usage-line"' not in html
    assert 'id="never-used-chip"' not in html
    assert 'value="usage"' not in html


# --- CLI end-to-end (hermetic: fake HOME with planted logs) ----------------

_SKILL_MD = """\
---
name: {name}
description: A planted skill. Use when the usage test runs.
---

# {name}

When to use: exercised by the usage CLI test.
"""

_CLAUDE_LOG = (
    '{"type":"assistant","timestamp":"2026-07-04T10:00:00.000Z","message":{"role":"assistant",'
    '"content":[{"type":"tool_use","id":"t1","name":"Skill","input":{"skill":"tdd"}}]}}\n'
)
_CODEX_LOG = (
    '{"type":"response_item","timestamp":"2026-07-04T08:00:00.000Z","payload":{"type":"function_call",'
    '"name":"shell_command","arguments":"{\\"command\\":\\"Get-Content -Path skills/tdd/SKILL.md\\"}",'
    '"call_id":"c1"}}\n'
)


def _plant_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_SKILL_MD.format(name=name), encoding="utf-8")


def _fake_home_with_logs(monkeypatch, home: Path, cwd: Path) -> None:
    # One used skill (tdd) and one never-used skill (unused-skill).
    _plant_skill(home / ".claude" / "skills", "tdd")
    _plant_skill(home / ".claude" / "skills", "unused-skill")
    proj = home / ".claude" / "projects" / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "s.jsonl").write_text(_CLAUDE_LOG, encoding="utf-8")
    sess = home / ".codex" / "sessions" / "2026" / "07" / "04"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "rollout.jsonl").write_text(_CODEX_LOG, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("skillabel.discover.Path.cwd", lambda: cwd)
    monkeypatch.setattr("skillabel.cli.Path.cwd", lambda: cwd)


def test_auto_integrates_usage_summary_and_report(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    _fake_home_with_logs(monkeypatch, home, cwd)

    out = tmp_path / "report.html"
    result = runner.invoke(app, ["auto", "--no-open", "--out", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr

    err = result.stderr
    assert "Usage" in err
    assert "tdd (2)" in err  # claude 1 + codex 1
    assert "1 skill(s) never used" in err  # unused-skill
    assert "Codex:" in err and "detected" in err

    html = out.read_text(encoding="utf-8")
    assert 'class="usage-line"' in html
    assert 'id="never-used-chip"' in html
    # Sidecar written next to the report.
    assert (out.parent / "usage.json").is_file()


def test_auto_no_usage_flag_skips_collection(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    _fake_home_with_logs(monkeypatch, home, cwd)

    out = tmp_path / "report.html"
    result = runner.invoke(app, ["auto", "--no-open", "--no-usage", "--out", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "never used" not in result.stderr
    html = out.read_text(encoding="utf-8")
    assert 'id="never-used-chip"' not in html
    assert not (out.parent / "usage.json").is_file()


def test_report_writes_usage_sidecar_to_home_not_the_scanned_folder(tmp_path, monkeypatch):
    """`report <path>` on a machine with session logs must not leave this
    machine's usage (including skills that are not in <path>) under <path>."""
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    _fake_home_with_logs(monkeypatch, home, cwd)
    repo = tmp_path / "repo"
    _plant_skill(repo, "tdd")  # the only skill in the scanned folder

    assert runner.invoke(app, ["scan", str(repo)]).exit_code == 0
    result = runner.invoke(app, ["report", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr

    assert not list(repo.rglob("usage.json"))
    sidecar = home / "skillabel-reports" / "usage.json"
    assert sidecar.is_file()
    assert "tdd" in sidecar.read_text(encoding="utf-8")


def test_report_still_renders_when_the_home_sidecar_cannot_be_written(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    _fake_home_with_logs(monkeypatch, home, cwd)
    (home / "skillabel-reports").write_text("a file where the folder should be", encoding="utf-8")
    repo = tmp_path / "repo"
    _plant_skill(repo, "tdd")
    assert runner.invoke(app, ["scan", str(repo)]).exit_code == 0
    out = tmp_path / "elsewhere" / "report.html"
    out.parent.mkdir()
    result = runner.invoke(app, ["report", str(repo), "--out", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Could not save the usage sidecar" in result.stderr
    assert 'class="usage-line"' in out.read_text(encoding="utf-8")
    assert not list(repo.rglob("usage.json"))
