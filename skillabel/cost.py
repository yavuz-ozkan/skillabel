"""Deterministic context-cost estimation.

The v1 token estimator is char-based and offline: ``ceil(len(text) / 3.5)``.
It is used everywhere a token count is needed keyless, and labelled
``estimate=True`` so the renderer can say so.

The three-tier breakdown mirrors how a skill actually loads:
- ``description_tokens`` — always in context (the frontmatter description),
- ``body_tokens``        — loaded when the skill triggers (the markdown body),
- ``reference_files``    — on-demand; each bundled resource with its token
  cost and whether the body references its relative path or basename.
"""

from __future__ import annotations

import math

from .schema import ContextCost, ParsedSkill, ReferenceFile


def estimate_tokens(text: str) -> int:
    """Char-based token estimate: ``ceil(len(text) / 3.5)`` (the v1 estimator)."""
    return math.ceil(len(text) / 3.5)


def _referenced_from_body(body: str, rel_path: str) -> bool:
    """True when the body mentions the resource's relative path or basename."""
    basename = rel_path.rsplit("/", 1)[-1]
    return rel_path in body or basename in body


def context_cost(parsed: ParsedSkill) -> ContextCost:
    """Build the deterministic :class:`ContextCost` for a parsed skill."""
    description = str(parsed.frontmatter.get("description") or "")

    reference_files: list[ReferenceFile] = []
    for rel in parsed.resources:
        # The parser already read each bundled file once, safely; a file it
        # did not read (too big, over the budget) is counted by its size.
        n = parsed.chars.get(rel, 0)
        reference_files.append(
            ReferenceFile(
                path=rel,
                tokens=math.ceil(n / 3.5),
                referenced_from_body=_referenced_from_body(parsed.body, rel),
            )
        )

    return ContextCost(
        description_tokens=estimate_tokens(description),
        body_tokens=estimate_tokens(parsed.body),
        reference_files=reference_files,
        estimate=True,
    )
