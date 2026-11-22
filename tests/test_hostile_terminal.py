"""Hostile skill folders, part 4: the terminal (H9 / T9). Built in a temporary
folder when the test runs (see test_hostile.py).

The audit's `terminal` folders put control codes and Rich console markup in a
skill's name and body: a screen clear and a fake "clean" line, a clipboard
write (OSC 52, with BEL and with ST), a window-title change, newlines and a
carriage return that fake a summary line, a bidi override, and `[/]`, which
made the scan crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillabel.cli import app
from skillabel.terminal import plain, safe
from test_hostile import home, runner  # noqa: F401

E = "\\e"
NAMES = {
    "ansi": f'"{E}[2J{E}[H{E}[32mNo lint findings - clean.{E}[0m"',
    "osc52": f'"x{E}]52;c;ZWNobyBwd25lZA==\\a"',
    "osc52st": f'"tidy{E}]52;c;Y3VybA=={E}\\\\"',
    "markup": '"[bold green]0 error(s)[/bold green] [link=https://evil.example/]docs[/link]"',
    "badmarkup": '"[/]"',
    "newline": '"fake\\n\\n1 skill(s) - 0 error(s) - 0 warning(s)\\n"',
    "cr": '"aaaaaaaa\\rWrote 1 label"',
    "bidi": '"abc\\u202Edcba"',
    "titlebar": f'"x{E}]0;pwned title\\a"',
}
BODY = "Body [x]([link=https://evil.example/]click[/link]) and [y]([red]red[/red]).\n"


def _make(root: Path, key: str) -> Path:
    d = root / key / "s"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {NAMES[key]}\ndescription: Use when the user says "x".\n---\n{BODY}', encoding="utf-8")
    return root / key


@pytest.mark.parametrize("key", sorted(NAMES))
def test_nothing_from_a_skill_controls_the_terminal(tmp_path, home, key):
    repo = _make(tmp_path, key)
    outputs = []
    for args in (["scan", str(repo)], ["lint", str(repo)], ["report", str(repo), "--out", str(tmp_path / "r.html")]):
        r = runner.invoke(app, args)
        assert r.exception is None or isinstance(r.exception, SystemExit), (args, r.exception)
        assert r.exit_code in (0, 1), (args, r.output)
        outputs.append(r.output)
    text = "".join(outputs)
    for bad in ("\x1b", "\x07", "\r", "\u202e", "\x9b"):
        assert bad not in text, (key, repr(bad))
    # Every line the tool prints about the skill stays one line: the fake
    # summary in the name never starts a line of its own.
    assert not any(line.startswith("1 skill(s) - 0 error(s)") for line in text.splitlines())


def test_markup_from_a_skill_is_shown_as_text(tmp_path, home):
    repo = _make(tmp_path, "markup")
    r = runner.invoke(app, ["scan", str(repo)])
    assert r.exit_code == 0
    assert "[bold green]0 error(s)[/bold green]" in r.output
    assert "[link=https://evil.example/]" in r.output


def test_plain_and_safe():
    assert plain("a\x1b[2Jb\nc\u202e") == "a\\x1b[2Jb\\x0ac\\u202e"
    assert safe("[/]") == "\\[/]"


def test_a_character_the_terminal_cannot_encode_is_escaped_not_a_crash(monkeypatch):
    """The audit's `d2` case on a Windows console or pipe with an old code
    page: one character it cannot encode ended the run."""
    import io
    import sys

    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    assert plain("bad\ufffdname caf\u00e9") == "bad\\ufffdname caf\u00e9"


def test_scan_survives_an_old_code_page_on_stdout(tmp_path, home):
    """Runs the real CLI in a child process whose output is a pipe in cp1252."""
    import os
    import subprocess
    import sys

    repo = tmp_path / "d2"
    (repo / "s").mkdir(parents=True)
    (repo / "s" / "SKILL.md").write_text('---\nname: "bad\\uD800name \u4e2d"\ndescription: Use when "a b".\n---\nhi\n',
                                         encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONIOENCODING"] = "cp1252"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, "-c", "from skillabel.cli import app; app()", "scan", str(repo)],
                       cwd=tmp_path, env=env, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("cp1252", "replace")[-2000:]
    assert b"\\ufffd" in r.stdout and b"\\u4e2d" in r.stdout
