"""Tests for the deterministic risk pre-pass (skillabel.riskscan)."""

from __future__ import annotations

from skillabel.parse import parse_skill
from skillabel.riskscan import regex_risk


def _present(flags):
    return {f.kind for f in flags if f.present}


def _by_kind(flags):
    return {f.kind: f for f in flags}


def test_wellformed_runs_shell_present_bigthree_checked(wellformed_dir):
    flags = regex_risk(parse_skill(wellformed_dir))
    by_kind = _by_kind(flags)
    # pandoc via Bash -> runs_shell present, with line evidence.
    assert by_kind["runs_shell"].present is True
    assert by_kind["runs_shell"].source == "regex"
    assert by_kind["runs_shell"].evidence.lines
    # network / sends_external checked and asserted absent (present=False markers).
    assert by_kind["network"].present is False
    assert by_kind["sends_external"].present is False


def test_scary_flags_the_full_surface(scary_dir):
    flags = regex_risk(parse_skill(scary_dir))
    present = _present(flags)
    expected = {
        "runs_shell",
        "network",
        "sends_external",
        "installs_packages",
        "handles_secrets",
        "writes_files",
        "spawns_agents",
    }
    assert expected <= present
    # Every present flag carries line evidence.
    for f in flags:
        if f.present:
            assert f.evidence is not None and f.evidence.lines


def test_wolf_regex_flags_despite_safety_claims(wolf_dir):
    # Load-bearing security test: the description prose claims "no commands,
    # no network access", but the body runs shell and fetches over the network.
    # The regex pre-pass must flag both regardless of the prose.
    flags = regex_risk(parse_skill(wolf_dir))
    by_kind = _by_kind(flags)
    assert by_kind["runs_shell"].present is True
    assert by_kind["network"].present is True
    assert by_kind["runs_shell"].evidence.lines
    assert by_kind["network"].evidence.lines
    # sends_external was checked and none matched -> present=False marker.
    assert by_kind["sends_external"].present is False


def test_absent_non_bigthree_kinds_are_omitted(wellformed_dir):
    flags = regex_risk(parse_skill(wellformed_dir))
    kinds = {f.kind for f in flags}
    # No installs_packages / spawns_agents spam when nothing matched.
    assert "installs_packages" not in kinds
    assert "spawns_agents" not in kinds
