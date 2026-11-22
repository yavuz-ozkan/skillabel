"""HTML report renderer.

`render(labels, *, title=...)` returns ONE self-contained HTML document:
all CSS/JS inline, zero external requests (offline-openable). The aesthetic is
a dark, restrained auditor — this tool reports surface area with evidence, never
verdicts, and it should look like it.

The template (`templates/report.html.j2`) renders, per skill, the six fixed
panels in order — **Fires when / Needs / Does / Produces / Touches / Costs** —
then Lints, then the Report card. All user-derived content flows through Jinja2
autoescaping. The renderer computes only display view-models here (badge states,
grade chips, token totals); it never mutates the label facts.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .schema import SkillLabel, env_named_folder, linked_folder, skill_md_unread

if TYPE_CHECKING:  # avoid an import cycle — usage.py imports slugify from here
    from .usage import UsageIndex

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Risk kinds surfaced in the aggregate risk-surface distribution (dashboard).
RISK_DIST_KINDS: tuple[str, ...] = ("runs_shell", "network", "writes_files")

# The catalog surfaces exactly these three risks as present / no pattern matched so a
# reader can trust the big three were positively evaluated.
BIG_THREE: tuple[str, ...] = ("runs_shell", "network", "sends_external")

RISK_LABELS: dict[str, str] = {
    "runs_shell": "runs shell",
    "writes_files": "writes files",
    "reads_outside_skill": "reads outside skill",
    "network": "network",
    "sends_external": "sends external",
    "installs_packages": "installs packages",
    "spawns_agents": "spawns agents",
    "handles_secrets": "handles secrets",
}

DIMENSION_LABELS: dict[str, str] = {
    "trigger_precision": "Trigger precision",
    "clarity": "Clarity",
    "context_efficiency": "Context efficiency",
    "self_containedness": "Self-containedness",
    "safety_transparency": "Safety transparency",
}

INPUT_KIND_LABELS: dict[str, str] = {
    "argument": "argument",
    "file": "file",
    "env_var": "env var",
    "api_key": "API key",
    "cli_tool": "CLI tool",
    "mcp_server": "MCP server",
    "network_service": "network service",
}

KEYLESS_NOTE = "Run with --ai for extraction & report card."


def slugify(name: str) -> str:
    """Kebab-case a skill name/dir for anchor ids (mirrors the CLI slug rule)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


def _total_tokens(label: SkillLabel) -> int:
    if label.cost is None:
        return 0
    return (
        label.cost.description_tokens
        + label.cost.body_tokens
        + sum(rf.tokens for rf in label.cost.reference_files)
    )


def _catalog_badges(label: SkillLabel) -> list[dict[str, str]]:
    """One badge per big-three risk: present (red) / absent (gray) / unknown."""
    by_kind = {rf.kind: rf for rf in label.risk}
    badges: list[dict[str, str]] = []
    unread = skill_md_unread(label.unscanned)
    for kind in BIG_THREE:
        flag = by_kind.get(kind)
        if unread:
            state = "unchecked"
        elif flag is not None and not flag.present and label.unscanned:
            state = "partial"  # no pattern matched, but some files were not read
        elif flag is None:
            state = "unknown"
        elif flag.present:
            state = "present"
        else:
            state = "absent"
        title = {"unchecked": "not checked",
                 "partial": "no pattern matched in the files read; some files were not read"}.get(state, state)
        badges.append({"kind": kind, "label": RISK_LABELS[kind], "state": state, "title": title})
    return badges


def _lint_counts(label: SkillLabel) -> dict[str, int]:
    errors = sum(1 for lint in label.lints if lint.severity == "error")
    warnings = sum(1 for lint in label.lints if lint.severity == "warning")
    return {"errors": errors, "warnings": warnings}


def _risk_count(label: SkillLabel) -> int:
    """Number of risk kinds observed present (surface-area size, not a verdict)."""
    return sum(1 for rf in label.risk if rf.present)


def _relative_date(iso_date: str | None, today: date | None = None) -> str | None:
    """A compact human relative age for an ISO date (``YYYY-MM-DD``):
    ``today`` / ``yesterday`` / ``Nd ago`` / ``Nw ago`` / ``Nmo ago`` /
    ``Ny ago``. Returns None for a missing/unparseable date."""
    if not iso_date:
        return None
    try:
        then = date.fromisoformat(iso_date[:10])
    except ValueError:
        return None
    days = ((today or date.today()) - then).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days}d ago"
    if days < 60:
        return f"{days // 7}w ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def _usage_view(usage: "UsageIndex | None", slug: str, today: date | None = None) -> dict | None:
    """Per-card usage view-model, or None when no usage data was collected.
    ``used=False`` is the loud decluttering signal — a known skill never invoked
    across any local session log."""
    if usage is None:
        return None
    record = usage.skills.get(slug)
    if record is None:
        return {"used": False, "uses": 0, "last_rel": None, "claude": 0, "codex": 0}
    return {
        "used": True,
        "uses": record.total_uses,
        "last_rel": _relative_date(record.last_used, today),
        "last_used": record.last_used,
        "claude": record.by_harness.get("claude", 0),
        "codex": record.by_harness.get("codex", 0),
    }


