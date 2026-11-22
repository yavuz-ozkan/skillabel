"""Hostile skill folders, part 5: a `.env` in the folder you scan (H8 / T8,
and D3). Built in a temporary folder when the test runs. Every key here is
made up, and `skillabel.ai.enrich` is replaced with a stub, so nothing is sent.

The audit's `repoconf` folder carries a `.env` that picks OpenRouter and gives
the attacker's own key. Skillabel never reads a `.env` (decision 88), so your
skills cannot go to the attacker's account, with or without your own key set.
"""

from __future__ import annotations

import os
import shutil

import pytest

import skillabel.ai
from skillabel.cli import app
from skillabel.providers import describe_provider
from test_hostile import file_link, home, runner, skill  # noqa: F401

NAMES = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "SKILLABEL_AI_PROVIDER", "ANTHROPIC_BASE_URL", "HTTPS_PROXY")


@pytest.fixture
def repoconf(tmp_path, home, monkeypatch):
    """A stranger's repo with a skill and a planted `.env`; cwd is that repo.
    The AI call is a stub that records which provider and key it would use."""
    for n in NAMES:
        monkeypatch.setenv(n, "placeholder")
        monkeypatch.delenv(n)
    repo = tmp_path / "repoconf"
    skill(repo / "skills" / "s1", "s1")
    (repo / ".env").write_text(
        "ANTHROPIC_BASE_URL=http://127.0.0.1:9/evil\nHTTPS_PROXY=http://127.0.0.1:9\n"
        "SKILLABEL_AI_PROVIDER=openrouter\nOPENROUTER_API_KEY=sk-or-FAKE-ATTACKER-KEY-000\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    seen = []

    async def fake_enrich(label, parsed, model):
        seen.append((describe_provider(), os.environ.get("OPENROUTER_API_KEY")))
        return label

    monkeypatch.setattr(skillabel.ai, "enrich", fake_enrich)
    return repo, seen


def test_your_key_in_the_environment_means_dotenv_is_not_read(repoconf, monkeypatch):
    repo, seen = repoconf
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-user-key")
    r = runner.invoke(app, ["scan", "skills", "--ai"])
    assert r.exit_code == 0, r.output
    assert seen == [(("anthropic", "ANTHROPIC_API_KEY"), None)]
    assert "AI: provider anthropic, key ANTHROPIC_API_KEY from your environment" in r.output
    for n in ("OPENROUTER_API_KEY", "SKILLABEL_AI_PROVIDER", "ANTHROPIC_BASE_URL", "HTTPS_PROXY"):
        assert n not in os.environ


def test_with_no_key_in_the_environment_the_planted_dotenv_is_not_read(repoconf):
    repo, seen = repoconf
    r = runner.invoke(app, ["scan", "skills", "--ai"])
    assert r.exit_code == 0, r.output
    assert seen == [(None, None)]  # no provider, no key: the real enrich goes keyless
    assert ".env" not in r.output
    for n in NAMES:
        assert n not in os.environ


def test_a_nul_byte_in_dotenv_changes_nothing(repoconf):
    repo, seen = repoconf
    (repo / ".env").write_bytes(b"ANTHROPIC_API_KEY=fake\x00nul-key\n")
    r = runner.invoke(app, ["scan", "skills", "--ai"])
    assert r.exit_code == 0, r.output
    assert seen == [(None, None)] and ".env" not in r.output


def test_a_dotenv_that_is_a_link_is_not_touched(repoconf, tmp_path):
    repo, seen = repoconf
    real = tmp_path / "elsewhere.env"
    shutil.move(repo / ".env", real)
    file_link(repo / ".env", real)
    r = runner.invoke(app, ["scan", "skills", "--ai"])
    assert r.exit_code == 0, r.output
    assert seen == [(None, None)] and ".env" not in r.output


@pytest.mark.parametrize("args", [["scan", "skills"], ["scan", "skills", "--ai"], ["lint", "skills"],
                                  ["auto", "--ai", "--no-open", "--no-usage"], ["report", "skills"]],
                         ids=lambda a: " ".join(a[:2]))
@pytest.mark.parametrize("ci", [False, True], ids=["laptop", "CI"])
def test_a_dotenv_inside_a_skill_folder_is_never_opened_or_stated(repoconf, home, monkeypatch, tmp_path, args, ci):
    """Decision 88 review: a `.env` bundled in a skill was read like any other
    bundled file. Now no `.env` is opened or stat'ed anywhere, by name alone."""
    import builtins

    repo, seen = repoconf
    for rel in ("skills/s1/.env", "skills/s1/sub/.env", "skills/s1/.ENV.local"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("OPENROUTER_API_KEY=sk-or-FAKE-BUNDLED-KEY-111\n", encoding="utf-8")
    skill(home / ".claude" / "skills" / "mine", "mine", "hi")
    (home / ".claude" / "skills" / "mine" / ".env").write_text("X=FAKE-HOME-SKILL-ENV\n", encoding="utf-8")
    (home / ".claude" / "skills" / ".env").write_text("X=FAKE-HOME-ROOT-ENV\n", encoding="utf-8")
    if ci:
        monkeypatch.setenv("CI", "true")
    if args[0] == "report":
        assert runner.invoke(app, ["scan", "skills"]).exit_code == 0
    touched = []

    def watch(real):
        def f(p, *a, **k):
            try:
                if os.path.basename(os.fsdecode(p)).lower().startswith(".env"):
                    touched.append(os.fsdecode(p))
            except TypeError:
                pass
            return real(p, *a, **k)
        return f

    for mod, name in ((builtins, "open"), (os, "open"), (os, "stat"), (os, "lstat")):
        monkeypatch.setattr(mod, name, watch(getattr(mod, name)))
    extra = ["--out", str(tmp_path / "r.html")] if args[0] in ("auto", "report") else []
    r = runner.invoke(app, args + extra)
    for mod, name in ((builtins, "open"), (os, "open"), (os, "stat"), (os, "lstat")):
        monkeypatch.setattr(mod, name, getattr(mod, name).__closure__[0].cell_contents)
    assert r.exit_code == 0, r.output
    assert touched == [], touched
    assert "FAKE-BUNDLED-KEY" not in r.output and "FAKE-HOME" not in r.output
    if args[0] == "scan":
        import json

        (lb,) = [json.loads(f.read_text(encoding="utf-8")) for f in (repo / "skills" / ".skillabel").glob("*.json")]
        assert {u["path"] for u in lb["unscanned"]} >= {".env", "sub/.env", ".ENV.local"}
        assert "env-file" in {x["rule"] for x in lb["lints"]}
        assert "FAKE-BUNDLED-KEY" not in json.dumps(lb)


def test_a_dotenv_folder_under_the_plugins_is_never_stated(home, monkeypatch, tmp_path):
    """Decision 88 review, round 2: the plugin glob stat'ed plugins/.env."""
    from skillabel.discover import discover_roots

    plugins = home / ".claude" / "plugins"
    skill(plugins / "p1" / "skills" / "a", "a")
    skill(plugins / "deep" / "x" / "skills" / "b", "b")
    (plugins / ".env" / "skills").mkdir(parents=True)
    (plugins / ".ENV.local").mkdir()
    (plugins / "p2").mkdir()
    (plugins / "p2" / ".env").write_text("X=FAKE\n", encoding="utf-8")
    touched = []
    for name in ("stat", "lstat", "scandir", "listdir"):
        real = getattr(os, name)

        def f(p=".", *a, _real=real, **k):
            if p is not None and ".env" in os.fsdecode(p).lower().replace("\\", "/").split("/")[-3:][-2:]:
                touched.append(os.fsdecode(p))
            return _real(p, *a, **k)

        monkeypatch.setattr(os, name, f)
    roots = discover_roots(home=home, cwd=tmp_path)
    monkeypatch.undo()
    assert touched == [], touched
    assert plugins / "p1" / "skills" in roots and plugins / "deep" / "x" / "skills" in roots
    assert not any(".env" in str(r).lower() for r in roots)
