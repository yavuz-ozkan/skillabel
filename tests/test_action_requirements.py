"""The GitHub Action installs from action-requirements.txt and
action-build-requirements.txt with `pip --require-hashes`. These tests fail
when those files stop matching uv.lock, so the Action installs exactly what
the lock file records.

To remake action-requirements.txt after changing uv.lock:
    uv export --frozen --no-dev --format requirements-txt --no-emit-project -o action-requirements.txt
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_requirements(path: Path) -> dict[str, tuple[str, set[str]]]:
    """name -> (version, {hashes}). Each entry must be `name==version` with hashes."""
    text = path.read_text(encoding="utf-8")
    entries: dict[str, tuple[str, set[str]]] = {}
    for block in re.split(r"\n(?=\S)", text):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        first = block.splitlines()[0]
        m = re.match(r"([A-Za-z0-9_.-]+)==([^\s;\\]+)", first)
        assert m, f"{path.name}: not pinned with ==: {first!r}"
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block))
        assert hashes, f"{path.name}: no hash for {m.group(1)}"
        entries[_norm(m.group(1))] = (m.group(2), hashes)
    return entries


def _locked_runtime() -> dict[str, tuple[str, set[str]]]:
    """Every package reachable from skillabel's own runtime dependencies in
    uv.lock (the dev group is not followed), with its version and hashes."""
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    by_name = {_norm(p["name"]): p for p in lock["package"]}
    todo = [_norm(d["name"]) for d in by_name["skillabel"].get("dependencies", [])]
    seen: dict[str, tuple[str, set[str]]] = {}
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        pkg = by_name[name]
        hashes = set()
        for art in [pkg.get("sdist")] + pkg.get("wheels", []):
            if art and "hash" in art:
                hashes.add(art["hash"].split(":", 1)[1])
        seen[name] = (pkg["version"], hashes)
        todo.extend(_norm(d["name"]) for d in pkg.get("dependencies", []))
    return seen


def test_action_requirements_match_uv_lock_exactly():
    locked = _locked_runtime()
    pinned = _parse_requirements(REPO / "action-requirements.txt")
    assert set(pinned) == set(locked), (
        f"missing: {sorted(set(locked) - set(pinned))}, extra: {sorted(set(pinned) - set(locked))}"
    )
    for name, (version, hashes) in locked.items():
        assert pinned[name][0] == version, f"{name}: file has {pinned[name][0]}, lock has {version}"
        assert pinned[name][1] == hashes, f"{name}: hashes differ from uv.lock"


def test_build_requirements_are_pinned_with_hashes():
    entries = _parse_requirements(REPO / "action-build-requirements.txt")
    assert "hatchling" in entries
    # Nothing here may also be a runtime package at another version.
    locked = _locked_runtime()
    for name, (version, _h) in entries.items():
        if name in locked:
            assert locked[name][0] == version, f"{name} differs from uv.lock"


def test_action_installs_wheels_only_and_waits_seven_days_for_new_releases():
    """No sdist build code runs in the Action's install, and uv takes no
    release younger than 7 days (T14)."""
    action = (REPO / "action.yml").read_text(encoding="utf-8")
    assert "--require-hashes --only-binary :all:" in action
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'exclude-newer = "7 days"' in pyproject
    dependabot = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert dependabot.count("default-days: 7") == 2
    assert 'package-ecosystem: "uv"' in dependabot and 'package-ecosystem: "github-actions"' in dependabot


def test_action_installs_only_from_the_hashed_files():
    action = (REPO / "action.yml").read_text(encoding="utf-8")
    assert "--require-hashes" in action
    assert '-r "$GITHUB_ACTION_PATH/action-requirements.txt"' in action
    assert '-r "$GITHUB_ACTION_PATH/action-build-requirements.txt"' in action
    assert '--no-deps --no-build-isolation "$GITHUB_ACTION_PATH"' in action
