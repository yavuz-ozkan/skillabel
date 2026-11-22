"""The label schema — the load-bearing module.

One schema, four consumers: the deterministic scanner's output, the AI
extractor's structured-outputs target, the JSON artifact on disk
(`.skillabel/<slug>.json`), and the HTML report renderer's input.

Honesty mechanism (modelled on an earlier `assumed` flag): every extracted
claim is a `Fact` carrying `evidence` (line numbers + quote from SKILL.md) and
`inferred` — True means the AI inferred it rather than the skill stating it.
The renderer must visually distinguish inferred facts.

Security stance: `RiskFlag`s report surface area with evidence — never
verdicts. There is deliberately no "safe" field anywhere in this schema.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    lines: list[int] = []  # 1-indexed lines in SKILL.md (or in `file`)
    quote: str = ""
    file: str | None = None  # a bundled file, relative; None means SKILL.md


class Fact(BaseModel):
    evidence: Evidence | None = None
    inferred: bool = False  # True = AI inferred it; render an "inferred" tag


class TriggerContract(Fact):
    fires_when: list[str] = []
    skip_when: list[str] = []


class SkillInput(Fact):
    name: str
    kind: Literal[
        "argument", "file", "env_var", "api_key", "cli_tool", "mcp_server", "network_service"
    ]
    required: bool = True


class FlowStep(Fact):
    n: int
    action: str
    tools: list[str] = []  # e.g. ["Bash", "WebFetch", "Write"]


class Output(Fact):
    what: str
    kind: Literal["file", "message", "pr", "external_side_effect", "other"] = "other"


RiskKind = Literal[
    "runs_shell",
    "writes_files",
    "reads_outside_skill",
    "network",
    "sends_external",
    "installs_packages",
    "spawns_agents",
    "handles_secrets",
]


class RiskFlag(Fact):
    kind: RiskKind
    present: bool
    source: Literal["regex", "ai"] = "regex"  # regex pre-pass survives AI disagreement


class ReferenceFile(BaseModel):
    path: str  # relative to the skill directory
    tokens: int
    referenced_from_body: bool


class ContextCost(BaseModel):
    description_tokens: int  # always-in-context tier
    body_tokens: int  # loaded-on-trigger tier
    reference_files: list[ReferenceFile] = []  # on-demand tier
    estimate: bool = True  # v1 ships char-based estimates (ceil(chars / 3.5))


class UnscannedFile(BaseModel):
    """A file in the skill that skillabel did not read, and why. When the
    skill's own SKILL.md is in this list, the skill was not checked at all."""

    path: str  # relative to the skill directory
    reason: str


class Lint(BaseModel):
    rule: str  # rule id from skillabel.lints
    severity: Literal["error", "warning", "info"]
    line: int | None = None
    message: str


DimensionName = Literal[
    "trigger_precision", "clarity", "context_efficiency", "self_containedness", "safety_transparency"
]


class Finding(BaseModel):
    text: str
    lines: list[int]  # must exist in SKILL.md — citation-validated, else rejected


class Dimension(BaseModel):
    name: DimensionName
    grade: Literal["A", "B", "C", "D", "F"]
    findings: list[Finding] = []


class SkillLabel(BaseModel):
    schema_version: Literal[1] = 1
    name: str
    source_path: str  # directory containing SKILL.md, as given to scan
    # v0.2: the discovery/scan root this skill was found under (e.g. ~/.claude/skills).
    # Optional so pre-0.2 label JSONs (which lack it) still load and render.
    source: str | None = None
    # v0.2: SHA-256 of the normalized SKILL.md content, used for drift detection
    # across sources. Optional for backward compatibility with pre-0.2 labels.
    content_hash: str | None = None
    description: str
    trigger: TriggerContract = TriggerContract()
    inputs: list[SkillInput] = []
    flow: list[FlowStep] = []
    outputs: list[Output] = []
    risk: list[RiskFlag] = []
    cost: ContextCost | None = None
    resources: list[str] = []  # bundled files (relative paths, SKILL.md excluded)
    lints: list[Lint] = []
    report_card: list[Dimension] | None = None  # None when run keyless
    ai_enriched: bool = False
    # Files skillabel did not read (links, too big, not text, over a cap).
    # Optional, so labels written before it still load.
    unscanned: list[UnscannedFile] = []
    # Where the skill's pointers that climb out of its folder lead, as paths
    # from the skill folder (``../shared/helpers.md``), by path arithmetic
    # only. The PR comment shows the skill when such a file changed.
    # Optional, so labels written before it still load.
    outside_refs: list[str] = []


# The reason recorded for a skill folder that is itself a link or junction.
# Such a folder is never entered, so nothing in it is read.
LINKED_FOLDER_REASON = "the skill folder is a link or junction, not followed"


# The reason recorded for a skill folder named like a `.env` file. It is never
# entered, so nothing in it is read (decision 88).
ENV_FOLDER_REASON = "the skill folder is named like a .env file, not read"


def env_named_folder(unscanned: list) -> bool:
    """True when the label is for a folder named like a `.env` file."""
    for u in unscanned or []:
        reason = u.get("reason", "") if isinstance(u, dict) else u.reason
        if reason == ENV_FOLDER_REASON:
            return True
    return False


def linked_folder(unscanned: list) -> bool:
    """True when the label is for a skill folder that is a link (see
    LINKED_FOLDER_REASON). Accepts models or plain dicts."""
    for u in unscanned or []:
        reason = u.get("reason", "") if isinstance(u, dict) else u.reason
        if reason == LINKED_FOLDER_REASON:
            return True
    return False


def skill_md_unread(unscanned: list) -> bool:
    """True when the skill's own SKILL.md (in any case) is in *unscanned*:
    the skill was not checked. Accepts models or plain dicts."""
    for u in unscanned or []:
        path = u.get("path", "") if isinstance(u, dict) else u.path
        if str(path).lower() == "skill.md":
            return True
    return False


class ParsedSkill(BaseModel):
    """Deterministic parser output (skillabel.parse) — input to every other layer."""

    path: str  # skill directory
    skill_md_path: str
    frontmatter: dict[str, Any]
    body: str  # markdown after the frontmatter block
    body_start_line: int  # 1-indexed line in SKILL.md where the body begins
    lines: list[str]  # full SKILL.md, split (line N == lines[N-1])
    resources: list[str]  # bundled files, relative, SKILL.md excluded
    # What the safe reader found or refused (see skillabel.parse).
    problems: list[Lint] = []  # findings about reading the skill
    unscanned: list[UnscannedFile] = []
    folders: list[str] = []  # bundled folders, relative
    texts: dict[str, str] = Field(default_factory=dict, exclude=True)  # bundled text files read
    chars: dict[str, int] = Field(default_factory=dict)  # per resource: chars read, or bytes if not read

    @property
    def read(self) -> bool:
        """False when SKILL.md itself was not read; nothing may be claimed."""
        return not skill_md_unread(self.unscanned)


class ExtractionResult(BaseModel):
    """Structured-outputs target for the AI extraction call (skillabel.ai)."""

    trigger: TriggerContract
    inputs: list[SkillInput]
    flow: list[FlowStep]
    outputs: list[Output]
    risk: list[RiskFlag]


class ReportCardResult(BaseModel):
    """Structured-outputs target for the report-card grading call."""

    dimensions: list[Dimension]
