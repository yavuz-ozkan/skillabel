"""B3: text from a skill cannot notify a person or link an issue in the PR
comment. GitHub is not run here: the check below is a small copy of the
patterns GitHub turns into mentions, references and links, applied to the
comment's text outside code spans (GitHub does not look inside code spans)."""

from __future__ import annotations

import re

import pytest

from skillabel import comment
from skillabel.cli import app
from test_hostile import home, runner, skill  # noqa: F401

HOSTILE = "Ask @octocat and @my-org/team about #12, GH-7, o/r#9, see https://github.com/o/r/issues/3 www.example.com"


def live_refs(md: str) -> list[str]:
    """What is left outside code spans, after backslash escapes are undone,
    that GitHub would turn into a mention, a reference or a link."""
    outside = re.sub(r"`[^`]*`", " ", md)
    outside = re.sub(r"\\(.)", r"\1", outside)
    pats = [r"(?<![A-Za-z0-9_`])@[A-Za-z0-9][A-Za-z0-9-]*", r"(?<![A-Za-z0-9_&])#\d+", r"(?i)\bGH-\d+",
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+", r"https?://\S+", r"(?i)\bwww\.\S+"]
    return [m for p in pats for m in re.findall(p, outside)]


def test_the_checker_sees_them_in_plain_text():
    assert len(live_refs(HOSTILE)) >= 7


def test_every_field_that_carries_skill_text_is_safe():
    lb = {
        "name": "ping @octocat", "source_path": "skills/ping", "description": HOSTILE,
        "trigger": {"fires_when": [HOSTILE, "@a @b @c #1 #2"]},
        "inputs": [{"name": HOSTILE, "kind": "file", "required": True}],
        "flow": [{"n": 1, "action": HOSTILE}], "outputs": [{"what": HOSTILE, "kind": "other"}],
        "report_card": [{"name": HOSTILE, "grade": "A"}], "ai_enriched": True,
        "risk": [{"kind": "runs_shell", "present": True, "evidence": {"lines": [1], "file": "@x/#3.sh"}}],
        "unscanned": [{"path": "@y.md", "reason": HOSTILE}], "lints": [],
    }
    md = comment.build_comment([lb], "full")
    assert live_refs(md) == [], live_refs(md)
    assert "`@octocat`" in md and "`#12,`" in md and "`GH-7,`" in md


def test_honest_text_reads_the_same():
    assert comment.text("Use when the user says tidy.") == "Use when the user says tidy\\."
    assert comment.text("a#b c@ d") == "a\\#b c\\@ d"


def test_a_skill_description_in_the_comment_cannot_mention_or_link(tmp_path, home):
    s = skill(tmp_path / "repo" / "ping", "ping")
    (s / "SKILL.md").write_text(f'---\nname: ping\ndescription: "Use when tidying. {HOSTILE}"\n---\nhi\n',
                                encoding="utf-8")
    work = tmp_path / "work"
    assert runner.invoke(app, ["scan", "--out", str(work), str(tmp_path / "repo")]).exit_code == 0
    (work / "changed_dirs.txt").write_text("\n", encoding="utf-8")
    assert comment.main([str(work), "lint", str(work)]) == 0
    md = (work / "comment.md").read_text(encoding="utf-8")
    assert "octocat" in md and live_refs(md) == []


@pytest.mark.parametrize("word", ["`@x`", "@x`y", "``#1``"])
def test_a_backtick_in_the_word_cannot_close_the_code_span(word):
    out = comment.text(word)
    assert out.count("`") == 2 and live_refs(out) == []


@pytest.mark.parametrize("word", ["(www.evil.example)", "*www.evil.example*", "_www.x.example_"])
def test_a_www_link_after_punctuation_is_a_code_span(word):
    """Change review round 1 (A and B): GitHub links www. after ( * _ too."""
    out = comment.text(word)
    assert out.startswith("`") and live_refs(out) == []
