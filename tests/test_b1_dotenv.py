"""B1 and decision 88: no command reads `./.env`, with or without `--ai`,
inside or outside an automated check. Built in a temporary folder; every key
is made up and `skillabel.ai.enrich` is a stub, so nothing is sent.
"""

from __future__ import annotations

import pytest

from skillabel.cli import app
from test_hostile import home, runner, skill  # noqa: F401
from test_hostile_dotenv import repoconf  # noqa: F401


def test_auto_ai_with_no_key_does_not_read_a_planted_dotenv(repoconf, home):
    repo, seen = repoconf
    skill(home / ".claude" / "skills" / "mine", "mine", "my own private skill")
    r = runner.invoke(app, ["auto", "--ai", "--no-open", "--no-usage", "--out", str(repo.parent / "r.html")])
    assert r.exit_code == 0, r.output
    assert ".env" not in r.output
    assert all(provider is None for provider, _key in seen)  # no key, so the real enrich would go keyless


@pytest.mark.parametrize("var, value", [("CI", "true"), ("GITHUB_ACTIONS", "true"), ("CI", "1"), ("CI", "TRUE"),
                                        ("CI", ""), ("CI", "0"), ("CI", "false"), ("CI", "False"), ("CI", "no")])
def test_scan_ai_never_reads_dotenv_whatever_ci_says(repoconf, monkeypatch, var, value):
    repo, seen = repoconf
    monkeypatch.setenv(var, value)
    r = runner.invoke(app, ["scan", "skills", "--ai"])
    assert r.exit_code == 0, r.output
    assert seen == [(None, None)] and ".env" not in r.output


def test_no_line_about_dotenv_when_there_is_none(repoconf, monkeypatch):
    repo, seen = repoconf
    (repo / ".env").unlink()
    r = runner.invoke(app, ["auto", "--ai", "--no-open", "--no-usage", "--out", str(repo.parent / "r.html")])
    assert ".env" not in r.output
