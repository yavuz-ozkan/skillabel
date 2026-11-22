"""AI enrichment layer: structured extraction and the report card.

`enrich` layers structured extraction and a rubric-graded report card on top of
a deterministic `SkillLabel`, then merges by these rules:

- Deterministic facts win. Lints, cost, and resources are copied through
  untouched. A regex risk flag with ``present=True`` survives even when the AI
  disagrees (the wolf rule): risk is deduped by ``kind`` — the regex flag is
  kept and an AI flag is added only for kinds the regex pre-pass never asserted.
- Extraction fills ``trigger``/``inputs``/``flow``/``outputs``. Every fact must
  cite line numbers from the numbered body; ``inferred=False`` is reserved for
  facts the skill literally states. Risk flags never soften — shell/network in
  the text means the flag is present regardless of surrounding prose.
- Citation validation: a report-card ``Finding`` citing lines outside the
  file's range is dropped; a dimension left with zero grounded findings AND a
  grade below B triggers one regrade attempt, then keeps the grade but annotates
  it. Extraction facts whose evidence lines are out of range have their evidence
  clamped to ``None`` and ``inferred`` set to ``True`` (an uncited fact is by
  definition inferred).
- ``ai_enriched=True`` only when both provider calls succeed.

Provider resolution is delegated to the ``skillabel.providers`` layer;
``AIProviderNotConfiguredError`` propagates when no provider is injected and none
is configured (the CLI catches it and degrades to a keyless deterministic label).
"""

from __future__ import annotations

from pathlib import Path

from ..providers import (
    ALLOWED_MODELS,
    DEFAULT_MODEL,
    AIProvider,
    resolve_provider,
)
from ..schema import (
    Dimension,
    ExtractionResult,
    Fact,
    Finding,
    ParsedSkill,
    ReportCardResult,
    RiskFlag,
    SkillLabel,
)

_MAX_TOKENS = 4096
_REGRADE_FLOOR = {"C", "D", "F"}  # grades "below B" that must be grounded or annotated
_ANNOTATION = "(citation validation removed ungrounded findings)"

_DIMENSIONS = [
    "trigger_precision",
    "clarity",
    "context_efficiency",
    "self_containedness",
    "safety_transparency",
]
_RUBRIC_DIR = Path(__file__).resolve().parent.parent / "rubrics"

_EXTRACTION_SYSTEM = """\
You are Skillabel's extraction pass. You read the body of a Claude-style
SKILL.md and extract the contract it hides in prose: its trigger, inputs, flow,
outputs, and risk surface. You are an auditor, not an advocate.

Honesty rules (binding):
- Every fact you emit MUST cite the line numbers it came from, in
  `evidence.lines`, using the "N| " line numbers shown in the body. Put the
  quoted span in `evidence.quote`.
- Set `inferred=False` ONLY for facts the skill LITERALLY states on a cited
  line. Anything you deduce, generalize, or assume is `inferred=True`; if you
  cannot cite a line for it, it is inferred and its evidence stays empty.
- NEVER soften a risk flag to fit the prose. If a shell/bash/subprocess
  invocation appears anywhere in the body, `runs_shell.present=True`. If a URL
  fetch (curl/wget/WebFetch/requests/httpx) appears, `network.present=True`.
  This holds even when the surrounding text claims the skill is safe or has no
  side effects — the operation in the text wins over the claim about it.
- Emit a RiskFlag only for kinds you can evidence from the body.
"""

_REPORT_CARD_SYSTEM_HEADER = """\
You are Skillabel's report-card pass. You grade a Claude-style SKILL.md against
five committed rubrics, one grade (A–F) per dimension, plus per-finding evidence.
There is NO aggregate score. Rules:
- Grade each dimension strictly against its rubric's A/C/F anchors below.
- Every `Finding` MUST cite real line numbers from the numbered body in
  `lines`; a finding you cannot ground on an existing line is a bug — omit it.
- Never render a verdict of "safe" or "unsafe". Safety_transparency grades
  honesty about surface area, not danger.

RUBRICS
=======
"""


def _load_rubrics() -> str:
    parts = [(_RUBRIC_DIR / f"{name}.md").read_text(encoding="utf-8") for name in _DIMENSIONS]
    return "\n\n".join(parts)


def _numbered_body(parsed: ParsedSkill) -> str:
    """The SKILL.md body with each line prefixed by its 1-indexed SKILL.md line
    number, so extracted `evidence.lines` are validatable against the file."""
    start = max(1, parsed.body_start_line)
    total = len(parsed.lines)
    return "\n".join(f"{i}| {parsed.lines[i - 1]}" for i in range(start, total + 1))


