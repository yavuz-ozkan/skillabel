"""Skillabel never opens, stats or reads a `.env` file (decision 88): the
provider and key come only from the process environment. A `.env` in the
folder you run skillabel from may come from a stranger's repo.

Each case runs the CLI in a fresh Python process, so the import-time behavior
is covered too. A Python audit hook and a watch on `os.stat` and `os.lstat` record any
open or stat of a file named `.env`. `skillabel.ai.enrich` is replaced with a stub, so nothing is sent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLANTED = ("ANTHROPIC_BASE_URL", "HTTPS_PROXY", "SSL_CERT_FILE")
ALLOWED = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "SKILLABEL_AI_PROVIDER")

_DRIVER = r"""
import json, os, sys
opened = []
def hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.path.basename(os.fsdecode(args[0])) == ".env":
            opened.append(True)
sys.addaudithook(hook)
_real_stat, _real_lstat = os.stat, os.lstat
def _watch(real):
    def f(path, *a, **k):
        try:
            if os.path.basename(os.fsdecode(path)) == ".env":
                opened.append(True)
        except TypeError:
            pass
        return real(path, *a, **k)
    return f
os.stat, os.lstat = _watch(_real_stat), _watch(_real_lstat)

import skillabel.ai
seen = {}
async def fake_enrich(label, parsed, model):
    seen["key"] = os.environ.get("ANTHROPIC_API_KEY")
    return label
skillabel.ai.enrich = fake_enrich

from skillabel.cli import app
try:
    app(sys.argv[1:], standalone_mode=False)
except SystemExit:
    pass
names = %r
print("RESULT " + json.dumps({
    "opened": bool(opened),
    "env": {k: os.environ.get(k) for k in names},
    "enrich_saw_key": seen.get("key"),
}))
"""


def _run(cwd: Path, home: Path, *args: str, extra_env: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in PLANTED + ALLOWED}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO))
    env.update(extra_env or {})
    code = _DRIVER % (PLANTED + ALLOWED,)
    proc = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, f"no result line\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return json.loads(line[len("RESULT "):])


@pytest.fixture
def planted(tmp_path, wellformed_dir):
    """A folder like a stranger's repo: one skill (also where `auto` looks),
    and a `.env` that tries to redirect the key, plus a fake key."""
    repo = tmp_path / "repo"
    skills = repo / ".claude" / "skills"
    shutil.copytree(wellformed_dir, skills / "demo")
    (repo / ".env").write_text(
        "ANTHROPIC_BASE_URL=http://127.0.0.1:9/\n"
        "HTTPS_PROXY=http://127.0.0.1:9/\n"
        f"SSL_CERT_FILE={tmp_path / 'no-such-ca.pem'}\n"
        "ANTHROPIC_API_KEY=fake-test-key-from-dotenv\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    return repo, home


COMMANDS = [
    ["--help"],
    ["lint", ".claude/skills"],
    ["scan", ".claude/skills"],
    ["report", ".claude/skills"],
    ["scan", ".claude/skills", "--ai"],
    ["auto", "--no-open"],
    ["auto", "--ai", "--no-open"],
]


CI_ENVS = [{}, {"CI": "true"}, {"GITHUB_ACTIONS": "true"}]


@pytest.mark.parametrize("extra", CI_ENVS, ids=["laptop", "CI", "GITHUB_ACTIONS"])
@pytest.mark.parametrize("args", COMMANDS, ids=lambda a: " ".join(a))
def test_no_command_ever_opens_or_stats_dotenv(planted, args, extra):
    repo, home = planted
    result = _run(repo, home, *args, extra_env=extra)
    assert result["opened"] is False, f"{args} touched .env"
    for name in PLANTED + ALLOWED:
        assert result["env"][name] is None, f"{name} reached the environment from .env with {args}"


def test_scan_ai_with_no_skills_does_not_open_dotenv(planted):
    repo, home = planted
    (repo / "empty").mkdir()
    result = _run(repo, home, "scan", "empty", "--ai")
    assert result["opened"] is False


def test_the_key_comes_only_from_the_real_environment(planted):
    repo, home = planted
    result = _run(repo, home, "scan", ".claude/skills", "--ai",
                  extra_env={"ANTHROPIC_API_KEY": "fake-test-key-from-real-env"})
    assert result["enrich_saw_key"] == "fake-test-key-from-real-env"
    assert result["opened"] is False


@pytest.mark.parametrize("extra", CI_ENVS, ids=["laptop", "CI", "GITHUB_ACTIONS"])
@pytest.mark.parametrize("args", [["scan", ".claude/skills", "--ai"], ["auto", "--ai", "--no-open", "--no-usage"]],
                         ids=lambda a: " ".join(a[:2]))
def test_a_planted_dotenv_key_sends_nothing_to_a_fake_server(planted, tmp_path, args, extra):
    """The real enrich runs (no stub). With no key in the environment and a
    planted `.env` key, the fake AI server on 127.0.0.1 gets 0 requests."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    repo, home = planted
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append(self.path)
            self.send_response(500)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = {k: v for k, v in os.environ.items() if k not in PLANTED + ALLOWED}
        env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO), NO_PROXY="127.0.0.1",
                   ANTHROPIC_BASE_URL=f"http://127.0.0.1:{srv.server_port}", PYTHONIOENCODING="utf-8", **extra)
        if args[0] == "auto":
            args = args + ["--out", str(tmp_path / "r.html")]
        p = subprocess.run([sys.executable, "-c", "import sys; from skillabel.cli import app; app(sys.argv[1:])", *args],
                           cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert hits == [], out
    assert "No AI provider configured" in out and ".env" not in out
