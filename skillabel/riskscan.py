"""Deterministic risk pre-pass.

Surface area with evidence, never verdicts. A regex seed set scans the text of
SKILL.md and of every bundled text file the parser read; every hit becomes a
``RiskFlag(source="regex", present=True)`` carrying the matched line numbers, a
short quote, and the file (``evidence.file``; None means SKILL.md). This layer
runs keyless and its ``present=True`` flags survive AI disagreement (the wolf
rule): a skill whose prose claims safety while its body runs shell + network is
flagged regardless.

Absent kinds are omitted from the list EXCEPT the big three
(``runs_shell`` / ``network`` / ``sends_external``): when nothing matched we
still emit a ``present=False`` marker. That marker means "no pattern matched in
the text that was read", not "this skill cannot do it": a pattern can be dodged
(a word split in two, an encoded command), and files that were not read are
listed in ``SkillLabel.unscanned``.

Before matching, each line is folded: format characters (zero-width, bidi) are
dropped, NFKC turns fullwidth and styled letters into plain ones, and a small
table turns Cyrillic and Greek look-alike letters into the Latin ones they
imitate, so ``сurl`` with a Cyrillic ``с`` still matches ``curl``. Every pattern
runs in linear time (bounded repeats only).
"""

from __future__ import annotations

import re
import unicodedata

from .schema import Evidence, ParsedSkill, RiskFlag, RiskKind

# Known shell binaries and interpreters whose backticked/inline use implies a
# shell run.
_SHELL_BINARIES = (
    "bash|sh|zsh|pandoc|curl|wget|tar|git|rm|cp|mv|mkdir|chmod|cat|echo|"
    "python|python3|node|npm|npx|pip|cargo|apt|apt-get|make|sed|awk|grep|"
    "ruby|perl|php|deno|bun|uv|uvx|pwsh|powershell|ssh|scp"
)
_SCRIPT_EXT = r"(?:sh|bash|zsh|py|rb|js|mjs|cjs|ts|pl|php|ps1|bat|cmd)"

# kind -> compiled patterns; a line matching any pattern is a hit for that kind.
_RISK_PATTERNS: dict[RiskKind, list[re.Pattern[str]]] = {
    "runs_shell": [
        re.compile(r"\(Bash\)"),
        re.compile(r"\bsubprocess\b"),
        re.compile(r"\b(bash|sh)\b\s+\S", re.IGNORECASE),
        re.compile(rf"`\s*(?:{_SHELL_BINARIES})\b"),
        # "run curl ...", "execute python ..."
        re.compile(rf"\b(?:run|runs|execute|executes)\s+(?:{_SHELL_BINARIES})\b", re.IGNORECASE),
        # "run the helper tidy.rb with ruby"
        re.compile(rf"\b(?:run|runs|execute|executes)\b[^.\n]{{0,80}}\b[\w./-]{{1,100}}\.{_SCRIPT_EXT}\b", re.IGNORECASE),
        # shell calls in bundled code
        re.compile(r"\b(?:os\.)?system\s*\("),
        re.compile(r"\bpopen\b", re.IGNORECASE),
        re.compile(r"\bexec[lv]?p?e?\s*\("),
        re.compile(r"\bchild_process\b"),
        re.compile(r"\b(?:Start-Process|Invoke-Expression)\b", re.IGNORECASE),
        re.compile(r"\|\s*(?:ba|z)?sh\b"),
    ],
    "network": [
        re.compile(r"\b(curl|wget)\b", re.IGNORECASE),
        re.compile(r"\bWebFetch\b"),
        re.compile(r"\brequests\.", re.IGNORECASE),
        re.compile(r"\bhttpx\b", re.IGNORECASE),
        re.compile(r"https?://"),
        re.compile(r"\burllib\b"),
        re.compile(r"\bsocket\b"),
        re.compile(r"\bfetch\s*\("),
        re.compile(r"\baiohttp\b"),
        re.compile(r"\b(?:Invoke-WebRequest|Invoke-RestMethod|iwr)\b", re.IGNORECASE),
        re.compile(r"\b(?:ssh|scp)\s+\S"),
    ],
    # sends_external phrasing; gated on a URL existing in the same file (see
    # below), since the URL is often on the following line.
    "sends_external": [
        re.compile(r"\b(POST|webhook|upload)\b", re.IGNORECASE),
    ],
    "installs_packages": [
        re.compile(r"\bpip\s+install\b", re.IGNORECASE),
        re.compile(r"\bnpm\s+i(nstall)?\b", re.IGNORECASE),
        re.compile(r"\bcargo\s+install\b", re.IGNORECASE),
        re.compile(r"\bapt(-get)?\s+install\b", re.IGNORECASE),
    ],
    "handles_secrets": [
        re.compile(r"\$[A-Za-z_][A-Za-z0-9_]{0,100}(KEY|TOKEN|SECRET)\b"),
        re.compile(r"\bAPI[_ ]?key\b", re.IGNORECASE),
    ],
    "writes_files": [
        re.compile(r"~/"),
        re.compile(r"\bwrite[s]?\b[^.\n]{0,200}\bto\b", re.IGNORECASE),
        re.compile(r"(?<![\w`])/[A-Za-z0-9._-]{1,100}/"),  # absolute unix path
        re.compile(r"[A-Za-z]:\\"),  # absolute windows path
    ],
    "spawns_agents": [
        re.compile(r"\bsub-?agent\b", re.IGNORECASE),
        re.compile(r"\bAgent tool\b", re.IGNORECASE),
        re.compile(r"\bfan[-\s]?out\b", re.IGNORECASE),
        re.compile(r"\bspawn\b", re.IGNORECASE),
    ],
    "reads_outside_skill": [
        re.compile(r"\bread[s]?\b[^.\n]{0,200}~/", re.IGNORECASE),
        re.compile(r"\bread[s]?\b[^.\n]{0,200}(?<![\w`])/[A-Za-z0-9._-]{1,100}/", re.IGNORECASE),
    ],
}

