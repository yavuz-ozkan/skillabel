# Skillabel GitHub Action

A composite action that labels the skills changed in a pull request and posts
**one sticky comment** with each skill's nutrition label. It reports surface
area with evidence — never a safe/unsafe verdict.

## What it does

On a PR it will:

1. Install `skillabel` from the action's own source (see [Install](#install)).
2. Make a fresh work folder, `$RUNNER_TEMP/skillabel`. Everything the action
   writes goes there, never into the checked-out pull request, so a link the
   pull request plants cannot redirect a write. It refuses a `path` input that
   is absolute, holds `..`, or goes through a link. Then `skillabel scan` the
   skills, writing labels to the work folder.
3. Render `skillabel-report.html` in the work folder and upload that file, and
   only that file, as a build artifact.
4. Post/update a single PR comment (marker `<!-- skillabel -->`) with a
   mini-label per changed skill: first the tool's own **Touches / Costs /
   Lints** lines, then **Fires when / Needs / Does / Produces** and — in
   `mode: full` — report-card grades, then the skill's description. Text taken
   from a skill is escaped so it shows as plain text. A word in it that GitHub
   would turn into a mention (`@name`), an issue or pull-request link (`#12`,
   `GH-7`, `owner/repo#9`) or a link (a URL) is shown as code, where GitHub
   does not notify or link. Not covered: a bare commit SHA, a repository's own
   custom autolinks, and emoji codes. Each value is cut at 500
   characters and each list at 20 entries. If the comment would pass GitHub's
   size limit, skill text is left out before any skill's Touches, Costs and
   Lints lines, and the comment says so. The comment is built by
   `python -I -m skillabel.comment`. In `mode: full`, the AI-written lines come
   under one line that marks them as an AI summary of the skill's own text.
   "No pattern matched" in the Touches line means no pattern matched in the
   text that was read; files that were not read are listed (see
   [What "checked" covers](../README.md#what-checked-covers)).
5. Fail the check if `skillabel lint` finds errors (or warnings when
   `strict: true`).

## Rollout note — start with `mode: lint`

`mode: lint` (the default) runs **only objective, deterministic checks**:
frontmatter, dead references, secret-ish strings, token cost, and the regex risk
pre-pass. No LLM, no judgment grades. This is the socially safe way to introduce
the tool to a team's skills repo — nobody's work gets an opinionated letter
grade on day one.

When the team is comfortable, switch to `mode: full` to add AI extraction and
the report card. `full` needs a provider key (below).

## Inputs

| Input | Default | Description |
|---|---|---|
| `path` | `.` | Directory to scan (a skill dir or a subtree with `SKILL.md` files). |
| `mode` | `lint` | `lint` = objective checks only. `full` = also AI extraction + report card. |
| `strict` | `false` | Fail on lint warnings too, not just errors. See [What `strict` adds](#what-strict-adds). |
| `model` | `claude-sonnet-5` | AI model id used when `mode: full`. |

### What `strict` adds

Without `strict`, only errors fail the check. A linked or unreadable `SKILL.md`
is an error, so it fails the gate either way. With `strict`, warnings fail it
too. Besides the writing lints, warnings cover:

- Files skillabel would not read: `linked-file` (a bundled link), `too-big`,
  `long-line`, `not-utf8`, `utf16-text`, `too-many-files`, `nested-too-deep`,
  `not-regular-file`, `could-not-read`.
- `lookalike-letters`: a word in `SKILL.md` mixes Latin letters with Cyrillic
  or Greek ones that look the same.
- `outside-reference`: the skill's text (frontmatter included) or a bundled
  `.md`, `.txt`, `.rst`, `.adoc`, `.yaml`, `.yml`, `.json`, `.toml` or `.html`
  file points with `..` to a file outside the skill folder, or to another
  machine. Skillabel does not read it; this is best effort, and scripts are not
  read for pointers. An honest pointer to a shared file next to the skill, such
  as `../shared/helpers.md`, counts too. A home, system or absolute path is the
  same finding as info, so it does not fail `strict`.
- Skill folders that were not checked: one that is a link or junction
  (`linked-skill-folder`) or one named like a `.env` file (`env-named-folder`).

Bundled text past the per-skill line budget is listed as not read, not as a
lint.

## Install

The action installs `skillabel` from its own checked-out source
(`$GITHUB_ACTION_PATH`), which is the code at the ref your `uses:` line names.
It never looks the name up on a package index. Skillabel is not published on
PyPI, so a package with that name there is not this project.

Its dependencies come from PyPI at the exact versions in `uv.lock`, each
checked against its hash, from wheels only (`--only-binary :all:`): `action-requirements.txt` (exported from `uv.lock`)
and `action-build-requirements.txt` (the hatchling build backend). The action
runs `pip install --require-hashes` on those two files, then installs its own
source with `--no-deps --no-build-isolation`. A test fails when those files and
`uv.lock` disagree. pip itself is the one that comes with `actions/setup-python`.

Referencing the action by ref is all it needs:

```yaml
- uses: yavuz-ozkan/skillabel@v0.3.0
```

A tag can be moved; a commit ID cannot. To be sure of the code you run, pin
the full commit ID that the tag points to, with the tag in a comment.

The same install done by hand in a workflow, if you prefer to pin it yourself:

```yaml
- uses: actions/checkout@v7
  with: { repository: yavuz-ozkan/skillabel, ref: v0.3.0, path: .skillabel-src }
- run: |
    pip install --require-hashes --only-binary :all: \
      -r .skillabel-src/action-requirements.txt -r .skillabel-src/action-build-requirements.txt
    pip install --no-deps --no-build-isolation ./.skillabel-src
```

A plain `pip install ./.skillabel-src` would take the newest versions its
ranges allow, not the locked ones.

## Minimal workflow (lint-only, no keys)

Do **not** add this to the skillabel repo itself — it ships the action but wires
it to nothing. Use it in the repo whose skills you want labeled:

```yaml
name: skillabel
on:
  pull_request:
    paths: ["skills/**"]
permissions:
  contents: read
  pull-requests: write   # required to post the sticky comment
jobs:
  label:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with: { fetch-depth: 0 }   # lets the action diff changed SKILL.md files
      - uses: yavuz-ozkan/skillabel@v0.3.0
        with:
          path: skills
          mode: lint
```

## Full mode (AI extraction + report card)

`mode: full` reads the provider key from the job environment automatically —
`skillabel` picks up `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, and
`SKILLABEL_AI_PROVIDER` from the process env. Pass your repo secret through
`env:`:

```yaml
      - uses: yavuz-ozkan/skillabel@v0.3.0
        with:
          path: skills
          mode: full
          strict: true
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          SKILLABEL_AI_PROVIDER: anthropic
          # or:
          # OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
          # SKILLABEL_AI_PROVIDER: openrouter
```

The scan runs inside the checked-out PR. Skillabel never reads a `.env`
file, so a `.env` in the pull request cannot supply a key or choose the
provider. Pass your key through `env:`, and set `SKILLABEL_AI_PROVIDER` next
to it, as above. The log shows one line naming the provider.

If no key is present, `full` degrades to keyless (deterministic label only) with
a single warning line — it never fails for a missing key.

## The sticky comment

The action finds its own prior comment, one written by `github-actions[bot]`
that contains the marker `<!-- skillabel -->`, via `gh api`, and **PATCHes it in
place**; if none exists it POSTs a new one. A comment by anyone else is never
edited, even if it holds the marker. The action uses the job's own token, so
its comments are by `github-actions[bot]`; with another token it would post a
new comment on every run. So a PR
carries exactly one Skillabel comment that updates on every push — no comment
spam. This needs `pull-requests: write` permission and runs only on
`pull_request` events.

## Notes

- `npm`-free — pure Python, installed with pip.
- The action never publishes or installs anything outside the runner.
- Changed-skill detection diffs the files against the PR base. A skill is
  "changed" when any file in its folder changed, not only its `SKILL.md`: a
  pull request that edits only a bundled script still gets that skill's
  section. A skill is also shown when a file outside its folder that it points
  to with `..` (an `outside-reference`, such as `../shared/helpers.md`) changed;
  its section says so. That target is worked out from the skill's folder by
  path arithmetic only, never opened; a pointer that leaves the repo changes
  nothing. With an absolute `path` input, only a pointer that stays inside the
  scanned folder counts. When no changed file is in any skill or pointed to by one, or the base cannot be found
  (e.g. shallow checkout), it labels every skill under `path`. Folder names
  with spaces, newlines or bytes that are not UTF-8 are kept whole.
- Run the action once per job: two runs in one job share `$RUNNER_TEMP/skillabel`.
- The scan and comment steps print text taken from skills, such as names.
  GitHub reads a log line that starts with `::` as a workflow command, so
  those steps first stop command processing with a random token
  (`::stop-commands::<token>`) and resume it when the step ends. A skill
  cannot fake a notice, a warning or an error in the job log.
- A skill whose `SKILL.md` is a link is not read and is a lint error, so it
  fails the lint gate. Commit the file itself, not a link to it.
- A skill folder that is itself a link is not entered. It gets its own
  section in the comment, "not checked: a link or junction, not followed",
  with a `linked-skill-folder` warning on its label. `skillabel lint` lists
  it as a warning too, so the lint gate passes by default and fails on it
  with `strict: true`. A skill folder named like a `.env` file is treated the
  same way, with an `env-named-folder` warning; it is never entered.
- Label files and the HTML report hold paths from the runner. Look before you
  share one.
