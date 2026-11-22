"""Tests for deterministic context cost (skillabel.cost)."""

from __future__ import annotations

import math

from skillabel.cost import context_cost, estimate_tokens
from skillabel.parse import parse_skill


def test_estimate_tokens_is_ceil_chars_over_3_5():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1  # ceil(1/3.5)
    assert estimate_tokens("x" * 7) == 2  # ceil(7/3.5)
    assert estimate_tokens("abcdefgh") == math.ceil(8 / 3.5)


def test_context_cost_tiers(wellformed_dir):
    parsed = parse_skill(wellformed_dir)
    cost = context_cost(parsed)
    assert cost.estimate is True
    assert cost.description_tokens == estimate_tokens(parsed.frontmatter["description"])
    assert cost.body_tokens == estimate_tokens(parsed.body)
    assert cost.description_tokens > 0
    assert cost.body_tokens > 0


def test_reference_files_referenced_from_body(wellformed_dir):
    parsed = parse_skill(wellformed_dir)
    cost = context_cost(parsed)
    by_path = {rf.path: rf for rf in cost.reference_files}
    assert set(by_path) == {"reference.md", "scripts/convert.py"}
    # Both resources are referenced from the body in the well-formed fixture.
    assert by_path["reference.md"].referenced_from_body is True
    assert by_path["scripts/convert.py"].referenced_from_body is True
    assert all(rf.tokens > 0 for rf in cost.reference_files)


def test_undocumented_resource_not_referenced(lintfarm_dir):
    parsed = parse_skill(lintfarm_dir)
    cost = context_cost(parsed)
    by_path = {rf.path: rf for rf in cost.reference_files}
    # mystery.sh is never mentioned in the lintfarm body.
    assert by_path["scripts/mystery.sh"].referenced_from_body is False