# Deterministic output order.
_KIND_ORDER: tuple[RiskKind, ...] = (
    "runs_shell",
    "writes_files",
    "reads_outside_skill",
    "network",
    "sends_external",
    "installs_packages",
    "spawns_agents",
    "handles_secrets",
)

_ALWAYS_ASSERT: frozenset[RiskKind] = frozenset({"runs_shell", "network", "sends_external"})

_URL_RE = re.compile(r"https?://")

_QUOTE_MAX = 100

# Cyrillic and Greek letters that look like Latin ones.
_LOOKALIKE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j",
    "ѕ": "s", "ԁ": "d", "һ": "h", "ԛ": "q", "ԝ": "w", "ɡ": "g", "ӏ": "l", "к": "k", "м": "m",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
    "Т": "T", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "α": "a", "ο": "o", "ρ": "p", "ν": "v", "ι": "i", "κ": "k", "υ": "u", "χ": "x",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N",
    "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
})


def fold(line: str) -> str:
    """The line as the patterns see it: no format characters, NFKC, look-alike
    letters made Latin. Only for matching; quotes use the original line."""
    if line.isascii():
        return line
    line = "".join(ch for ch in line if unicodedata.category(ch) != "Cf")
    return unicodedata.normalize("NFKC", line).translate(_LOOKALIKE)


def _quote(line: str) -> str:
    q = line.strip()
    return q[:_QUOTE_MAX]


def _scan_file(lines: list[str], skip: frozenset = frozenset()) -> dict[RiskKind, list[int]]:
    hits: dict[RiskKind, list[int]] = {}
    folded = [fold(ln) for ln in lines]
    for i, line in enumerate(folded):
        for kind, patterns in _RISK_PATTERNS.items():
            if kind in skip:  # already found in an earlier file; its evidence is kept
                continue
            if any(p.search(line) for p in patterns):
                hits.setdefault(kind, []).append(i + 1)
    # sends_external requires exfil *phrasing* AND a URL somewhere in the same
    # file; the target URL frequently sits on the line after the phrasing.
    if "sends_external" in hits and not any(_URL_RE.search(ln) for ln in folded):
        del hits["sends_external"]
    return hits


def regex_risk(parsed: ParsedSkill) -> list[RiskFlag]:
    """Scan SKILL.md and the bundled text files and return regex risk flags
    (deterministic). A skill whose SKILL.md was not read gets no flags at all:
    not "present", not "absent"."""
    if not parsed.read:
        return []
    files: list[tuple[str | None, list[str]]] = [(None, parsed.lines)]
    for rel in sorted(parsed.texts):
        files.append((rel, parsed.texts[rel].split("\n")))

    # kind -> (file, lines) of the first file with a hit; SKILL.md comes first.
    found: dict[RiskKind, tuple[str | None, list[str], list[int]]] = {}
    for rel, lines in files:
        for kind, matched in _scan_file(lines, frozenset(found)).items():
            if kind not in found:
                found[kind] = (rel, lines, matched)

    flags: list[RiskFlag] = []
    for kind in _KIND_ORDER:
        if kind in found:
            rel, lines, matched = found[kind]
            flags.append(
                RiskFlag(
                    kind=kind,
                    present=True,
                    source="regex",
                    evidence=Evidence(lines=matched, quote=_quote(lines[matched[0] - 1]), file=rel),
                )
            )
        elif kind in _ALWAYS_ASSERT:
            flags.append(RiskFlag(kind=kind, present=False, source="regex", evidence=None))
    return flags


def lookalike_words(lines: list[str]) -> list[tuple[int, str]]:
    """(line number, word) for words that mix Latin letters with Cyrillic or
    Greek ones, a way to hide a command from a text search."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if line.isascii():
            continue
        for word in re.findall(r"\w+", line):
            latin = any("a" <= c.lower() <= "z" for c in word)
            other = any(unicodedata.name(c, "").startswith(("CYRILLIC", "GREEK")) for c in word)
            if latin and other:
                out.append((i + 1, word))
                break
    return out
