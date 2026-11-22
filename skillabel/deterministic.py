"""The keyless deterministic label.

``build_label`` composes the whole deterministic core — parse + lints + cost +
regex risk — into a :class:`SkillLabel` with ``ai_enriched=False`` and
``report_card=None``. The AI layer (``skillabel.ai``) enriches this in place; the CLI can
render a fully useful label from this function alone with no API key.

``safe_build`` is what the commands call: one hostile skill that makes the
parser fail gives a label that says it was not checked, and the run goes on.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .cost import context_cost
from .lints import outside_paths, run_lints
from .parse import SKILL_FILENAME, parse_skill
from .riskscan import regex_risk
from .schema import ENV_FOLDER_REASON, LINKED_FOLDER_REASON, Lint, ParsedSkill, SkillLabel, UnscannedFile


def _content_hash(parsed: ParsedSkill) -> str | None:
    """SHA-256 of the normalized SKILL.md content (BOM/CR already stripped by the
    parser), so drift detection is stable regardless of checkout EOL style.
    None when SKILL.md was not read: there is nothing to compare."""
    if not parsed.read:
        return None
    text = "\n".join(parsed.lines)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def clean_text(value: object) -> tuple[str, bool]:
    """``str(value)`` with lone surrogates (which cannot be written as UTF-8)
    replaced by U+FFFD. Returns the text and whether anything was replaced."""
    s = str(value)
    out = "".join("�" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in s)
    return out, out != s


def build_label(path: str | Path, *, source: str | None = None, parsed: ParsedSkill | None = None) -> SkillLabel:
    """Build the deterministic :class:`SkillLabel` for the skill at *path*.

    The ``name`` falls back to the skill directory name when frontmatter lacks a
    ``name`` field. ``source`` records the discovery/scan root this skill was
    found under (v0.2 multi-source scan); it is left ``None`` when unspecified.
    Pass *parsed* to reuse a parse already made.
    """
    if parsed is None:
        parsed = parse_skill(path)
    fm = parsed.frontmatter

    name, bad_name = clean_text(str(fm.get("name") or "").strip() or Path(parsed.path).name)
    description, bad_desc = clean_text(fm.get("description") or "")
    lints = run_lints(parsed)
    if bad_name or bad_desc:
        lints.append(Lint(rule="bad-character", severity="warning", line=None,
                          message="The name or description holds a character that is not valid text; "
                          "it was replaced with �."))

    # File names on Linux may hold bytes that are not UTF-8; they arrive as
    # surrogates and could not be written into the label file.
    unscanned = [u.model_copy(update={"path": clean_text(u.path)[0]}) for u in parsed.unscanned]
    label = SkillLabel(
        name=name,
        source_path=clean_text(parsed.path)[0],
        source=clean_text(source)[0] if source is not None else None,
        content_hash=_content_hash(parsed),
        description=description,
        lints=lints,
        cost=context_cost(parsed),
        risk=regex_risk(parsed),
        resources=[clean_text(r)[0] for r in parsed.resources],
        report_card=None,
        ai_enriched=False,
        unscanned=unscanned,
        outside_refs=outside_paths(parsed),
    )
    # Any text from a file name (resources, costs, evidence, lint messages)
    # may hold bytes that are not UTF-8; clean every string once.
    return SkillLabel.model_validate(_clean_all(label.model_dump()))


def _clean_all(value):
    if isinstance(value, str):
        return clean_text(value)[0]
    if isinstance(value, list):
        return [_clean_all(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean_all(v) for k, v in value.items()}
    return value


def failed_label(path: str | Path, exc: BaseException, *, source: str | None = None) -> SkillLabel:
    """The label for a skill the parser could not read at all: it says the
    skill was not checked, names the error type only (never its text, which
    may quote the file), and claims nothing about risk."""
    p = Path(path)
    name, _ = clean_text(p.name)
    return SkillLabel(
        name=name or "skill",
        source_path=clean_text(p)[0],
        source=clean_text(source)[0] if source is not None else None,
        content_hash=None,
        description="",
        lints=[Lint(rule="could-not-read", severity="error", line=None,
                    message=f"Skillabel could not read this skill ({type(exc).__name__}), so it was not checked.")],
        risk=[],
        unscanned=[UnscannedFile(path=SKILL_FILENAME, reason=f"error while reading ({type(exc).__name__})")],
    )


def linked_folder_label(path: str | Path, *, source: str | None = None, command: str | None = None,
                        name: str | None = None) -> SkillLabel:
    """The label for a skill folder that is a link or junction. The link is
    never followed: nothing is read through it, not even its listing, so the
    label claims nothing and says it was not checked. It may not be a skill
    at all, and the finding says so. *command*, when given, is the command
    that labels it on purpose; it is added to the finding."""
    p = Path(path)
    name, _ = clean_text(name or p.name)
    how = (f" To label it on purpose, only if you know where the link points, run: {command}" if command
           else " Its path holds unusual characters, so no command is given; label it by hand." if command == ""
           else "")
    return SkillLabel(
        name=name or "skill",
        source_path=clean_text(p)[0],
        source=clean_text(source)[0] if source is not None else None,
        content_hash=None,
        description="",
        lints=[Lint(rule="linked-skill-folder", severity="warning", line=None,
                    message=f"`{name}` is a link or junction. Skillabel does not follow links, so this "
                    f"folder was not checked. It may hold a skill.{how}")],
        risk=[],
        unscanned=[UnscannedFile(path=SKILL_FILENAME, reason=LINKED_FOLDER_REASON)],
    )


def env_named_folder_label(path: str | Path, *, source: str | None = None) -> SkillLabel:
    """The label for a folder named like a `.env` file. It is never entered:
    nothing in it is opened, stat'ed or read, so the label claims nothing and
    says it was not checked, like the label for a linked folder."""
    p = Path(path)
    name, _ = clean_text(p.name)
    return SkillLabel(
        name=name or "skill",
        source_path=clean_text(p)[0],
        source=clean_text(source)[0] if source is not None else None,
        content_hash=None,
        description="",
        lints=[Lint(rule="env-named-folder", severity="warning", line=None,
                    message=f"`{name}` is named like a .env file. Skillabel never opens, stats or reads .env "
                    "files, so this folder was not checked. It may hold a skill.")],
        risk=[],
        unscanned=[UnscannedFile(path=SKILL_FILENAME, reason=ENV_FOLDER_REASON)],
    )


def safe_build(path: str | Path, *, source: str | None = None) -> tuple[SkillLabel, ParsedSkill | None]:
    """Build the label, or a "not checked" label if anything goes wrong.
    Returns the parse too (None on failure), so the AI step can reuse it."""
    try:
        parsed = parse_skill(path)
        return build_label(path, source=source, parsed=parsed), parsed
    except Exception as exc:  # one hostile skill must not stop the run
        return failed_label(path, exc, source=source), None
