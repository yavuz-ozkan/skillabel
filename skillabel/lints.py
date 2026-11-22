"""Deterministic lints (stable rule ids).

Every rule is a pure, deterministic heuristic over the parsed skill. Line
numbers are attached wherever the offending text is locatable in SKILL.md.

Heuristics (documented so results are inspectable). Every pattern runs in
linear time on a line or body of any length (bounded repeats, no nested ones),
because a skill may be written to stall the checker.

- Findings about reading the skill (a linked file, a file too big, YAML
  aliases, text that is not UTF-8) come from the parser and are listed first.
  When SKILL.md itself was not read, they are the only findings: nothing else
  can be said about a file that was not read.

- ``missing-frontmatter-field`` (error): frontmatter lacks a non-empty ``name``
  or ``description``. One lint per missing field.
- ``dead-reference`` (error): a Markdown link target ``[..](path)`` — an explicit
  promise of a bundled file — does not exist on disk relative to the skill dir.
  URLs, anchors and placeholder tokens (containing ``<>{}*`` or whitespace) are
  ignored so ``<input>.md`` never fires.
- ``unbundled-reference`` (info): a *backticked* path token that is not bundled
  with the skill. Deliberately not an error: skills legitimately reference files
  in the target project (``CONTEXT.md``, ``CONTRIBUTING.md``) — the info note
  just makes the external dependency visible.
- ``outside-reference`` (warning or info): the frontmatter, the body, or a bundled
  instruction or data text file (``.md``, ``.txt``, ``.rst``, ``.adoc``, ``.yaml``, ``.yml``,
  ``.json``, ``.toml``, ``.html``; not scripts), points to a file outside the skill folder, in a link, a
  backticked span or plain prose. A path that climbs out with ``..``
  (counted from the file it is written in), a UNC path or a ``file:`` link is
  a warning: the skill's author may control that file. A home, system or
  absolute path (``~/x``, ``$HOME/x``, ``/etc/x``, ``C:\\x``) is info.
  Skillabel never reads such a file, so the finding says what it says was not
  checked. Best effort: a pointer can still be written in a way this does
  not see.
- ``secret-ish-string`` (error): a key-shaped literal — ``sk-…``, ``ghp_…``,
  ``xox[baprs]-…``, AWS ``AKIA…``, or a quoted ≥24-char high-entropy value
  (mixed letters+digits) — appears anywhere in the file.
- ``greedy-description`` (warning): the description contains neither a quoted
  trigger phrase (``"..."``) nor a ``use when``-style conditional. ``use
  whenever`` deliberately does NOT count (word boundary after ``when``).
- ``no-trigger-examples`` (warning): the body has neither a when-to-use /
  fires-on trigger section nor a quoted multi-word invocation example (a quoted
  phrase containing a space — so a bare secret literal does not qualify).
- ``oversized-body`` (warning): body token estimate exceeds the threshold
  (default 2500; tests pass a low threshold to trip a small fixture).
- ``lookalike-letters`` (warning): a word in SKILL.md mixes Latin letters
  with Cyrillic or Greek ones that look the same (``сurl``).
- ``undocumented-script`` (warning): a bundled executable (.sh/.py/.js/.ps1) is
  never mentioned (by relative path or basename) in the body.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .cost import estimate_tokens
from .riskscan import lookalike_words
from .schema import Lint, ParsedSkill

_SCRIPT_EXTENSIONS = (".sh", ".py", ".js", ".ps1")

# Markdown link target: [text](target). The link text may not hold another
# "[", so a run of "[" costs linear time.
_MD_LINK_RE = re.compile(r"\[[^\]\[]{0,2000}\]\(([^)\s]{1,2000})\)")
# A backticked token that looks like a real relative file path.
_CODE_PATH_RE = re.compile(r"`([A-Za-z0-9][A-Za-z0-9._/\-]{0,500}\.[A-Za-z0-9]{1,50})`")
# A quoted phrase. (The multi-word check splits on quotes instead of a regex.)
_QUOTED_RE = re.compile(r'"[^"]+"')
# "use when"-style conditional; \b after `when` excludes "whenever".
_USE_WHEN_RE = re.compile(
    r"\b(use (this )?(skill )?when\b|when the user\b|fires on\b|fires when\b|"
    r"triggers? when\b|when to use\b)",
    re.IGNORECASE,
)
_TRIGGER_SECTION_RE = re.compile(
    r"(when to use|when-to-use|fires on|fires when|##\s*when\b)", re.IGNORECASE
)

_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9-]{9,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
_HIGH_ENTROPY_RE = re.compile(r"""["']([A-Za-z0-9+/_=\-]{24,})["']""")

_PLACEHOLDER_CHARS = set("<>{}*| ")


def _find_line(lines: list[str], needle: str) -> int | None:
    for i, line in enumerate(lines):
        if needle in line:
            return i + 1
    return None


def _is_local_relative(target: str) -> bool:
    """A plain relative path inside the skill: no leading slash or backslash,
    no drive or scheme, no backslash at all, no '..' part, no NUL."""
    if not target or target[0] in "#/\\":
        return False
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", target):  # scheme:// (http, mailto, ...) or a drive
        return False
    if any(c in _PLACEHOLDER_CHARS for c in target):
        return False
    if "\\" in target or "\x00" in target or ":" in target:
        return False
    if ".." in target.split("/"):
        return False
    return True


def _has_quoted_multiword(body: str) -> bool:
    """True when some text between two neighbouring double quotes holds a
    space: the same answer as the old regex `"[^"]* [^"]*"`, in linear time."""
    parts = body.split('"')
    return any(" " in p for p in parts[1:-1])


# A skill may hold tens of thousands of links; past these caps the rest are
# summed up in one finding, so the check stays fast and the label small.
MAX_TARGETS = 2000  # link or path targets looked at, per kind
MAX_FINDINGS = 100  # findings shown per rule


class _Index:
    """The reader's file and folder lists as sets, built once per skill."""

    def __init__(self, parsed: ParsedSkill) -> None:
        self.names = set(parsed.resources) | set(parsed.folders)
        self.folded = {os.path.normcase(x) for x in self.names}
        self.skipped = [u.path.rstrip("/") for u in parsed.unscanned]


def _bundled(parsed: ParsedSkill, target: str, idx: "_Index | None" = None) -> bool | None:
    """Is *target* (a plain relative path) a bundled file or folder? Answered
    from what the safe reader listed, never by asking the file system, so a
    link cannot turn it into a question about a file outside the skill.
    None when the reader skipped it or a folder above it (no claim either way).
    `./` and empty parts are dropped first, so `./references/a.md` is the same
    file as `references/a.md`."""
    idx = idx or _Index(parsed)
    t = "/".join(p for p in target.split("/") if p not in ("", "."))
    if not t:
        return True  # the skill folder itself
    if t in idx.names or t.lower() == "skill.md":
        return True
    # On a file system that ignores case (Windows), the old check found
    # `Guide.md` for `guide.md` too; keep that.
    if os.path.normcase(t) in idx.folded:
        return True
    for p in idx.skipped:
        if p == "…" or t == p or t.startswith(p + "/"):
            return None
    return False


def _capped(rule: str, severity: str, what: str) -> Lint:
    return Lint(rule=rule, severity=severity, line=None,
                message=f"The skill has more than {MAX_TARGETS} {what}; only the first {MAX_TARGETS} were checked.")


def _more(rule: str, severity: str, n: int, what: str) -> Lint:
    return Lint(rule=rule, severity=severity, line=None,
                message=f"... and {n} more {what} (only the first {MAX_FINDINGS} are listed).")


# --- outside-reference ----------------------------------------------------------
# Instructions can live in a file outside the skill folder, which skillabel
# never reads. This finds pointers to such files, best effort: every word of
# the text (so prose, links, link titles, reference links, src= and href=
# values and backticked commands are all seen), and the inside of <...>, which
# may hold spaces. Each candidate is looked at as text only; the file is never
# opened or asked about.
_ANGLE_RE = re.compile(r"<([^<>\n]{1,2000})>")
_EDGE = "\"'`()[]<>{},;!?‘’“”"  # curly quotes too
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]{0,30}:")
_TILDE_USER_RE = re.compile(r"^~[A-Za-z0-9_.\-]{1,64}/")
_HOME_VAR_RE = re.compile(r"^(\$home|\$\{home\}|%userprofile%|%homepath%|\$env:userprofile)(/|$)")
# Bundled text an agent may be told to read or follow. Scripts (.py, .sh,
# .js, ...) are not read for pointers: code uses `../` paths honestly.
_INSTRUCTION_FILES = (".md", ".markdown", ".txt", ".rst", ".adoc", ".yaml", ".yml", ".json", ".toml",
                      ".html", ".htm")
