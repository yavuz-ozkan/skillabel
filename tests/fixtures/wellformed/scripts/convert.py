"""Batch-convert every .md file in a directory to .docx via pandoc.

Usage: python convert.py <directory> [template.docx]
Wraps step 2 of the skill for many files; see SKILL.md.
"""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    directory = Path(sys.argv[1])
    template = sys.argv[2] if len(sys.argv) > 2 else None
    for md in directory.glob("*.md"):
        cmd = ["pandoc", str(md), "-o", str(md.with_suffix(".docx"))]
        if template:
            cmd += ["--reference-doc", template]
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
