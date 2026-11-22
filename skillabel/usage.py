"""Usage collection (v0.3) — "which of my skills do I actually USE?".

Answers the decluttering question *retroactively*, from local agent session
logs already on disk. No telemetry, no setup: we read the logs two agents keep,
count skill invocations, and attribute a last-used date. **Only skill names and
timestamps are ever read** — never prompt or message content, and nothing is
sent anywhere.

Two harnesses, two very different signals (both characterized against the real
files on this machine):

Claude Code — ``~/.claude/projects/**/*.jsonl``
    Each line is one JSON record. A skill invocation is a ``Skill`` tool call:
    ``"name":"Skill","input":{"skill":"<name>"``. Slash-command markers
    ``<command-name>/<name></command-name>`` in user messages are *also* counted
    — but only when the name (leading ``/`` and any ``plugin:`` prefix stripped)
    matches a known skill slug, so harness built-ins like ``/clear`` or
    ``/rate-limit-options`` never count. Every use is one invocation (no
    per-session dedup); the line's ``timestamp`` field gives last-used.

Codex — ``~/.codex/sessions/**/*.jsonl``
    Records are ``{"payload": {...}, "timestamp": ..., "type": ...}``. Every
    session's ``user_instructions``/system prompt lists *every* installed skill
    as ``(file: .../skills/<slug>/SKILL.md)`` — a catalog, NOT usage (all 515
    sessions reference all skills). Real usage is the agent actually *opening* a
    SKILL.md via a ``shell_command`` function-call, e.g.
    ``Get-Content -Path skills/pdf-ocr/SKILL.md``. So a Codex use is counted
    once per session per skill whose ``SKILL.md`` is read inside an executed
    ``shell_command`` — the catalog in the instructions is ignored because it
    never appears in a function-call record.

Performance: hundreds of files, streamed line-by-line with a cheap substring
gate before any regex; whole files are never ``json.loads``-ed. Unreadable files
are skipped. The full scan on this machine runs in a couple of seconds.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from .report import slugify

# --- default log locations (injectable for tests) ---------------------------
# Resolved at call time (not import time) so a monkeypatched Path.home() — the
# way the CLI tests plant hermetic roots — is honored.


def default_claude_root() -> Path:
    return Path.home() / ".claude" / "projects"


def default_codex_root() -> Path:
    return Path.home() / ".codex" / "sessions"

# --- compiled matchers (all applied per line, never on whole files) ---------

# Claude: a Skill tool-use block — captures the invoked skill name.
_CLAUDE_SKILL = re.compile(
    r'"name"\s*:\s*"Skill"\s*,\s*"input"\s*:\s*\{\s*"skill"\s*:\s*"([^"]+)"'
)
# Claude: a slash-command marker in a user message.
_CLAUDE_CMD = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")
# Any record's ISO-8601 timestamp (top-level field on both harnesses).
_TIMESTAMP = re.compile(r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2})')
# Codex: the skill-directory name immediately preceding a SKILL.md reference.
# Separators may be "/" or JSON-escaped Windows backslashes ("\\\\"); "+" spans
# however many escape layers the double-encoded arguments string carries.
_CODEX_SKILL = re.compile(r"([A-Za-z0-9_.-]+)[/\\]+SKILL\.md")


class SkillUsage(BaseModel):
    """Per-skill usage rollup. ``by_harness`` always carries both keys so the
    schema stays harness-tagged even when one harness contributed nothing."""

    total_uses: int = 0
    last_used: str | None = None  # ISO date (YYYY-MM-DD) of the most recent use
    by_harness: dict[str, int] = Field(default_factory=lambda: {"claude": 0, "codex": 0})


class UsageIndex(BaseModel):
    """slug -> :class:`SkillUsage`, plus the unknown names we saw but can't map.

    Only skills that map to a *known* slug (a scanned label) land in ``skills``.
    Skill invocations whose name we don't recognize (an uninstalled/renamed
    skill, a project-local Codex skill) are tallied in ``unmatched`` — surfaced
    in the report as a footnote count, never as cards.
    """

    schema_version: int = 1
    harnesses: list[str] = Field(default_factory=lambda: ["claude", "codex"])
    generated_at: str = ""
    skills: dict[str, SkillUsage] = Field(default_factory=dict)
    unmatched: dict[str, int] = Field(default_factory=dict)

    # --- convenience rollups (used by the CLI summary and the report) --------

    def totals_by_harness(self) -> dict[str, int]:
        totals = {"claude": 0, "codex": 0}
        for usage in self.skills.values():
            for harness, n in usage.by_harness.items():
                totals[harness] = totals.get(harness, 0) + n
        return totals

    def total_uses(self) -> int:
        return sum(u.total_uses for u in self.skills.values())

    def unmatched_count(self) -> int:
        """Total unmatched *invocations* (not distinct names)."""
        return sum(self.unmatched.values())

    def top_used(self, n: int = 5) -> list[tuple[str, SkillUsage]]:
        return sorted(
            self.skills.items(),
            key=lambda kv: (kv[1].total_uses, kv[1].last_used or ""),
            reverse=True,
        )[:n]

    def write(self, path: Path) -> None:
        from .safeio import safe_write_text  # never writes through a link

        safe_write_text(path, self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> "UsageIndex":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _canonical(name: str) -> str:
    """Normalize an invocation name to a comparable slug: drop a leading ``/``
    and any ``namespace:`` / ``plugin:`` prefix, then kebab-case. So
    ``/superpowers:brainstorm`` -> ``brainstorm`` and
    ``frontend-design:frontend-design`` -> ``frontend-design``."""
    name = name.strip().lstrip("/").strip()
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return slugify(name)


def _line_date(line: str) -> str | None:
    m = _TIMESTAMP.search(line)
    return m.group(1) if m else None


class _Aggregate:
    """Mutable accumulator; frozen into a :class:`UsageIndex` at the end."""

    def __init__(self, known: set[str]) -> None:
        self.known = known
        self.skills: dict[str, SkillUsage] = {}
        self.unmatched: dict[str, int] = {}

    def record(self, canon: str, harness: str, when: str | None, *, count: int = 1) -> None:
        if canon not in self.known:
            self.unmatched[canon] = self.unmatched.get(canon, 0) + count
            return
        usage = self.skills.get(canon)
        if usage is None:
            usage = SkillUsage(by_harness={"claude": 0, "codex": 0})
            self.skills[canon] = usage
        usage.by_harness[harness] = usage.by_harness.get(harness, 0) + count
        usage.total_uses += count
        if when and (usage.last_used is None or when > usage.last_used):
            usage.last_used = when


def _iter_jsonl(root: Path):
    """Yield every readable ``*.jsonl`` under *root* (recursively)."""
    if not root or not root.is_dir():
        return
    for path in root.rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                yield path, fh
        except OSError:
            continue


def _collect_claude(root: Path, agg: _Aggregate) -> None:
    for _path, fh in _iter_jsonl(root):
        try:
            for line in fh:
                if '"Skill"' not in line and "<command-name>" not in line:
                    continue
                when = None
                for m in _CLAUDE_SKILL.finditer(line):
                    when = when or _line_date(line)
                    # Skill tool-use is unambiguously a skill: unknown -> unmatched.
                    agg.record(_canonical(m.group(1)), "claude", when)
                for m in _CLAUDE_CMD.finditer(line):
                    canon = _canonical(m.group(1))
                    # Slash commands are ambiguous (built-in vs skill): only count
                    # a confirmed known slug, and never route them to unmatched.
                    if canon in agg.known:
                        when = when or _line_date(line)
                        agg.record(canon, "claude", when)
        except OSError:
            continue


def _collect_codex(root: Path, agg: _Aggregate) -> None:
    for _path, fh in _iter_jsonl(root):
        # Per session (= per file): count each read skill once.
        session: dict[str, str | None] = {}
        try:
            for line in fh:
                if "SKILL.md" not in line:
                    continue
                if '"name":"shell_command"' not in line or '"type":"function_call"' not in line:
                    continue
                when = _line_date(line)
                for m in _CODEX_SKILL.finditer(line):
                    slug = m.group(1)
                    if slug.startswith("."):  # .system/ scaffolding skills
                        continue
                    canon = slugify(slug)
                    prev = session.get(canon, "__missing__")
                    if prev == "__missing__":
                        session[canon] = when
                    elif when and (prev is None or when > prev):
                        session[canon] = when
        except OSError:
            continue
        for canon, when in session.items():
            agg.record(canon, "codex", when)


def stores_present(claude_root: Path | None = None, codex_root: Path | None = None) -> bool:
    """True when at least one harness log store exists on disk."""
    claude_root = claude_root or default_claude_root()
    codex_root = codex_root or default_codex_root()
    return claude_root.is_dir() or codex_root.is_dir()


def collect_usage(
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    known_slugs: set[str] | None = None,
) -> UsageIndex:
    """Scan the local Claude Code and Codex session logs and roll up per-skill
    usage. Roots default to the real home locations but are injectable so tests
    can point at hand-crafted fixtures. ``known_slugs`` are the slugs of the
    scanned labels; a use maps to a known slug, and unrecognized skill names are
    tallied under :attr:`UsageIndex.unmatched`.
    """
    claude_root = claude_root if claude_root is not None else default_claude_root()
    codex_root = codex_root if codex_root is not None else default_codex_root()
    known = {_canonical(s) for s in (known_slugs or set())}

    agg = _Aggregate(known)
    _collect_claude(claude_root, agg)
    _collect_codex(codex_root, agg)

    return UsageIndex(
        generated_at=date.today().isoformat(),
        skills=dict(sorted(agg.skills.items())),
        unmatched=dict(sorted(agg.unmatched.items(), key=lambda kv: kv[1], reverse=True)),
    )