MAX_TARGET_SHOWN = 200


def _candidate(word: str) -> str:
    """A word cut down to the path it may hold. Format characters (zero-width
    space, joiner, byte order mark) are dropped from this word only, as the
    risk scan drops them from its lines; the file text and the other lints are
    left as they are."""
    import unicodedata

    w = "".join(ch for ch in word if unicodedata.category(ch) != "Cf").strip(_EDGE)
    if "](" in w:  # [text](target) written without spaces
        w = w.rsplit("](", 1)[1].strip(_EDGE)
    low = w.lower()
    for prefix in ("src=", "href="):
        if low.startswith(prefix):
            w = re.split(r"[\"'>]", w[len(prefix):].strip(_EDGE), maxsplit=1)[0]
            break
    w = w.lstrip("@")  # @../x.md, how some agents include a file
    # A sentence's own punctuation after the path: `[x](../a.md).`,
    # `(see ../a.md).`, `"../a.md".` Drop a final `:` or a single `.` and the
    # edge characters it hid, until nothing changes (`..` stays: it is a path).
    # A trailing `...` is an ellipsis, and Markdown emphasis around the whole
    # path (`*../a.md*`, `**../a.md**`, `_../a.md_`) is not part of it.
    # At most 8 passes, so a huge word of punctuation stays linear in time.
    for _ in range(8):
        before = w
        if w.endswith("..."):
            w = w.rstrip(".")
        elif w.endswith(":") or (w.endswith(".") and not w.endswith("..")):
            w = w[:-1]
        w = w.strip(_EDGE).strip("*")
        if len(w) > 2 and w.startswith("_") and w.endswith("_"):
            w = w.strip("_")
        if w == before:
            break
    return w.split("#", 1)[0] if not w.startswith("#") else ""


