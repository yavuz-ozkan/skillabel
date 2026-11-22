"""Debug: compare OpenRouter structured (response_format) vs instructed-JSON paths.

Prints status codes and result shape only — never the key.
"""

import asyncio
import json
import os

import httpx

from skillabel.ai import _EXTRACTION_SYSTEM, _user_prompt
from skillabel.deterministic import build_label
from skillabel.parse import parse_skill
from skillabel.schema import ExtractionResult

PATH = "tests/fixtures/wolf"


async def main() -> None:
    key = os.environ["OPENROUTER_API_KEY"]
    parsed = parse_skill(PATH)
    label = build_label(PATH)
    user = _user_prompt(label, parsed)
    schema = ExtractionResult.model_json_schema()
    base = {
        "model": "anthropic/claude-sonnet-5",
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": _EXTRACTION_SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", timeout=180.0) as http:
        headers = {"Authorization": f"Bearer {key}"}

        for trial in range(2):
            r = await http.post("/chat/completions", headers=headers, json={
                **base,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "extraction", "strict": True, "schema": schema}},
            })
            shape = "?"
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                try:
                    data = json.loads(content)
                    shape = f"flow={len(data.get('flow', []))} outputs={len(data.get('outputs', []))} fires={len(data.get('trigger', {}).get('fires_when', []))} skips={len(data.get('trigger', {}).get('skip_when', []))}"
                except Exception as e:  # noqa: BLE001
                    shape = f"unparseable: {type(e).__name__}"
            print(f"structured trial {trial}: HTTP {r.status_code} -> {shape}")

        r = await http.post("/chat/completions", headers=headers, json={
            **base,
            "messages": [
                {"role": "system", "content": _EXTRACTION_SYSTEM
                 + "\n\nRespond with ONLY a valid JSON object matching this JSON Schema — no prose, no code fences:\n"
                 + json.dumps(schema)},
                {"role": "user", "content": user},
            ],
        })
        content = r.json()["choices"][0]["message"]["content"]
        content2 = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(content2)
        print(f"instructed: HTTP {r.status_code} -> flow={len(data.get('flow', []))} outputs={len(data.get('outputs', []))} fires={len(data.get('trigger', {}).get('fires_when', []))} skips={len(data.get('trigger', {}).get('skip_when', []))}")


if __name__ == "__main__":
    asyncio.run(main())
