"""B2: `auto`, and only `auto`, follows a link that sits directly inside one
of your own home skill folders. A link anywhere else is still not followed.
Every skipped skill folder is named and counted, with the command that would
label it. Built in a temporary folder with a fake home; nothing real is read.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from skillabel.cli import _plain_path, app, label_command
from test_hostile import dir_link, home, runner, skill  # noqa: F401


def _auto(tmp_path, cwd: Path):
    out = tmp_path / "auto.html"
    old = os.getcwd()
    os.chdir(cwd)
    try:
        r = runner.invoke(app, ["auto", "--no-open", "--no-usage", "--out", str(out)])
    finally:
        os.chdir(old)
    return r, (out.read_text(encoding="utf-8") if out.exists() else "")


@pytest.fixture
def setup(tmp_path, home, monkeypatch):
    monkeypatch.setattr("skillabel.discover.Path.cwd", lambda: tmp_path / "proj")
    skill(home / ".claude" / "skills" / "plain", "plain")
    skill(home / "elsewhere" / "linked", "linked", 'Run `bash -c "echo hi"`')
    dir_link(home / ".claude" / "skills" / "linked", home / "elsewhere" / "linked")
    proj = tmp_path / "proj"
    skill(proj / ".claude" / "skills" / "projplain", "projplain")
    skill(tmp_path / "outside" / "projlinked", "projlinked", "FAKE-PROJECT-LINK-TARGET-55")
    dir_link(proj / ".claude" / "skills" / "projlinked", tmp_path / "outside" / "projlinked")
    return proj


def test_auto_labels_a_linked_skill_in_a_home_skill_folder(tmp_path, home, setup):
    r, html = _auto(tmp_path, setup)
    assert r.exit_code == 0, r.output
    card = html.split("<h3>linked</h3>")[1].split("</details>")[0]
    assert "Not checked" not in card and 'data-kind="runs_shell"' in card and "badge present" in card


def test_the_same_link_in_a_project_folder_is_still_not_followed(tmp_path, home, setup):
    r, html = _auto(tmp_path, setup)
    card = html.split("<h3>projlinked</h3>")[1].split("</details>")[0]
    assert "Not checked: a link or junction" in card
    assert "FAKE-PROJECT-LINK-TARGET-55" not in html + r.output


def test_the_summary_names_and_counts_every_skipped_folder_with_a_command(tmp_path, home, setup):
    r, html = _auto(tmp_path, setup)
    assert "1 skill folder(s) not checked" in r.output
    link = setup / ".claude" / "skills" / "projlinked"
    cmd = label_command(link)
    assert cmd and cmd.startswith(f'skillabel scan "{link}" --out "{home}')
    assert "skillabel-reports" in cmd and "skillabel report" in cmd
    flat = " ".join(r.output.split())
    assert " ".join(cmd.split()) in flat
    assert "only if you know where the link points" in r.output
    assert "skillabel scan" in html.split("<h3>projlinked</h3>")[1].split("</details>")[0]


def test_scan_of_a_home_skill_folder_still_does_not_follow_the_link(tmp_path, home, setup):
    root = home / ".claude" / "skills"
    out = tmp_path / "scan-out"
    assert runner.invoke(app, ["scan", str(root), "--out", str(out)]).exit_code == 0
    labels = [json.loads(f.read_text(encoding="utf-8")) for f in (out / ".skillabel").glob("*.json")]
    linked = next(lb for lb in labels if lb["name"] == "linked")
    assert linked["risk"] == [] and linked["content_hash"] is None


@pytest.mark.parametrize("real_first", [True, False])
def test_a_skill_linked_from_one_home_folder_to_another_is_labelled_once(tmp_path, home, monkeypatch, real_first):
    monkeypatch.setattr("skillabel.discover.Path.cwd", lambda: tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    real_root, link_root = (".agents", ".claude") if real_first else (".claude", ".agents")
    skill(home / real_root / "skills" / "x", "x")
    dir_link(home / link_root / "skills" / "x", home / real_root / "skills" / "x")
    r, html = _auto(tmp_path, tmp_path / "proj")
    assert r.exit_code == 0, r.output
    assert html.count("<h3>x</h3>") == 1
    assert "point to a skill already labelled" in " ".join(r.output.split())


def test_extra_roots_and_plugins_never_follow(tmp_path, home, monkeypatch):
    monkeypatch.setattr("skillabel.discover.Path.cwd", lambda: tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    skill(tmp_path / "t" / "target", "target", "FAKE-EXTRA-ROOT-TARGET-66")
    extra = tmp_path / "team"
    skill(extra / "ok", "ok")
    dir_link(extra / "lnk", tmp_path / "t" / "target")
    (home / ".skillabel.toml").write_text(f'[discovery]\nextra_roots = ["{extra.as_posix()}"]\n', encoding="utf-8")
    plug = home / ".claude" / "plugins" / "p" / "skills"
    skill(plug / "pok", "pok")
    dir_link(plug / "plnk", tmp_path / "t" / "target")
    r, html = _auto(tmp_path, tmp_path / "proj")
    assert r.exit_code == 0, r.output
    assert "FAKE-EXTRA-ROOT-TARGET-66" not in html + r.output
    assert html.count("Not checked: a link or junction") == 2


def test_a_project_root_that_resolves_to_a_home_root_does_not_follow(tmp_path, home, monkeypatch):
    """The home roots are matched as written, never by where they lead."""
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    skill(tmp_path / "t" / "target", "target", "FAKE-RESOLVED-ROOT-77")
    skill(home / "other" / "o", "o")
    (home / ".claude" / "skills").mkdir(parents=True)
    dir_link(home / ".claude" / "skills" / "o", home / "other" / "o")
    dir_link(proj / ".claude" / "skills", home / ".claude" / "skills")  # project root -> home root
    from skillabel.cli import _scan_roots

    results = _scan_roots([proj / ".claude" / "skills"], ai=False, model="m", link_commands=True,
                          follow_roots={home / ".claude" / "skills"})
    (lb,) = [label for _r, _p, label in results]
    assert lb.risk == [] and lb.content_hash is None


@pytest.mark.parametrize("bad", ['x"; rm -rf ~; "', "x$(echo pwned)", "x`id`", "x;y", "x&y", "x%PATH%",
                                 "x'y", "x^y"])
def test_no_command_is_printed_for_a_path_a_shell_could_run(tmp_path, home, bad):
    if sys.platform == "win32" and any(c in bad for c in '"<>|?*'):
        pytest.skip("Windows file names cannot hold this character")
    link = tmp_path / "skills" / bad
    assert label_command(link) is None


def test_plain_paths_pass_and_a_trailing_backslash_does_not():
    assert _plain_path("C:\\Users\\me\\.claude\\skills\\tidy-text_2")
    assert _plain_path("/home/me/.claude/skills/tidy text")
    assert not _plain_path("C:\\Users\\me\\")
    assert not _plain_path("/x:y")
    assert _plain_path("/home/me/skills/caf\u00e9")


def test_a_hostile_link_name_gets_no_command_in_the_summary(tmp_path, home, monkeypatch):
    monkeypatch.setattr("skillabel.discover.Path.cwd", lambda: tmp_path / "proj")
    proj = tmp_path / "proj"
    skill(proj / ".claude" / "skills" / "ok", "ok")
    skill(tmp_path / "t" / "z", "z")
    dir_link(proj / ".claude" / "skills" / "x;calc", tmp_path / "t" / "z")
    r, html = _auto(tmp_path, proj)
    assert "label it by hand" in r.output and "x;calc\" --out" not in r.output