_POINTER_CHARS = frozenset("/\\~$%:")


def leaves_skill(target: str, depth: int = 0) -> str | None:
    """Does *target*, read as a path from a file *depth* folders below the
    skill folder, point outside the skill? Returns the kind (``climb``,
    ``network``, ``home``, ``absolute``) or None. Text only; never asks the
    file system."""
    from urllib.parse import unquote

    if not target or len(target) > 4000:
        return None
    t = unquote(target).replace("\\", "/")
    low = t.lower()
    if _HOME_VAR_RE.match(low):
        return "home"
    if re.match(r"^[a-z]:/", low):  # a drive path
        return "absolute"
    if _SCHEME_RE.match(low):
        return "network" if low.startswith("file:") else None
    if t.startswith("~/") or _TILDE_USER_RE.match(t):
        return "home"
    if not t.startswith("/") and ".." in t and "/" in t:
        # A climb out counts even with glob or placeholder characters in it
        # (`../../secret*.md`): an agent can follow a glob.
        for part in t.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                depth -= 1
                if depth < 0:
                    return "climb"
            else:
                depth += 1
        return None
    if any(c in "<>{}*|" for c in t):  # a placeholder such as /<path>
        return None
    if t.startswith("//"):  # a UNC path //host/share/...
        return "network" if len([p for p in t.split("/") if p]) >= 2 else None
    if t.startswith("/"):  # an absolute path; one part (/help) is a slash command
        return "absolute" if len([p for p in t.split("/") if p]) >= 2 else None
    return None  # a relative path with no `..` part, or a bare `..`


