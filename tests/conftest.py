"""Shared test scaffolding."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _not_in_ci(monkeypatch):
    """skillabel reads no `./.env` inside an automated check (CI or
    GITHUB_ACTIONS set). The tests run the same on a CI runner as on a
    laptop: those two are cleared, and tests of that rule set them. A
    provider choice set on the runner is cleared too."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("SKILLABEL_AI_PROVIDER", raising=False)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def wellformed_dir() -> Path:
    return FIXTURES / "wellformed"


@pytest.fixture
def lintfarm_dir() -> Path:
    return FIXTURES / "lintfarm"


@pytest.fixture
def scary_dir() -> Path:
    return FIXTURES / "scary"


@pytest.fixture
def wolf_dir() -> Path:
    return FIXTURES / "wolf"


@pytest.fixture
def sample_label_dict() -> dict:
    return json.loads((FIXTURES / "expected" / "wellformed_label.json").read_text(encoding="utf-8"))