def _user_prompt(label: SkillLabel, parsed: ParsedSkill) -> str:
    return (
        f"Skill name: {label.name}\n"
        f"Description (always-in-context): {label.description}\n\n"
        "SKILL.md body (each line prefixed 'N| ' with its line number):\n\n"
        f"{_numbered_body(parsed)}\n"
    )


def _in_range(lines: list[int], n: int) -> bool:
    return bool(lines) and all(1 <= line <= n for line in lines)


def _clamp_fact(fact: Fact, n: int) -> Fact:
    """Out-of-range (or empty) evidence → drop evidence, mark inferred."""
    ev = fact.evidence
    if ev is not None and not _in_range(ev.lines, n):
        return fact.model_copy(update={"evidence": None, "inferred": True})
    return fact


def _merge_risk(base: list[RiskFlag], ai: list[RiskFlag]) -> list[RiskFlag]:
    """Regex flags survive AI disagreement; dedupe by kind — keep the regex flag,
    add an AI flag only for kinds the regex pre-pass never asserted."""
    seen = {flag.kind for flag in base}
    merged = list(base)
    for flag in ai:
        if flag.kind not in seen:
            merged.append(flag.model_copy(update={"source": "ai"}))
            seen.add(flag.kind)
    return merged


def _validate_dimension(dim: Dimension, n: int) -> Dimension:
    kept = [f for f in dim.findings if _in_range(f.lines, n)]
    return dim.model_copy(update={"findings": kept})


def _needs_regrade(dim: Dimension) -> bool:
    return not dim.findings and dim.grade in _REGRADE_FLOOR


async def _extract(
    provider: AIProvider, label: SkillLabel, parsed: ParsedSkill, model: str
) -> ExtractionResult:
    return await provider.parse_structured(
        _EXTRACTION_SYSTEM, _user_prompt(label, parsed), model, ExtractionResult, _MAX_TOKENS
    )


async def _report_card(
    provider: AIProvider, label: SkillLabel, parsed: ParsedSkill, model: str
) -> list[Dimension]:
    n = len(parsed.lines)
    system = _REPORT_CARD_SYSTEM_HEADER + _load_rubrics()
    user = _user_prompt(label, parsed)

    result = await provider.parse_structured(system, user, model, ReportCardResult, _MAX_TOKENS)
    dims = [_validate_dimension(d, n) for d in result.dimensions]

    if any(_needs_regrade(d) for d in dims):
        regrade = await provider.parse_structured(system, user, model, ReportCardResult, _MAX_TOKENS)
        fresh = {d.name: _validate_dimension(d, n) for d in regrade.dimensions}
        dims = [
            fresh[d.name] if _needs_regrade(d) and d.name in fresh and not _needs_regrade(fresh[d.name]) else d
            for d in dims
        ]

    # Any dimension still ungrounded keeps its grade but is annotated.
    return [
        d.model_copy(update={"findings": [Finding(text=_ANNOTATION, lines=[])]}) if _needs_regrade(d) else d
        for d in dims
    ]


async def enrich(
    label: SkillLabel,
    parsed: ParsedSkill,
    *,
    model: str = DEFAULT_MODEL,
    provider: AIProvider | None = None,
) -> SkillLabel:
    """Enrich a deterministic label with AI extraction and a report card.

    Runs the extraction and report-card calls, applies citation validation, and
    merges by the rules in the module docstring. `model` outside `ALLOWED_MODELS` falls back to
    `DEFAULT_MODEL`. With `provider=None`, resolves via `resolve_provider()`;
    `AIProviderNotConfiguredError` propagates when nothing is configured.
    """
    if model not in ALLOWED_MODELS:
        model = DEFAULT_MODEL
    if provider is None:
        provider = resolve_provider()

    n = len(parsed.lines)

    extraction = await _extract(provider, label, parsed, model)
    report_card = await _report_card(provider, label, parsed, model)

    trigger = _clamp_fact(extraction.trigger, n)
    inputs = [_clamp_fact(f, n) for f in extraction.inputs]
    flow = [_clamp_fact(f, n) for f in extraction.flow]
    outputs = [_clamp_fact(f, n) for f in extraction.outputs]
    ai_risk = [_clamp_fact(f, n) for f in extraction.risk]

    return label.model_copy(
        update={
            "trigger": trigger,
            "inputs": inputs,
            "flow": flow,
            "outputs": outputs,
            "risk": _merge_risk(label.risk, ai_risk),  # deterministic risk untouched
            "report_card": report_card,
            "ai_enriched": True,
        }
    )