def _maybe_pointer(word: str) -> bool:
    """A cheap test, linear in the word, before any parsing."""
    return ".." in word or any(c in _POINTER_CHARS for c in word)


def outside_targets(text: str, depth: int = 0) -> list[tuple[str, str]]:
    """The pointers in *text* that leave the skill folder, as (target, kind),
    in order, once each. *depth* is how many folders below the skill folder
    the text's own file sits, so `../SKILL.md` in `references/a.md` stays
    inside. Look-alike characters (such as a fullwidth dot) are folded first."""
    import unicodedata

    text = unicodedata.normalize("NFKC", text)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(c: str) -> None:
        if c and c not in seen:
            seen.add(c)
            kind = leaves_skill(c, depth)
            if kind:
                found.append((c, kind))

    def add_word(c: str) -> None:
        # The whole word, and, for `--config=../x` or `key=../x`, the value
        # after the last `=` too (a path may itself hold `=`).
        add(c)
        if "=" in c and "://" not in c:
            add(_candidate(c.rsplit("=", 1)[1]))

    for m in _ANGLE_RE.finditer(text):
        if _maybe_pointer(m.group(1)):
            add_word(_candidate(m.group(1).strip()))
    # A target with a space, found inside <...>, is named whole; its first
    # word alone is not named again.
    spaced = [f for f, _k in found if " " in f][:MAX_FINDINGS]
    for word in text.split():
        if not _maybe_pointer(word):
            continue
        c = _candidate(word)
        if not c or (spaced and any(s.startswith(c) for s in spaced)):
            continue
        add_word(c)
    return found


# A pointer that climbs out of the folder, or to another machine, names a
# file the skill's author may control: a warning. A home, system or absolute
# path is common in honest setup text: info, so it is shown and does not fail
# `strict` on its own.
_OUTSIDE_SEVERITY = {"climb": "warning", "network": "warning", "home": "info", "absolute": "info"}


FRONTMATTER = "\0frontmatter"  # a key no bundled file name can be


def _frontmatter_text(parsed: ParsedSkill) -> str:
    """The raw frontmatter lines of SKILL.md (between the `---` lines): the
    description is the one part an agent always has in its context."""
    if parsed.body_start_line <= 1:
        return ""
    return "\n".join(parsed.lines[1:parsed.body_start_line - 2])


def _outside_sources(parsed: ParsedSkill) -> list[tuple[str, str]]:
    """(where, text) for every text checked for outside pointers: the
    frontmatter, the SKILL.md body, and the bundled instruction files read."""
    return [(FRONTMATTER, _frontmatter_text(parsed)), ("", parsed.body)] + sorted(
        (rel, txt) for rel, txt in parsed.texts.items() if rel.lower().endswith(_INSTRUCTION_FILES))


MAX_OUTSIDE_PATHS = 200


