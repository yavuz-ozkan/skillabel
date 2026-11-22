---
name: md-to-docx
description: Convert Markdown files to Word documents (.docx) using pandoc. Use when the user asks to "convert this to Word", "export as docx", or names a .docx deliverable. Not for PDF output.
---

# Markdown → Word (.docx)

## When to use
- Fires on: "convert this markdown to Word", "export README.md as docx", any request naming a `.docx` deliverable.
- Skip when: the user wants PDF (use a PDF skill instead) or Google Docs.

## Requirements
- The `pandoc` CLI must be installed — verify with `pandoc --version`.
- Required input: the path to the source `.md` file.
- Optional input: a reference `.docx` template for styling.

## Steps
1. Verify pandoc is available: run `pandoc --version` (Bash).
2. Convert: run `pandoc <input>.md -o <output>.docx`, adding `--reference-doc <template>.docx` when a template was provided (Bash).
3. Report the output file path to the user.

For less-common pandoc flags, read [reference.md](reference.md). For batch
conversions of a whole directory, use the bundled script
`scripts/convert.py` (documented inline), which wraps step 2 per file.