def _group_key(label: SkillLabel) -> str:
    """Grouping key for cross-source duplicate/drift detection: the display slug."""
    return slugify(label.name or PurePath(label.source_path).name or "skill")


def _source_label(source: str | None) -> str | None:
    """A short, human-readable form of a source root — its last two path parts
    (e.g. ``.claude/skills``, ``plugin-dev/skills``)."""
    if not source:
        return None
    parts = [p for p in PurePath(source).parts if p not in ("/", "\\")]
    tail = parts[-2:] if len(parts) >= 2 else parts
    return "/".join(tail) if tail else source


def _lint_sig(label: SkillLabel) -> tuple:
    return tuple(sorted((lint.rule, lint.severity, lint.line or 0) for lint in label.lints))


def _body_tokens(label: SkillLabel) -> int:
    return label.cost.body_tokens if label.cost else 0


def _drift_groups(labels: list[SkillLabel]) -> list[dict]:
    """Detect same-slug skills that appear under more than one source.

    A group is a set of labels sharing a display slug; only groups spanning at
    least two distinct sources are returned. ``status`` is ``identical`` when
    every member's ``content_hash`` matches, else ``drift`` — with ``diffs``
    naming which of description / body length / lint results differ.
    """
    buckets: dict[str, list[int]] = defaultdict(list)
    for i, label in enumerate(labels):
        # A skill whose SKILL.md was not read has nothing to compare.
        if skill_md_unread(label.unscanned):
            continue
        buckets[_group_key(label)].append(i)

    groups: list[dict] = []
    for key, indices in buckets.items():
        members = [labels[i] for i in indices]
        if len({m.source for m in members}) < 2:
            continue
        hashes = {m.content_hash for m in members}
        diffs: list[str] = []
        if len({m.description for m in members}) > 1:
            diffs.append("description")
        if len({_body_tokens(m) for m in members}) > 1:
            diffs.append("body length")
        if len({_lint_sig(m) for m in members}) > 1:
            diffs.append("lint results")
        identical = len(hashes) == 1 and None not in hashes
        status = "identical" if identical else "drift"
        if status == "drift" and not diffs:
            diffs = ["content"]
        groups.append(
            {"key": key, "indices": indices, "status": status, "diffs": diffs, "peers": len(members)}
        )
    return groups


def drift_summary(labels: list[SkillLabel]) -> dict[str, int]:
    """Aggregate cross-source duplicate/drift group counts (shared by the CLI
    summary and the report dashboard)."""
    groups = _drift_groups(labels)
    return {
        "duplicate_groups": sum(1 for g in groups if g["status"] == "identical"),
        "drift_groups": sum(1 for g in groups if g["status"] == "drift"),
    }


def _build_view(
    label: SkillLabel,
    uid: str,
    drift: dict | None,
    usage: "UsageIndex | None" = None,
    today: date | None = None,
) -> dict:
    slug = slugify(label.name or label.source_path or "skill")
    return {
        "label": label,
        "uid": uid,
        "slug": slug,
        "badges": _catalog_badges(label),
        "grades": [
            {"name": DIMENSION_LABELS.get(d.name, d.name), "grade": d.grade}
            for d in (label.report_card or [])
        ],
        "lint_counts": _lint_counts(label),
        "total_tokens": _total_tokens(label),
        "risk_count": _risk_count(label),
        "source": label.source,
        "source_label": _source_label(label.source),
        "drift": drift,
        "usage": _usage_view(usage, slug, today),
        "unread": skill_md_unread(label.unscanned),
        "unread_link": linked_folder(label.unscanned),
        "unread_env": env_named_folder(label.unscanned),
        "not_read": len(label.unscanned),
    }


def _severity_of(view: dict) -> str:
    if view["lint_counts"]["errors"]:
        return "error"
    if view["lint_counts"]["warnings"]:
        return "warning"
    return "clean"


def _usage_dashboard(views: list[dict], usage: "UsageIndex | None") -> dict | None:
    """Usage tiles for the header: top-5 most-used skills (linked to their
    cards), the never-used count (the loud declutter signal), uses split by
    harness, and the unmatched-invocation footnote count. None when usage was
    not collected."""
    if usage is None:
        return None
    # First card per slug — for linking a most-used entry to a card and naming it.
    first: dict[str, dict] = {}
    for v in views:
        first.setdefault(v["slug"], v)

    top5 = []
    for slug, record in usage.top_used(5):
        v = first.get(slug)
        top5.append(
            {
                "slug": slug,
                "uid": v["uid"] if v else None,
                "name": v["label"].name if v else slug,
                "uses": record.total_uses,
            }
        )

    distinct_slugs = {v["slug"] for v in views}
    never_used = sorted(distinct_slugs - set(usage.skills))
    return {
        "top5": top5,
        "never_used_count": len(never_used),
        "by_harness": usage.totals_by_harness(),
        "total_uses": usage.total_uses(),
        "unmatched_count": usage.unmatched_count(),
        "unmatched_distinct": len(usage.unmatched),
        "generated_at": usage.generated_at,
    }