def outside_paths(parsed: ParsedSkill) -> list[str]:
    """Where the pointers that climb out of the skill folder lead, as paths
    from the skill folder (``../shared/helpers.md``), worked out with path
    arithmetic only: the target is never opened, stat'ed or followed. Only
    `..` climbs count; home, absolute and network pointers are left out, since
    they cannot name a file in the same repo. Used by the PR comment to show a
    skill when a file it points to changed."""
    from urllib.parse import unquote

    if not parsed.read:
        return []
    out: list[str] = []
    for rel, txt in _outside_sources(parsed):
        base = [] if rel in ("", FRONTMATTER) else rel.split("/")[:-1]
        for target, kind in outside_targets(txt, len(base)):
            if kind != "climb":
                continue
            t = re.split(r"[?#]", unquote(target).replace("\\", "/"), maxsplit=1)[0]
            stack, ups = list(base), 0
            for part in t.split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    if stack:
                        stack.pop()
                    else:
                        ups += 1
                else:
                    stack.append(part)
            path = "/".join([".."] * ups + stack)
            if ups and stack and path not in out:
                out.append(path)
                if len(out) >= MAX_OUTSIDE_PATHS:
                    return out
    return out


def _outside_lints(parsed: ParsedSkill) -> list[Lint]:
    out: list[Lint] = []
    more = 0
    for rel, txt in _outside_sources(parsed):
        in_skill_md = rel in ("", FRONTMATTER)
        depth = 0 if in_skill_md else rel.count("/")  # folders between the skill folder and this file
        for target, kind in outside_targets(txt, depth):
            if len(out) >= MAX_FINDINGS:
                more += 1
                continue
            shown = target if len(target) <= MAX_TARGET_SHOWN else target[:MAX_TARGET_SHOWN] + "…"
            where = "The frontmatter" if rel == FRONTMATTER else "Body" if rel == "" else f"`{rel}`"
            out.append(Lint(
                rule="outside-reference", severity=_OUTSIDE_SEVERITY[kind],
                line=_find_line(parsed.lines, target) if in_skill_md else None,
                message=f"{where} points to `{shown}`, outside the skill folder. Skillabel did not read it, "
                        "so what it says was not checked."))
    if more:
        out.append(_more("outside-reference", "info", more, "pointers to files outside the skill"))
    return out


def _high_entropy(value: str) -> bool:
    return bool(re.search(r"[A-Za-z]", value)) and bool(re.search(r"[0-9]", value))


