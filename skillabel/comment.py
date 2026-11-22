"""Build the GitHub Action's sticky PR comment from the label files.

Run by the Action as ``python -I -m skillabel.comment <path> <mode> [<work>]``.
It reads the labels from ``<path>/.skillabel/*.json`` and the changed files
from ``<work>/changed_files.z`` (NUL-separated, from ``git diff -z``; an older
``changed_dirs.txt`` with one folder a line also works; a skill is shown when
any file in its folder changed), and writes
``<work>/comment.md``. ``<work>`` defaults to the current folder. The Action
passes its own fresh folder under ``$RUNNER_TEMP`` for both, so no file the
pull request planted is read or written through. A link there is refused.

Every value that comes from a label file can come from a skill written by a
stranger. So each one is made a single line, stripped of control and format
characters, and has every ASCII punctuation character backslash-escaped.
CommonMark shows an escaped character as itself, so honest text reads the
same, while skill text can no longer open an HTML comment, start a list item,
or make bold text. The tool's own lines (Touches, Costs, Lints) come first in
each skill's section, before any text taken from the skill.
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
import unicodedata
from pathlib import Path

from .schema import env_named_folder, linked_folder, skill_md_unread

MARKER = "<!-- skillabel -->"
BIG3 = ("runs_shell", "network", "sends_external")
RISK_LABEL = {
    "runs_shell": "runs shell", "network": "network", "sends_external": "sends external",
    "writes_files": "writes files", "reads_outside_skill": "reads outside skill",
    "installs_packages": "installs packages", "spawns_agents": "spawns agents",
    "handles_secrets": "handles secrets",
}
_PUNCT = frozenset(string.punctuation)
MAX_VALUE = 500  # characters per value taken from a label, before escaping
MAX_COMMENT = 60000  # GitHub refuses comments over 65,536 characters
MAX_ITEMS = 20  # entries shown per list (facts, steps, risk lines)


def _one_line(value: object) -> str:
    """Collapse all whitespace to single spaces and drop control (Cc) and
    format (Cf: bidi overrides, zero-width) characters."""
    chars = []
    for ch in str(value):
        cat = unicodedata.category(ch)
        if ch.isspace() or cat == "Cc":
            chars.append(" ")
        elif cat == "Cf":
            continue
        else:
            chars.append(ch)
    s = " ".join("".join(chars).split())
    # A very long value would push the comment past GitHub's size limit, so
    # the post fails and no comment appears. Keep each value short.
    return s if len(s) <= MAX_VALUE else s[:MAX_VALUE] + "…"


# A word GitHub would turn into a mention (@name, @org/team), an issue or
# pull-request link (#12, GH-7, owner/repo#9) or a link (a URL). The
# backslash escape does not stop that: GitHub finds them in the rendered
# text. It does not look inside a code span, so such a word becomes one.
_REF_RE = re.compile(r"@[A-Za-z0-9]|#[0-9]|(?i:gh-)[0-9]|://|(?i:www)\.")


def text(value: object) -> str:
    """Skill-derived text, safe to place in Markdown outside a code span. A
    word that could notify a person or link an issue is a code span."""
    words = []
    for word in _one_line(value).split(" "):
        if _REF_RE.search(word):
            words.append("`" + word.replace("`", "'") + "`")
        else:
            words.append("".join("\\" + ch if ch in _PUNCT else ch for ch in word))
    return " ".join(words)


def code(value: object) -> str:
    """Skill-derived text as a code span (backticks become ')."""
    return "`" + _one_line(value).replace("`", "'") + "`"


def _items(values: list, render) -> list[str]:
    """Render at most MAX_ITEMS entries of a list, then say how many are left."""
    values = list(values)
    shown = [render(v) for v in values[:MAX_ITEMS]]
    if len(values) > MAX_ITEMS:
        shown.append(f"… and {len(values) - MAX_ITEMS} more")
    return shown


def _chips(fact: dict | None) -> str:
    """Evidence line chips: `L12` in SKILL.md, `tidy.rb:L1` in a bundled file."""
    ev = (fact or {}).get("evidence") or {}
    where = f"{text(ev['file'])}:" if ev.get("file") else ""
    return " ".join(_items(ev.get("lines", []), lambda n: f"{where}L{text(n)}"))


def is_changed(src: str, changed: list[str]) -> bool:
    if not changed:
        return True
    s = src.replace("\\", "/").rstrip("/")
    return any(s == d or s.endswith("/" + d.rstrip("/")) or Path(s).name == Path(d).name for d in changed)


def _clean(text: str) -> str:
    """Lone surrogates (bytes in a file name that are not UTF-8) as U+FFFD,
    one per byte: the same clean the label's own paths get."""
    return "".join("�" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def _parts(path: str) -> tuple[str, ...]:
    """A path as its parts. A backslash separates parts only on Windows, where
    it is a separator; on Linux it can be part of a folder name."""
    p = path.replace("\\", "/") if os.name == "nt" else path
    return tuple(x for x in p.split("/") if x not in ("", "."))


def changed_holders(files: list[str]) -> set[tuple[str, ...]]:
    """Every folder that holds a changed path, and each changed path itself
    (git lists a committed link as one path), as tuples of parts."""
    out: set[tuple[str, ...]] = set()
    for name in files:
        parts = _parts(_clean(name))
        for k in range(1, len(parts) + 1):
            out.add(parts[:k])
    return out


def holds_a_change(src: str, holders: set[tuple[str, ...]]) -> bool:
    """Is the skill folder *src* a changed path, or does it hold one? *src*
    may be absolute while git names are relative, so its tail is matched."""
    parts = _parts(_clean(src))
    return any(parts[i:] in holders for i in range(len(parts)))


def points_to_a_change(src: str, refs: list, holders: set[tuple[str, ...]], root: str | None = None) -> str | None:
    """The first changed path that one of the skill's outside pointers
    (*refs*, paths from the skill folder such as ``../shared/helpers.md``)
    leads to, or None. Path arithmetic only: nothing is opened or followed. A
    pointer that climbs above the folder the scan ran from (it leaves the
    repo) changes nothing. When the scan path was absolute, the pointer must
    stay inside the scanned folder *root* (the label's ``source``); with no
    *root*, an absolute path cannot be worked out and changes nothing."""
    parts = list(_parts(_clean(src)))
    absolute = os.path.isabs(src) or (os.name == "nt" and len(src) > 1 and src[1] == ":")
    root_parts = tuple(_parts(_clean(root))) if absolute and isinstance(root, str) and root else None
    if absolute and not root_parts:
        return None
    for ref in refs or []:
        if not isinstance(ref, str):
            continue
        rp = list(_parts(_clean(ref)))
        ups = 0
        while ups < len(rp) and rp[ups] == "..":
            ups += 1
        rest = rp[ups:]
        if not ups or not rest or ".." in rest or ups > len(parts):
            continue
        joined = tuple(parts[:len(parts) - ups] + rest)
        if absolute:
            # git names are relative to the repo; match a tail that holds the
            # whole pointed-to part, and only for a pointer that stays inside
            # the scanned folder (so it cannot leave the repo).
            n = len(root_parts)
            if len(joined) <= n or joined[:n] != root_parts:
                continue
            hit = next((joined[i:] for i in range(len(joined) - len(rest) + 1) if joined[i:] in holders), None)
        else:
            hit = joined if joined in holders else None
        if hit:
            return "/".join(hit)
    return None


def load_labels(path: str | Path, changed: list[str], files: list[str] | None = None) -> list[dict]:
    """Label files under ``<path>/.skillabel``: regular files that are not
    links, up to a size cap. A file that cannot be read as a label is skipped.

    A label is shown when its skill's SKILL.md folder is in *changed* (as
    before), or when its folder holds any changed path in *files* (a bundled
    script, a link). With nothing changed in any skill, every label is shown."""
    from .safeio import ReadRefused, UnsafePathError, is_link, read_capped

    label_dir = Path(path) / ".skillabel"
    if is_link(label_dir):
        raise UnsafePathError(f"{label_dir} is a link or junction, so skillabel will not read labels from it.")
    found = sorted(label_dir.glob("*.json")) if label_dir.is_dir() else []
    all_labels = []
    for f in found:
        try:
            data = json.loads(read_capped(f, 16 << 20))
        except (ReadRefused, ValueError):
            print(f"skipped label file {f.name}: not a readable label", file=sys.stderr)
            continue
        if isinstance(data, dict):
            data.pop("_shown_because", None)  # only this step sets it, never a label file
            all_labels.append(data)
    holders = changed_holders(files or [])
    hit = [holds_a_change(str(d.get("source_path", "")), holders) for d in all_labels]
    # A skill is shown, too, when a file outside it that it points to changed
    # (a shared doc): the comment says why.
    for d, h in zip(all_labels, hit):
        if not h and holders:
            why = points_to_a_change(str(d.get("source_path", "")), d.get("outside_refs") or [], holders,
                                     d.get("source"))
            if why:
                d["_shown_because"] = why
    hit = [h or bool(d.get("_shown_because")) for d, h in zip(all_labels, hit)]
    if not changed and not any(hit):
        return all_labels  # nothing about a skill changed: show every skill, as before
    return [d for d, h in zip(all_labels, hit)
            if h or (changed and is_changed(str(d.get("source_path", "")), changed))]


def _tool_lines(lb: dict) -> list[str]:
    """The heading and the tool's own lines: Touches, Costs, Lints."""
    out = [f"### {code(lb.get('name', ''))}"]
    if lb.get("_shown_because"):
        out.append(f"- **Shown because:** a file it points to outside its folder changed: "
                   f"{code(lb['_shown_because'])}")
    if env_named_folder(lb.get("unscanned")):
        out.append("- **Touches:** _not checked: named like a .env file, not read (see Lints)_")
    elif linked_folder(lb.get("unscanned")):
        out.append("- **Touches:** _not checked: a link or junction, not followed; it may hold a skill (see Lints)_")
    elif skill_md_unread(lb.get("unscanned")):
        out.append("- **Touches:** _not checked: SKILL.md was not read (see Lints)_")
    if skill_md_unread(lb.get("unscanned")):
        lints = lb.get("lints") or []
        errs = sum(1 for x in lints if x.get("severity") == "error")
        warns = sum(1 for x in lints if x.get("severity") == "warning")
        out.append(f"- **Lints:** {errs} error(s), {warns} warning(s)")
        return out
    risks = lb.get("risk") or []
    present = _items([r for r in risks if r.get("present")],
                     lambda r: f"{code(RISK_LABEL.get(r['kind'], r['kind']))} {_chips(r)}".strip())
    checked = [RISK_LABEL.get(r["kind"], text(r["kind"])) for r in risks
               if r["kind"] in BIG3 and not r.get("present")]
    unscanned = lb.get("unscanned") or []
    touch = " · ".join(present) if present else "_nothing found_"
    if checked:
        if unscanned:
            touch += (f"  (no pattern matched in the files read, {len(unscanned)} not read: "
                      f"{', '.join(checked)})")
        else:
            touch += f"  (no pattern matched in SKILL.md text and bundled text files: {', '.join(checked)})"
    out.append("- **Touches:** " + touch)
    if unscanned:
        out.append("- **Not read:** " + ", ".join(_items(unscanned, lambda u: f"{code(u.get('path', ''))} ({text(u.get('reason', ''))})")))
    cost = lb.get("cost") or {}
    if cost:
        total = cost.get("description_tokens", 0) + cost.get("body_tokens", 0) \
            + sum(rf.get("tokens", 0) for rf in cost.get("reference_files", []))
        est = " (est)" if cost.get("estimate", True) else ""
        out.append(f"- **Costs:** ~{total} tokens{est}")
    lints = lb.get("lints") or []
    errs = sum(1 for x in lints if x["severity"] == "error")
    warns = sum(1 for x in lints if x["severity"] == "warning")
    out.append(f"- **Lints:** {errs} error(s), {warns} warning(s)")
    return out


AI_NOTE = ("- **AI summary:** _the lines below are an AI summary of the skill's own text, and the "
           "skill can steer it. The tool's own lines above do not use AI._")


def _skill_text_lines(lb: dict) -> list[str]:
    """What was taken from the skill, escaped, after the tool's lines. In
    `mode: full`, the AI-written lines come under one line saying so."""
    out = [AI_NOTE] if lb.get("ai_enriched") else []
    trig = lb.get("trigger") or {}
    fires = trig.get("fires_when") or []
    if fires:
        out.append("- **Fires when:** " + "; ".join(_items(fires, text)))
    elif not lb.get("ai_enriched"):
        out.append("- **Fires when:** _run `mode: full` for extraction_")
    inputs_ = lb.get("inputs") or []
    if inputs_:
        out.append("- **Needs:** " + ", ".join(_items(inputs_, lambda i:
            f"{text(i['name'])} ({text(i['kind'])}{'' if i.get('required', True) else ', optional'})")))
    flow = lb.get("flow") or []
    if flow:
        out.append("- **Does:** " + " ".join(_items(flow, lambda s: f"{text(s['n'])}\\. {text(s['action'])}")))
    outputs = lb.get("outputs") or []
    if outputs:
        out.append("- **Produces:** " + ", ".join(_items(outputs, lambda o: f"{text(o['what'])} ({text(o['kind'])})")))
    rc = lb.get("report_card")
    if rc:
        out.append("- **Report card:** " + " · ".join(_items(rc, lambda d: f"{text(d['name'])} {text(d['grade'])}")))
    if lb.get("description"):
        out.append("")
        out.append(text(lb["description"]))
    return out


TEXT_LEFT_OUT = "- _Text from the skill left out to stay under GitHub's size limit; see the HTML report artifact._"
SKILLS_LEFT_OUT = "_{n} more skill(s) left out to stay under GitHub's size limit; see the HTML report artifact._"


def build_comment(labels: list[dict], mode: str) -> str:
    """Every skill shown gets its Touches, Costs and Lints lines. When the
    comment would be too big, skill text is left out first; skills are left
    out only if their tool lines alone do not fit, and the comment says so."""
    head = [MARKER, "## Skillabel", ""]
    head.append("_Surface area with evidence, never verdicts — a flashlight, not a sandbox._")
    head.append("")
    if not labels:
        head.append("No changed skills to label.")
    footer = []
    if mode == "lint":
        footer.append("> Running in `mode: lint` (objective checks only, no judgment grades). "
                      "Switch to `mode: full` with a provider key for extraction + report card.")
    budget = MAX_COMMENT - len("\n".join(head + footer)) - len(SKILLS_LEFT_OUT) - len(TEXT_LEFT_OUT) - 20

    # Pass 1: tool lines for as many skills as fit, each with room kept for
    # the line that says its text was left out.
    note = len(TEXT_LEFT_OUT) + 1
    tools: list[list[str]] = []
    for lb in labels:
        part = _tool_lines(lb)
        size = len("\n".join(part)) + 2 + note
        if size > budget:
            break
        tools.append(part)
        budget -= size
    # Pass 2: skill text, in order, while it fits; else the left-out line.
    sections = []
    for lb, part in zip(labels, tools):
        extra = _skill_text_lines(lb)
        size = len("\n".join(extra)) + 1
        if extra and size <= budget + note:
            budget -= size - note
            sections.append(part + extra + [""])
        elif extra:
            sections.append(part + [TEXT_LEFT_OUT, ""])
        else:
            sections.append(part + [""])
    out = head + [line for s in sections for line in s]
    if len(tools) < len(labels):
        out += [SKILLS_LEFT_OUT.format(n=len(labels) - len(tools)), ""]
    return "\n".join(out + footer)


def changed_skill_dirs(raw: bytes) -> list[str]:
    """The skill folders among the NUL-separated file names from
    ``git diff -z --name-only``: every name whose last part is the skill file
    in any case, as its folder. Names with spaces or newlines stay whole."""
    dirs: list[str] = []
    for name in raw.decode("utf-8", errors="replace").split("\0"):
        if not name:
            continue
        head, _, last = name.rpartition("/")
        if last.lower() == "skill.md" and (head or ".") not in dirs:
            dirs.append(head or ".")
    return dirs


def changed_names(raw: bytes) -> list[str]:
    """Every name in the NUL-separated list from ``git diff -z``. Bytes that
    are not UTF-8 are kept one per byte (as the label's own paths are)."""
    return [n for n in raw.decode("utf-8", errors="surrogateescape").split("\0") if n]


def read_changed(work: Path) -> list[str]:
    """The changed skill folders (see :func:`read_changes`)."""
    return read_changes(work)[0]


def read_changes(work: Path) -> tuple[list[str], list[str]]:
    """The changed skill folders and every changed path, from
    ``<work>/changed_files.z`` (what the Action writes), else the folders from
    an older ``changed_dirs.txt`` (one folder a line, no paths). No file, or an
    empty one, means every skill. A link is refused."""
    from .safeio import ReadRefused, UnsafePathError, is_link, read_capped

    for name in ("changed_files.z", "changed_dirs.txt"):
        f = work / name
        if is_link(f):
            raise UnsafePathError(f"{f} is a link or junction, so skillabel will not read it.")
        if not f.exists():
            continue
        try:
            raw = read_capped(f, 64 << 20)
        except ReadRefused as exc:
            raise UnsafePathError(f"{f} was not read: {exc.reason}.") from None
        if name == "changed_files.z":
            return changed_skill_dirs(raw), changed_names(raw)
        return [c.strip() for c in raw.decode("utf-8", errors="replace").splitlines() if c.strip()], []
    return [], []


def main(argv: list[str] | None = None) -> int:
    from .safeio import UnsafePathError, safe_write_text

    args = sys.argv[1:] if argv is None else argv
    path, mode = args[0], args[1]
    work = Path(args[2]) if len(args) > 2 else Path(".")
    try:
        changed, files = read_changes(work)
        labels = load_labels(path, changed, files)
        safe_write_text(work / "comment.md", build_comment(labels, mode), work)
    except UnsafePathError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    print("comment.md written with", len(labels), "label(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
