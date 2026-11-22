"""Text that is safe to print to a terminal.

Names, descriptions, lint messages and file paths can come from a skill
written by a stranger. Printed as they are, control characters could clear
the screen, move the cursor to fake a summary line, set the window title or
write to the clipboard, and Rich console markup such as ``[bold]`` or
``[link=...]`` would be obeyed (and a stray ``[/]`` makes Rich crash).

:func:`safe` shows every control, format and line-break character as a
visible escape (``\\x1b``, ``\\u202e``), so the text stays on one line and does
nothing, and then escapes Rich markup.
"""

from __future__ import annotations

import sys
import unicodedata

from rich.markup import escape

_SHOW = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Co", "Cn"})


def _encodable(ch: str) -> bool:
    """Can *ch* be written to this terminal? A Windows console or pipe may use
    an old code page, and one character it cannot encode would end the run."""
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None) or "utf-8"
        try:
            ch.encode(enc)
        except (UnicodeEncodeError, LookupError):
            return False
    return True


def plain(value: object) -> str:
    """*value* as one line with every control or format character made
    visible, and every character the terminal cannot encode written as an
    escape."""
    out = []
    for ch in str(value):
        if unicodedata.category(ch) in _SHOW or (not ch.isascii() and not _encodable(ch)):
            n = ord(ch)
            out.append(f"\\x{n:02x}" if n < 0x100 else f"\\u{n:04x}" if n < 0x10000 else f"\\U{n:08x}")
        else:
            out.append(ch)
    return "".join(out)


def safe(value: object) -> str:
    """*value* made :func:`plain`, then with Rich markup escaped."""
    return escape(plain(value))
