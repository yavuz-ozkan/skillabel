"""Checks on action.yml that keep the Action's shell steps safe to run on a
pull request whose files were written by someone else."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).resolve().parents[1] / "action.yml"


def _run_scripts() -> list[tuple[str, str]]:
    data = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    return [(s.get("name", "?"), s["run"]) for s in data["runs"]["steps"] if "run" in s]


def test_python_runs_isolated_from_the_checked_out_folder():
    """`python -m x`, `python - <<EOF` and `python -c` put the current folder
    (the PR's files) first on the import path. -I keeps it off."""
    call = re.compile(r"\bpython3?\b((?:\s+-\w+)*)\s+(-m|-c|-)(\s|$)")
    for name, script in _run_scripts():
        for line in script.splitlines():
            if line.strip().startswith("#"):
                continue
            for m in call.finditer(line):
                flags = m.group(1).split()
                assert "-I" in flags, f"step {name!r} runs python without -I: {line.strip()}"


def test_no_expression_is_pasted_into_a_run_script():
    """GitHub pastes `${{ ... }}` into the script text before bash runs it, so a
    branch name like `x$(cmd)` would run. Every value must come through env:."""
    for name, script in _run_scripts():
        code = "\n".join(l for l in script.splitlines() if not l.strip().startswith("#"))
        assert "${{" not in code, f"step {name!r} pastes an expression into its script"


def _steps() -> dict:
    data = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    return {s.get("name"): s for s in data["runs"]["steps"]}


def test_every_write_goes_to_a_fresh_work_folder_under_runner_temp():
    """A PR can plant links in the checkout. So the action writes nothing
    there: labels, the report, comment.md and changed_files.z all go to a
    fresh folder under $RUNNER_TEMP, and only labels this run wrote are in it."""
    scripts = dict(_run_scripts())
    scan = scripts["Scan and report"]
    assert 'WORK="${RUNNER_TEMP:?}/skillabel"' in scan
    fresh = scan.index('rm -rf -- "$WORK"')
    assert fresh < scan.index('mkdir -p -- "$WORK"') < scan.index("skillabel scan")
    for line in scan.splitlines():
        if line.strip().startswith("skillabel scan"):
            assert '--out "$WORK"' in line, line
        if line.strip().startswith("skillabel report"):
            assert '--out "$WORK/skillabel-report.html" -- "$WORK"' in line, line
    assert '> "$WORK/changed_files.z"' in scan
    assert "$PATH_IN/.skillabel" not in scan
    comment = scripts["Build sticky comment"]
    assert 'python -I -m skillabel.comment "$WORK" "$INPUT_MODE" "$WORK"' in comment
    post = scripts["Post sticky PR comment"]
    assert post.count('-F body=@"$WORK/comment.md"') == 2
    assert "body=@comment.md" not in post


def test_upload_takes_only_the_report_from_the_work_folder():
    upload = _steps()["Upload HTML report"]
    assert upload["with"]["path"] == "${{ runner.temp }}/skillabel/skillabel-report.html"


def test_scan_refuses_a_path_input_that_goes_through_a_link():
    scan = dict(_run_scripts())["Scan and report"]
    assert '[ -L "$CHECK" ]' in scan
    assert scan.index('[ -L "$CHECK" ]') < scan.index("skillabel scan")


def test_scan_refuses_an_absolute_path_input_or_one_with_dot_dot():
    scan = dict(_run_scripts())["Scan and report"]
    assert 'case "/$PATH_IN/" in' in scan and "//*|*/../*|" in scan
    assert scan.index('case "/$PATH_IN/" in') < scan.index("skillabel scan")


def _path_check_snippet() -> str:
    """The lines of the scan step that vet the path input, run as they are."""
    scan = dict(_run_scripts())["Scan and report"]
    start = scan.index('case "/$PATH_IN/" in')
    end = scan.index("done", scan.index('CHECK="$PATH_IN"')) + len("done")
    return 'PATH_IN="${INPUT_PATH:-.}"\n' + scan[start:end] + '\necho "OK $PATH_IN"\n'


def test_path_check_in_a_real_shell(tmp_path):
    """The path vetting runs in bash against a real link: `skills`, `skills/`
    and `skills//` (a link) are refused; a plain folder passes."""
    import os
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash on this machine")
    (tmp_path / "real" / "sub").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    try:
        os.symlink(tmp_path / "outside", tmp_path / "skills", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this machine cannot make a symlink ({type(exc).__name__})")
    script = tmp_path / "check.sh"
    script.write_text(_path_check_snippet(), encoding="utf-8", newline="\n")
    probe = subprocess.run([bash, "-c", '[ -L skills ] && echo yes'], cwd=tmp_path, capture_output=True, text=True)
    if probe.stdout.strip() != "yes":
        pytest.skip("this bash does not see the symlink Python made")

    def run(value: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, INPUT_PATH=value)
        return subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                              cwd=tmp_path, env=env, capture_output=True, text=True)

    for bad in ("skills", "skills/", "skills//", "skills/x", "/etc", "../x", "real/../skills", "a" + chr(92) + "b", "C:/x"):
        r = run(bad)
        assert r.returncode == 1 and "::error::" in r.stdout, (bad, r.stdout, r.stderr)
    for good in ("real", "real/", "real/sub", ".", ""):
        r = run(good)
        assert r.returncode == 0 and "OK " in r.stdout, (good, r.stdout, r.stderr)


def test_changed_list_keeps_folder_names_with_spaces_and_newlines(tmp_path):
    """The audit's replay: a skill folder named "evil skill" was left out of
    the comment. Runs the step's own git diff line on a real repo."""
    import os
    import shutil
    import subprocess

    from skillabel.comment import changed_skill_dirs

    bash, git = shutil.which("bash"), shutil.which("git")
    if not bash or not git:
        pytest.skip("needs bash and git")
    scan = dict(_run_scripts())["Scan and report"]
    line = next(l for l in scan.splitlines() if "git diff -z" in l).strip()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    run = lambda *a: subprocess.run(list(a), cwd=tmp_path, env=env, check=True, capture_output=True)
    run(git, "init", "-q", "-b", "main")
    (tmp_path / "skills" / "tidy").mkdir(parents=True)
    (tmp_path / "skills" / "tidy" / "SKILL.md").write_text("a", encoding="utf-8")
    run(git, "add", "-A"); run(git, "commit", "-qm", "base"); run(git, "branch", "base")
    (tmp_path / "skills" / "evil skill").mkdir()
    (tmp_path / "skills" / "evil skill" / "SKILL.md").write_text("b", encoding="utf-8")
    (tmp_path / "skills" / "low").mkdir()
    (tmp_path / "skills" / "low" / "skill.md").write_text("c", encoding="utf-8")
    run(git, "add", "-A"); run(git, "commit", "-qm", "pr")
    work = tmp_path / "work"
    work.mkdir()
    script = line.replace("origin/$BASE_REF", "base").replace(" \\", "")
    subprocess.run([bash, "-c", 'WORK="$1"; ' + script + ' || :', "_", str(work)], cwd=tmp_path, env=env, check=True)
    dirs = changed_skill_dirs((work / "changed_files.z").read_bytes())
    assert sorted(dirs) == ["skills/evil skill", "skills/low"]


# --- R4: no text from a skill can be read as a workflow command ------------


def gh_commands(log: str) -> list[str]:
    """A small copy of how GitHub's runner reads a step's log: a line whose
    text, after leading white space, starts with "::" is a command, unless
    commands were stopped by "::stop-commands::TOKEN" and not yet resumed by
    "::TOKEN::". "##[" anywhere is an old-style command. Returns what it saw."""
    seen, token = [], None
    for line in log.splitlines():  # splits on \r too
        s = line.lstrip()
        if token is not None:
            if s == f"::{token}::":
                seen.append("<resume>")
                token = None
            continue
        if s.startswith("::stop-commands::"):
            token = s[len("::stop-commands::"):].strip()
            seen.append("<stop>")
        elif s.startswith("::"):
            seen.append(s.split("::")[1].split(" ")[0])
        elif "##[" in s:
            seen.append("##[")
    return seen


def test_the_command_reader_copy_sees_a_command_at_a_line_start():
    assert gh_commands("a\n  ::notice::x\r::error::y\n") == ["notice", "error"]
    assert gh_commands("::stop-commands::t1\n::notice::x\n::t1::\n::warning::z") == ["<stop>", "<resume>", "warning"]
    assert gh_commands("a name ##[error]x\n") == ["##["]
    assert gh_commands("::stop-commands::t2\na name ##[error]x\n::t2::\n") == ["<stop>", "<resume>"]


@pytest.mark.parametrize("step", ["Scan and report", "Build sticky comment"])
def test_tool_calls_run_inside_a_stop_commands_block(step):
    script = dict(_run_scripts())[step]
    stop = script.index('echo "::stop-commands::$TOKEN"')
    assert script.index('TOKEN=$(python -I -c "import secrets; print(secrets.token_hex(16))")') < stop
    assert script.index("trap ") < stop
    first_tool = min(i for i in (script.find("skillabel scan"), script.find("python -I -m skillabel.comment")) if i >= 0)
    assert stop < first_tool


def test_a_skill_name_built_to_start_a_command_is_not_read_as_one(tmp_path):
    """The auditor's gha case, through the scan step's own script in bash, with
    a fake $RUNNER_TEMP, $GITHUB_OUTPUT and home. GitHub's runner is imitated
    by gh_commands, not run."""
    import os
    import shutil
    import subprocess
    import sys

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash on this machine")
    repo = tmp_path / "repo"
    (repo / "skills" / "x").mkdir(parents=True)
    name = "x" * 76 + " ::notice::Skillabel-all-skills-checked-no-risks-found x ##[error]old-style-command"
    (repo / "skills" / "x" / "SKILL.md").write_text(
        f'---\nname: "{name}"\ndescription: Use when "a b".\n---\nhi\n', encoding="utf-8")
    shim = tmp_path / "shim"
    shim.mkdir()
    py = Path(sys.executable).as_posix()
    src = ACTION.parent.as_posix()
    boot = "import sys; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = 'skillabel'; from skillabel.cli import app; app()"
    (shim / "skillabel").write_text(f'#!/usr/bin/env bash\nexec "{py}" -c "{boot}" "{src}" "$@"\n',
                                    encoding="utf-8", newline="\n")
    (shim / "python").write_text(f'#!/usr/bin/env bash\nexec "{py}" "$@"\n', encoding="utf-8", newline="\n")
    for f in shim.iterdir():
        f.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(dict(_run_scripts())["Scan and report"], encoding="utf-8", newline="\n")
    rt = tmp_path / "rt"
    rt.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "PYTHONPATH")}
    env.update(INPUT_PATH="skills", INPUT_MODE="lint", INPUT_MODEL="m", INPUT_STRICT="false", BASE_REF="",
               RUNNER_TEMP=str(rt), GITHUB_OUTPUT=str(tmp_path / "out.txt"), HOME=str(home), USERPROFILE=str(home),
               COLUMNS="80", PYTHONIOENCODING="utf-8", PATH=str(shim) + os.pathsep + env.get("PATH", ""))
    p = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=repo, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    log = p.stdout + p.stderr
    assert p.returncode == 0, log
    assert "\n::notice::Skillabel" in "\n" + log  # the wrapped name does start a line...
    assert gh_commands(p.stdout) == ["<stop>", "<resume>"]  # ...but only inside the stopped block
    assert gh_commands(p.stderr) == []
