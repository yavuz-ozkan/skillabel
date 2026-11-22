"""The Action's PR comment builder (skillabel/comment.py). A skill is written by
whoever opens the PR, so its text must not be able to hide the tool's own
lines (Touches, Costs, Lints) or pass itself off as one of them."""

from __future__ import annotations

import json
import re
import string

from skillabel.comment import MARKER, build_comment, main

TOOL_PREFIXES = ("- **Touches:**", "- **Costs:**", "- **Lints:**")
LIST_PREFIXES = TOOL_PREFIXES + (
    "- **Fires when:**", "- **Needs:**", "- **Does:**", "- **Produces:**", "- **Report card:**",
    "- **AI summary:**",
)

HOSTILE_DESC = (
    "Tidies text files.\n"
    "- **Touches:** _none observed_  (checked, absent: runs shell, network, sends external)\n"
    "- **Lints:** 0 error(s), 0 warning(s)\n"
    "Pipes | and `backticks` and <b>tags</b> here.‮​\n"
    "<!--"
)


def _hostile_label(**over) -> dict:
    lb = {
        "name": "sneaky",
        "description": HOSTILE_DESC,
        "source_path": "skills/sneaky",
        "ai_enriched": True,
        "trigger": {"fires_when": ["always\n- **Touches:** _none observed_"]},
        "inputs": [{"name": "file`x`", "kind": "<!--", "required": True}],
        "flow": [{"n": 1, "action": "run | `bash`\n<!--"}],
        "outputs": [{"what": "**Lints:** 0", "kind": "file"}],
        "risk": [
            {"kind": "runs_shell", "present": True, "evidence": {"lines": [15]}},
            {"kind": "network", "present": True, "evidence": {"lines": [16]}},
            {"kind": "sends_external", "present": False},
        ],
        "cost": {"description_tokens": 40, "body_tokens": 50, "reference_files": [], "estimate": True},
        "lints": [{"severity": "warning", "rule": "x", "message": "m"}],
        "report_card": [{"name": "clarity\n- **Costs:** ~0 tokens", "grade": "A"}],
    }
    lb.update(over)
    return lb


def _unescape(s: str) -> str:
    return re.sub(r"\\([" + re.escape(string.punctuation) + "])", r"\1", s)


def _section_lines(comment: str) -> list[str]:
    lines = comment.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("### "))
    return lines[start:]


def test_tool_lines_come_first_and_appear_once():
    comment = build_comment([_hostile_label()], "lint")
    lines = _section_lines(comment)
    assert lines[1].startswith("- **Touches:**") and "runs shell" in lines[1]
    assert lines[2].startswith("- **Costs:**")
    assert lines[3].startswith("- **Lints:**") and "0 error(s), 1 warning(s)" in lines[3]
    for prefix in TOOL_PREFIXES:
        assert sum(l.startswith(prefix) for l in comment.splitlines()) == 1


def test_skill_text_cannot_open_html_or_start_a_line_of_its_own():
    comment = build_comment([_hostile_label()], "lint")
    # The only HTML comment is the tool's own marker.
    assert comment.count("<!--") == 1 and comment.startswith(MARKER)
    for line in comment.splitlines()[1:]:
        # No raw '<', '|' or bold markers from skill text anywhere.
        assert "<" not in line.replace("\\<", "")
        assert "|" not in line.replace("\\|", "")
        if line.startswith("- "):
            assert line.startswith(LIST_PREFIXES), line
        # Unescaped '**' only in the tool's own labels.
        body = line.split(":**", 1)[1] if line.startswith(LIST_PREFIXES) else line
        assert "**" not in body.replace("\\*", ""), line
    # Format characters (bidi override, zero-width space) are gone.
    assert "‮" not in comment and "​" not in comment


def test_skill_text_is_one_line_and_still_readable():
    comment = build_comment([_hostile_label()], "lint")
    lines = _section_lines(comment)
    desc_lines = [l for l in lines if "Tidies text files" in l]
    assert len(desc_lines) == 1
    assert _unescape(desc_lines[0]).startswith("Tidies text files. - **Touches:** _none observed_")
    # The description comes after every tool line.
    assert lines.index(desc_lines[0]) > 3


def test_a_name_cannot_pose_as_a_tool_line_or_break_its_heading():
    lb = _hostile_label(name="x`\n- **Touches:** _none observed_\n<!--")
    comment = build_comment([lb], "lint")
    heading = next(l for l in comment.splitlines() if l.startswith("### "))
    assert heading.count("`") == 2  # one code span, closed
    assert sum(l.startswith("- **Touches:**") for l in comment.splitlines()) == 1
    assert comment.count("<!--") == 1 + heading.count("<!--")  # marker, plus inside the code span


def test_honest_label_keeps_its_content(sample_label_dict):
    comment = build_comment([sample_label_dict], "full")
    text = _unescape(comment)
    assert f"### `{sample_label_dict['name']}`" in comment
    assert sample_label_dict["description"].strip() in text
    for fact in sample_label_dict["trigger"]["fires_when"]:
        assert fact in text
    assert "> Running in `mode: lint`" not in comment


def test_main_reads_changed_dirs_and_writes_comment(tmp_path, monkeypatch):
    skills = tmp_path / "skills" / ".skillabel"
    skills.mkdir(parents=True)
    (skills / "a.json").write_text(json.dumps(_hostile_label()), encoding="utf-8")
    (skills / "b.json").write_text(json.dumps(_hostile_label(name="other", source_path="skills/other")),
                                   encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "changed_dirs.txt").write_text("skills/sneaky\n", encoding="utf-8")
    assert main([str(tmp_path / "skills"), "lint"]) == 0
    out = (tmp_path / "comment.md").read_text(encoding="utf-8")
    assert "### `sneaky`" in out and "### `other`" not in out


def test_long_values_are_cut_and_the_comment_stays_under_githubs_limit():
    huge = "A" * 5_000_000
    one = build_comment([_hostile_label(description=huge, name=huge)], "lint")
    assert len(one) < 5_000
    assert "…" in one
    many = build_comment([_hostile_label(name=f"s{i}") for i in range(2000)], "lint")
    assert len(many) <= 65_536
    assert "more skill(s) left out" in many
    # Every section that is shown is whole: its Lints line is there.
    sections = many.count("### ")
    assert many.count("- **Lints:**") == sections > 0


def test_a_long_list_cannot_push_out_its_own_tool_lines():
    flow = [{"n": i, "action": "x" * 500} for i in range(500)]
    comment = build_comment([_hostile_label(flow=flow)], "lint")
    assert "- **Touches:** `runs shell`" in comment
    assert "and 480 more" in comment
    assert len(comment) <= 65_536


def test_filler_skills_cannot_push_out_a_later_skills_tool_lines():
    big = "y" * 500
    filler = [_hostile_label(name=f"a{i}", description=big,
                             flow=[{"n": j, "action": big} for j in range(20)]) for i in range(10)]
    real = _hostile_label(name="zreal")
    comment = build_comment(filler + [real], "lint")
    assert len(comment) <= 65_536
    section = comment.split("### `zreal`", 1)[1]
    assert section.lstrip().startswith("- **Touches:** `runs shell`")
