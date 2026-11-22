"""Debug: dump the raw ExtractionResult for a fixture skill (no secrets printed)."""

import asyncio
import json
import sys

from skillabel.ai import _extract  # noqa: PLC2701 — debug script
from skillabel.deterministic import build_label
from skillabel.parse import parse_skill
from skillabel.providers import resolve_provider


async def main() -> None:
    path = sys.argv[1]
    parsed = parse_skill(path)
    label = build_label(path)
    provider = resolve_provider()
    print(f"provider: {type(provider).__name__}")
    result = await _extract(provider, label, parsed, "claude-sonnet-5")
    print(json.dumps(result.model_dump(), indent=2)[:4000])


if __name__ == "__main__":
    asyncio.run(main())
