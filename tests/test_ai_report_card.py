"""Report-card grading + citation-validation tests (mocked provider)."""

from __future__ import annotations

from skillabel.ai import enrich
from skillabel.schema import (
    Dimension,
    Evidence,
    ExtractionResult,
    Finding,
    FlowStep,
    Output,
    ParsedSkill,
    ReportCardResult,
    SkillInput,
    SkillLabel,
    TriggerContract,
)

_DIMS = [
    "trigger_precision",
    "clarity",
    "context_efficiency",
    "self_containedness",
    "safety_transparency",
]
_ANNOTATION = "(citation validation removed ungrounded findings)"


# --------------------------------------------------------------------------- #
# Fakes / builders
# --------------------------------------------------------------------------- #

class FakeProvider:
    def __init__(self, report_cards: list[ReportCardResult]) -> None:
        self._rcs = list(report_cards)
        self.rc_calls = 0
        self.models: list[str] = []

    async def parse_structured(self, system, user, model, output_model, max_tokens):
        self.models.append(model)
        if output_model is ExtractionResult:
            return _empty_extraction()
        if output_model is ReportCardResult:
            rc = self._rcs[min(self.rc_calls, len(self._rcs) - 1)]
            self.rc_calls += 1
            return rc
        raise AssertionError(f"unexpected output_model {output_model!r}")

    async def complete_text(self, system, user, model, max_tokens):  # pragma: no cover
        return ""


def _empty_extraction() -> ExtractionResult:
    return ExtractionResult(
        trigger=TriggerContract(evidence=Evidence(lines=[5], quote="x")),
        inputs=[SkillInput(name="i", kind="file", evidence=Evidence(lines=[5]))],
        flow=[FlowStep(n=1, action="do", evidence=Evidence(lines=[6]))],
        outputs=[Output(what="o", kind="file", evidence=Evidence(lines=[6]))],
        risk=[],
    )


def make_parsed(n_lines: int = 12) -> ParsedSkill:
    lines = [f"line {i} content" for i in range(1, n_lines + 1)]
    return ParsedSkill(
        path="skills/demo",
        skill_md_path="skills/demo/SKILL.md",
        frontmatter={"name": "demo", "description": "d"},
        body="\n".join(lines[4:]),
        body_start_line=5,
        lines=lines,
        resources=[],
    )


def base_label() -> SkillLabel:
    return SkillLabel(name="demo", source_path="skills/demo", description="A demo skill.")


def report_card(**overrides: Dimension) -> ReportCardResult:
    dims = [overrides.get(name, Dimension(name=name, grade="A", findings=[])) for name in _DIMS]
    return ReportCardResult(dimensions=dims)


def _dim(name: str, grade: str, findings: list[Finding]) -> Dimension:
    return Dimension(name=name, grade=grade, findings=findings)


# --------------------------------------------------------------------------- #
# Grounded findings survive; report card lands on the label
# --------------------------------------------------------------------------- #

async def test_grounded_findings_survive():
    rc = report_card(
        clarity=_dim("clarity", "C", [Finding(text="prose, not steps", lines=[7])]),
    )
    provider = FakeProvider([rc])

    out = await enrich(base_label(), make_parsed(), provider=provider)

    assert out.report_card is not None
    clarity = next(d for d in out.report_card if d.name == "clarity")
    assert clarity.grade == "C"
    assert [f.text for f in clarity.findings] == ["prose, not steps"]
    assert provider.rc_calls == 1  # nothing needed a regrade


# --------------------------------------------------------------------------- #
# A finding citing a nonexistent line (9999) is dropped
# --------------------------------------------------------------------------- #

async def test_out_of_range_finding_is_dropped():
    rc = report_card(
        clarity=_dim(
            "clarity", "B",
            [Finding(text="grounded", lines=[7]), Finding(text="ghost", lines=[9999])],
        ),
    )
    provider = FakeProvider([rc])

    out = await enrich(base_label(), make_parsed(n_lines=12), provider=provider)

    clarity = next(d for d in out.report_card if d.name == "clarity")
    texts = [f.text for f in clarity.findings]
    assert "grounded" in texts
    assert "ghost" not in texts  # cited line 9999 does not exist


# --------------------------------------------------------------------------- #
# Zero grounded findings + grade below B => one regrade, then annotate
# --------------------------------------------------------------------------- #

async def test_ungrounded_low_grade_regrades_then_annotates():
    bad = report_card(
        self_containedness=_dim("self_containedness", "F", [Finding(text="ghost", lines=[9999])]),
    )
    provider = FakeProvider([bad])  # repeats the same ungrounded card on regrade

    out = await enrich(base_label(), make_parsed(), provider=provider)

    assert provider.rc_calls == 2  # one initial + one regrade attempt
    sc = next(d for d in out.report_card if d.name == "self_containedness")
    assert sc.grade == "F"  # grade kept
    assert [f.text for f in sc.findings] == [_ANNOTATION]  # annotated, not silently blank


# --------------------------------------------------------------------------- #
# Regrade can rescue: second card supplies a grounded finding
# --------------------------------------------------------------------------- #

async def test_regrade_rescues_with_grounded_finding():
    bad = report_card(
        self_containedness=_dim("self_containedness", "F", [Finding(text="ghost", lines=[9999])]),
    )
    good = report_card(
        self_containedness=_dim("self_containedness", "F", [Finding(text="dead reference at guide.md", lines=[10])]),
    )
    provider = FakeProvider([bad, good])

    out = await enrich(base_label(), make_parsed(), provider=provider)

    assert provider.rc_calls == 2
    sc = next(d for d in out.report_card if d.name == "self_containedness")
    assert sc.grade == "F"
    assert [f.text for f in sc.findings] == ["dead reference at guide.md"]  # rescued, not annotated


# --------------------------------------------------------------------------- #
# Zero findings but grade B (not below B) is left alone — no regrade, no annotate
# --------------------------------------------------------------------------- #

async def test_grade_b_with_no_findings_is_not_annotated():
    rc = report_card(
        clarity=_dim("clarity", "B", []),
    )
    provider = FakeProvider([rc])

    out = await enrich(base_label(), make_parsed(), provider=provider)

    assert provider.rc_calls == 1  # B is not "below B" — no regrade
    clarity = next(d for d in out.report_card if d.name == "clarity")
    assert clarity.grade == "B"
    assert clarity.findings == []