def _dashboard(views: list[dict], labels: list[SkillLabel]) -> dict:
    """Aggregate figures for the report header: tier token totals, top-5 heaviest
    skills, risk-surface distribution, source list, and duplicate/drift counts."""
    # Identical copies (same content hash) of a skill installed under several
    # roots must not double-count the library's token totals or occupy several
    # top-5 slots — dedupe by content hash (fall back to identity when absent).
    def _hash_key(label: SkillLabel) -> object:
        return label.content_hash or id(label)

    seen_hashes: set[object] = set()
    tier = {"description": 0, "body": 0, "reference": 0}
    for label in labels:
        if label.cost is None or _hash_key(label) in seen_hashes:
            continue
        seen_hashes.add(_hash_key(label))
        tier["description"] += label.cost.description_tokens
        tier["body"] += label.cost.body_tokens
        tier["reference"] += sum(rf.tokens for rf in label.cost.reference_files)

    unique_tokens = 0
    seen_hashes.clear()
    by_name: dict[str, dict] = {}
    for v in sorted(views, key=lambda v: v["total_tokens"], reverse=True):
        if _hash_key(v["label"]) not in seen_hashes:
            seen_hashes.add(_hash_key(v["label"]))
            unique_tokens += v["total_tokens"]
        entry = by_name.setdefault(
            v["label"].name,
            {"uid": v["uid"], "name": v["label"].name, "tokens": v["total_tokens"], "copies": 0},
        )
        entry["copies"] += 1
    top5 = [
        e for e in sorted(by_name.values(), key=lambda e: e["tokens"], reverse=True)[:5]
        if e["tokens"] > 0
    ]

    risk_dist = {
        kind: sum(
            1 for label in labels if any(rf.kind == kind and rf.present for rf in label.risk)
        )
        for kind in RISK_DIST_KINDS
    }

    sources: list[dict] = []
    seen_sources: set[str] = set()
    for v in views:
        src = v["source"]
        if src and src not in seen_sources:
            seen_sources.add(src)
            sources.append({"value": src, "label": v["source_label"]})

    drift = drift_summary(labels)
    return {
        "skills": len(views),
        "sources": len(sources),
        "source_list": sources,
        "errors": sum(v["lint_counts"]["errors"] for v in views),
        "warnings": sum(v["lint_counts"]["warnings"] for v in views),
        "tokens": unique_tokens,
        "tier": tier,
        "top5": top5,
        "risk_dist": risk_dist,
        "risk_labels": {k: RISK_LABELS[k] for k in RISK_DIST_KINDS},
        "duplicate_groups": drift["duplicate_groups"],
        "drift_groups": drift["drift_groups"],
    }


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"], default=True, default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        risk_labels=RISK_LABELS,
        dimension_labels=DIMENSION_LABELS,
        input_kind_labels=INPUT_KIND_LABELS,
        keyless_note=KEYLESS_NOTE,
    )
    return env


def render(
    labels: list[SkillLabel],
    *,
    title: str = "Skillabel report",
    usage: "UsageIndex | None" = None,
) -> str:
    """Render labels to one self-contained HTML string.

    Empty input yields a valid empty-state document. Every label is displayed
    with the six fixed panels; keyless labels (`ai_enriched=False`) show the
    keyless note in place of the empty extraction panels and report card. The
    report is interactive client-side (search / sort / filter) and, when labels
    carry multiple sources, badges each skill with its source and flags
    cross-source duplicates and drift.

    When a :class:`~skillabel.usage.UsageIndex` is supplied, each card gains a
    usage line (uses / last-used / per-harness split, or a muted "never used"),
    the header gains usage tiles, and the controls gain a most-used sort and a
    never-used filter. Passing ``usage=None`` renders exactly as before.
    """
    env = _environment()
    template = env.get_template("report.html.j2")
    today = date.today()

    drift_by_index: dict[int, dict] = {}
    for group in _drift_groups(labels):
        for i in group["indices"]:
            drift_by_index[i] = {
                "status": group["status"],
                "diffs": group["diffs"],
                "peers": group["peers"],
            }

    views: list[dict] = []
    seen_uid: dict[str, int] = {}
    for i, label in enumerate(labels):
        slug = slugify(label.name or label.source_path or "skill")
        n = seen_uid.get(slug, 0)
        seen_uid[slug] = n + 1
        uid = slug if n == 0 else f"{slug}-{n + 1}"
        view = _build_view(label, uid, drift_by_index.get(i), usage, today)
        view["severity"] = _severity_of(view)
        views.append(view)

    dashboard = _dashboard(views, labels)
    usage_dashboard = _usage_dashboard(views, usage)
    # `totals` retains the pre-0.2 keys so nothing downstream breaks.
    totals = {
        "skills": dashboard["skills"],
        "errors": dashboard["errors"],
        "warnings": dashboard["warnings"],
        "tokens": dashboard["tokens"],
    }
    return template.render(
        title=title,
        views=views,
        totals=totals,
        dashboard=dashboard,
        usage=usage_dashboard,
        multi_source=dashboard["sources"] > 1,
    )