def run_lints(parsed: ParsedSkill, *, oversized_body_tokens: int = 2500) -> list[Lint]:
    """Run every lint rule against *parsed*; deterministic, order-stable."""
    lints: list[Lint] = list(parsed.problems)
    if not parsed.read:
        return lints
    fm = parsed.frontmatter
    body = parsed.body
    lines = parsed.lines

    # missing-frontmatter-field --------------------------------------------
    for field in ("name", "description"):
        if not str(fm.get(field) or "").strip():
            lints.append(
                Lint(
                    rule="missing-frontmatter-field",
                    severity="error",
                    line=None,
                    message=f"Frontmatter is missing a `{field}`.",
                )
            )

    # dead-reference / unbundled-reference ----------------------------------
    # A Markdown link promises a bundled file -> missing = error. A backticked
    # path mention may point at the *target project* (CONTEXT.md etc.) ->
    # missing = info, never an error.
    seen: set[str] = set()
    idx = _Index(parsed)
    capped = False
    shown = more = looked = 0
    for target in _MD_LINK_RE.findall(body):
        target = target.split("#", 1)[0]
        if target in seen or not _is_local_relative(target):
            continue
        seen.add(target)
        looked += 1
        if looked > MAX_TARGETS:
            capped = True
            break
        if _bundled(parsed, target, idx) is False:
            shown += 1
            if shown > MAX_FINDINGS:
                more += 1
                continue
            lints.append(
                Lint(
                    rule="dead-reference",
                    severity="error",
                    line=_find_line(lines, target),
                    message=f"Body links to `{target}`, which does not exist in the skill.",
                )
            )
    if more:
        lints.append(_more("dead-reference", "error", more, "links to files that do not exist"))
    if capped:
        lints.append(_capped("dead-reference", "error", "links"))
    capped = False
    shown = more = looked = 0
    for target in _CODE_PATH_RE.findall(body):
        target = target.split("#", 1)[0]
        if target in seen or not _is_local_relative(target):
            continue
        seen.add(target)
        looked += 1
        if looked > MAX_TARGETS:
            capped = True
            break
        if _bundled(parsed, target, idx) is False:
            shown += 1
            if shown > MAX_FINDINGS:
                more += 1
                continue
            lints.append(
                Lint(
                    rule="unbundled-reference",
                    severity="info",
                    line=_find_line(lines, target),
                    message=f"Body mentions `{target}`, which is not bundled with the "
                    "skill — fine if it refers to a file in the target project.",
                )
            )
    if more:
        lints.append(_more("unbundled-reference", "info", more, "paths that are not bundled"))
    if capped:
        lints.append(_capped("unbundled-reference", "info", "backticked paths"))

    # outside-reference -----------------------------------------------------
    lints.extend(_outside_lints(parsed))

    # secret-ish-string -----------------------------------------------------
    secret_lines: set[int] = set()
    for i, line in enumerate(lines):
        hit = any(rx.search(line) for rx in _SECRET_RES)
        if not hit:
            for m in _HIGH_ENTROPY_RE.finditer(line):
                if _high_entropy(m.group(1)):
                    hit = True
                    break
        if hit:
            secret_lines.add(i + 1)
    secret_sorted = sorted(secret_lines)
    if len(secret_sorted) > MAX_FINDINGS:
        lints.append(_more("secret-ish-string", "error", len(secret_sorted) - MAX_FINDINGS,
                           "lines with key-shaped literals"))
    for ln in secret_sorted[:MAX_FINDINGS]:
        lints.append(
            Lint(
                rule="secret-ish-string",
                severity="error",
                line=ln,
                message="A key-shaped / high-entropy literal is committed in the skill.",
            )
        )

    # greedy-description ----------------------------------------------------
    description = str(fm.get("description") or "")
    if description.strip():
        has_quoted = bool(_QUOTED_RE.search(description))
        has_conditional = bool(_USE_WHEN_RE.search(description))
        if not has_quoted and not has_conditional:
            lints.append(
                Lint(
                    rule="greedy-description",
                    severity="warning",
                    line=_find_line(lines, "description:"),
                    message="Description has no quoted trigger phrase or `use when` "
                    "condition; it will overtrigger.",
                )
            )

    # no-trigger-examples ---------------------------------------------------
    if not _TRIGGER_SECTION_RE.search(body) and not _has_quoted_multiword(body):
        lints.append(
            Lint(
                rule="no-trigger-examples",
                severity="warning",
                line=None,
                message="Body has no when-to-use section or quoted example invocations.",
            )
        )

    # oversized-body --------------------------------------------------------
    body_tokens = estimate_tokens(body)
    if body_tokens > oversized_body_tokens:
        lints.append(
            Lint(
                rule="oversized-body",
                severity="warning",
                line=None,
                message=f"Body is ~{body_tokens} tokens (> {oversized_body_tokens}); "
                "push detail into on-demand reference files.",
            )
        )

    # lookalike-letters -----------------------------------------------------
    for ln, _word in lookalike_words(lines)[:20]:
        lints.append(
            Lint(
                rule="lookalike-letters",
                severity="warning",
                line=ln,
                message="A word mixes Latin letters with Cyrillic or Greek look-alikes, a way to hide "
                "a command from a text search. The risk check reads it as the Latin word.",
            )
        )

    # undocumented-script ---------------------------------------------------
    for rel in parsed.resources:
        if not rel.lower().endswith(_SCRIPT_EXTENSIONS):
            continue
        basename = rel.rsplit("/", 1)[-1]
        if rel not in body and basename not in body:
            lints.append(
                Lint(
                    rule="undocumented-script",
                    severity="warning",
                    line=None,
                    message=f"Bundled script `{rel}` is never mentioned in the body.",
                )
            )

    return lints
