"""Skillabel command-line interface.

Three commands:

  scan <path> [--ai] [--model M] [--out DIR]
      Discover skills, build deterministic labels, optionally AI-enrich, and
      write `<out>/.skillabel/<slug>.json` (default out = <path>). A missing AI
      key with `--ai` prints one warning line and continues keyless.

  report <path> [--out FILE]
      Read `.skillabel/*.json` and render one self-contained HTML report
      (default `~/skillabel-reports/skillabel-report.html`, outside the
      scanned folder).

  lint <path> [--strict]
      Deterministic checks only (never AI). Rich table of findings; exit 1 on
      any `error`, or on warnings too when `--strict`.

The heavy scan/lint imports (skillabel.deterministic / .parse / .ai — built by
sibling agents) are deferred into the command bodies so that importing this
module, and the `report` command, never depend on them.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .discover import candidate_roots, discover_roots
from .providers import DEFAULT_MODEL, AIProviderNotConfiguredError
from .report import drift_summary, render, slugify
from .safeio import UnsafePathError, safe_mkdir, safe_write_text
from .terminal import safe
from .schema import SkillLabel
from .usage import UsageIndex, collect_usage, stores_present


# The AI provider and key come only from the process environment. Skillabel
# never opens, stats or reads a `.env` file, for any command (versions before
# this one read `./.env`; that file may belong to a stranger's repo).


def _say_provider() -> None:
    """One line naming the provider; its key always comes from your environment."""
    from .providers import describe_provider

    picked = describe_provider()
    if picked is None:
        return
    name, var = picked
    err_console.print(f"AI: provider {safe(name)}, key {safe(var)} from your environment", soft_wrap=True)


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    # A crash trace must not print the values of local variables, which can
    # hold text from a skill.
    pretty_exceptions_show_locals=False,
    help="Nutrition labels for agent skills — surface area with evidence, never verdicts.",
)
# Every value taken from a skill or from the file system is passed through
# terminal.safe() before it is printed; emoji codes such as :x: are off too.
console = Console(emoji=False)
err_console = Console(stderr=True, emoji=False)

_SEV_STYLE = {"error": "bold red", "warning": "yellow", "info": "dim"}


def _refuse(exc: UnsafePathError) -> None:
    """Stop with one clear line when a write would go through a link."""
    err_console.print(f"[bold red]Refused:[/bold red] {safe(str(exc))}", soft_wrap=True)
    raise typer.Exit(code=2)


def _cannot_write(exc: OSError) -> None:
    """Stop with one clear line when a file cannot be written."""
    where = f" {exc.filename}" if exc.filename else ""
    err_console.print(f"[bold red]Could not write{safe(str(where))}:[/bold red] {safe(str(exc.strerror or exc))}", soft_wrap=True)
    raise typer.Exit(code=1)


def _home_reports() -> Path:
    return Path.home() / "skillabel-reports"


def _inside(path: Path, folder: Path) -> bool:
    """Is *path* inside *folder* (or the same), however either is spelled?
    The letter case is folded where the file system ignores it (Windows),
    and other spellings (``\\\\?\\C:\\...``) are caught by the file system's id."""
    from .safeio import is_inside

    return is_inside(path, folder)


def _write_anchor(out: Path | None, paths: list[Path]) -> Path:
    """The folder whose links a `scan` write must not go through: the scanned
    folder that holds `--out`, when one does, else `--out` itself, else the
    first scanned folder."""
    if out is None:
        return paths[0]
    return next((p for p in paths if _inside(out, p)), out)


_SLUG_MAX = 60  # a long name must not make a file name the system refuses


def _slug_for(label: SkillLabel, skill_path: Path) -> str:
    """Slug = skill name if set, else the skill directory name; kebab-cased,
    then a short hash of the skill's own folder path, so two skills that share
    a name (one could hide behind the other) never share a label file."""
    digest = hashlib.sha1(str(skill_path).encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{slugify(label.name or skill_path.name)[:_SLUG_MAX].strip('-')}-{digest}"


def _source_slug(source: str | Path) -> str:
    """A filesystem-safe, per-source prefix so same-slug skills from different
    roots never clobber each other's label JSON. Combines the kebab-cased root
    name with a short stable hash of the full path (paths carry spaces,
    backslashes, and drive letters on Windows)."""
    text = str(source)
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:8]
    base = (slugify(Path(text).name)[:_SLUG_MAX].strip("-")) or "root"
    return f"{base}-{digest}"


def _known_slugs(labels: list[SkillLabel]) -> set[str]:
    """The display slugs the report keys usage by (mirrors the report card slug)."""
    return {slugify(lb.name or lb.source_path or "skill") for lb in labels}


def _print_usage_summary(usage: UsageIndex, labels: list[SkillLabel]) -> None:
    """Stderr usage rollup for `auto`: top-5 most-used, the loud never-used
    count, the harness split, unmatched invocations, and the Codex verdict."""
    by_harness = usage.totals_by_harness()
    never_used = len(_known_slugs(labels) - set(usage.skills))
    err_console.print(
        f"\n[bold]Usage[/bold] (local session logs) - "
        f"{usage.total_uses()} use(s) across {len(usage.skills)} skill(s) - "
        f"claude {by_harness['claude']} / codex {by_harness['codex']}"
    )
    top = usage.top_used(5)
    if top:
        rendered = ", ".join(f"{slug} ({u.total_uses})" for slug, u in top)
        err_console.print(f"  top used: {safe(rendered)}", soft_wrap=True)
    err_console.print(f"  [bold red]{never_used}[/bold red] skill(s) never used")
    if usage.unmatched_count():
        err_console.print(
            f"  [dim]{usage.unmatched_count()} unmatched invocation(s) "
            f"across {len(usage.unmatched)} unrecognized name(s)[/dim]"
        )
    codex_uses = by_harness["codex"]
    verdict = (
        f"detected - {codex_uses} use(s) via shell reads of SKILL.md"
        if codex_uses
        else "no detectable skill usage in Codex logs"
    )
    err_console.print(f"  Codex: {verdict}")


_AI_UNAVAILABLE_NOTE = (
    "[yellow]No AI provider configured "
    "(set ANTHROPIC_API_KEY or OPENROUTER_API_KEY); "
    "continuing with deterministic labels only.[/yellow]"
)


def _skills_under(root: Path, follows: bool = False,
                  env_named: list[Path] | None = None) -> tuple[list[Path], list[Path]]:
    """The skill folders under *root*, and the folder links that are not
    inside a skill folder (each may be a skill folder; it is never entered).
    A link inside a skill is a finding on that skill's own label. Every link
    is named on stderr so nothing is hidden quietly."""
    from .parse import discover_skills

    found_env: list[Path] = []
    skills, skipped = discover_skills(root, env_named=found_env)
    for link in skipped:
        if follows and link.parent == root:
            continue  # `auto` may follow it; the scan says what it did with it
        err_console.print(
            f"[yellow]Skipped a link or junction (not followed):[/yellow] {safe(str(link))}", soft_wrap=True
        )
    in_skill = set(skills)
    linked = [link for link in skipped if not any(p in in_skill for p in link.parents)]
    if env_named is not None:  # one inside a skill is that skill's own not-read entry
        env_named.extend(f for f in found_env if not any(p in in_skill for p in f.parents))
    return skills, linked


def _clear_old_labels(label_dir: Path) -> None:
    """Remove the ``*.json`` files in skillabel's own label folder, so only
    labels this scan writes are there. A link is removed as a link, never
    followed. Called after every label is built and the folder is checked."""
    from .safeio import is_link

    removed = 0
    for f in sorted(label_dir.glob("*.json")):
        try:
            if is_link(f) and os.name == "nt" and f.is_dir():
                os.rmdir(f)  # a junction or folder link named *.json (Windows)
            elif is_link(f) or f.is_file():
                os.unlink(f)
            else:
                continue
            removed += 1
        except OSError as exc:
            err_console.print(f"[yellow]Could not remove old label file[/yellow] {safe(f.name)} "
                              f"({safe(str(exc.strerror or exc))})", soft_wrap=True)
    if removed:
        err_console.print(f"[dim]Removed {removed} old label file(s) from {safe(str(label_dir))}.[/dim]",
                          soft_wrap=True)


def _load_labels(label_dir: Path) -> list[SkillLabel]:
    """The label files in *label_dir*: only regular files that are not links,
    up to a size cap. A file that is not a valid label is named and skipped,
    without echoing what is in it."""
    from .safeio import ReadRefused, is_link, read_capped

    if is_link(label_dir):
        raise UnsafePathError(f"{label_dir} is a link or junction, so skillabel will not read labels from it.")
    if not label_dir.is_dir():
        return []
    labels = []
    for f in sorted(label_dir.glob("*.json")):
        # Older versions wrote a usage sidecar next to the labels; never
        # mistake it for a label.
        if f.name == "usage.json":
            continue
        try:
            labels.append(SkillLabel.model_validate_json(read_capped(f, 16 << 20)))
        except ReadRefused as exc:
            err_console.print(f"[yellow]Skipped label file[/yellow] {safe(f.name)}: {safe(exc.reason)}", soft_wrap=True)
        except ValueError:
            err_console.print(f"[yellow]Skipped label file[/yellow] {safe(f.name)}: not a valid label", soft_wrap=True)
    return labels


def _holds_skill_md(folder: Path) -> bool:
    from .parse import is_skill_file_name

    try:
        return any(is_skill_file_name(n) for n in os.listdir(folder))
    except OSError:
        return False


def _real(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def _scan_roots(
    roots: list[Path], *, ai: bool, model: str, link_commands: bool = False,
    follow_roots: set[Path] | None = None, same_folder: list[Path] | None = None,
) -> list[tuple[Path, Path, SkillLabel]]:
    """Build a label for every skill under each root, tagging each label with the
    root it was found under. Shared by `scan` and `auto`. With `--ai`, a missing
    provider key prints one warning and the remaining skills stay keyless.
    Returns ``(root, skill_path, label)`` triples in discovery order.

    Only for a root in *follow_roots* (`auto` passes your own three home skill
    folders), a link that sits directly in that root and points to a folder
    with a SKILL.md is labelled as a skill. Nothing below it is followed. A
    followed link to a folder that is already labelled (the same skill
    installed twice) is not labelled again; it is added to *same_folder*."""
    from .deterministic import env_named_folder_label, linked_folder_label, safe_build

    follow_roots = follow_roots or set()
    env_named: dict[Path, list[Path]] = {root: [] for root in roots}
    found = [(root, *_skills_under(root, follows=root in follow_roots, env_named=env_named[root])) for root in roots]
    real_skills = {_real(s) for _root, skills, _linked in found for s in skills}
    followed_real: set[str] = set()

    results: list[tuple[Path, Path, SkillLabel]] = []
    ai_unavailable = False
    provider_said = False
    for root, skills, linked in found:
        links = set(linked)
        follow: set[Path] = set()
        if root in follow_roots:
            for link in linked:
                if link.parent == root and _holds_skill_md(link):
                    key = _real(link)
                    if key in real_skills or key in followed_real:
                        links.discard(link)
                        if same_folder is not None:
                            same_folder.append(link)
                        continue
                    followed_real.add(key)
                    follow.add(link)
                    err_console.print(f"[dim]Followed a link in your home skill folder:[/dim] {safe(str(link))}",
                                      soft_wrap=True)
        envs = set(env_named[root])
        for skill_path in sorted(skills + [lk for lk in linked if lk in links] + sorted(envs),
                                 key=lambda p: p.as_posix()):
            if skill_path in envs:
                # Named like a .env file: never entered, never read (decision 88).
                results.append((root, skill_path, env_named_folder_label(skill_path, source=str(root))))
                continue
            if skill_path in links and skill_path not in follow:
                # A skill folder that is a link: never followed, never read,
                # never sent to the AI. Its label says it was not checked.
                results.append((root, skill_path, linked_folder_label(
                    skill_path, source=str(root),
                    command=(label_command(skill_path) or "") if link_commands else None)))
                continue
            label, parsed = safe_build(skill_path, source=str(root))
            # A skill that was not read is never sent to the AI.
            if ai and not ai_unavailable and parsed is not None and parsed.read:
                from .ai import enrich

                if not provider_said:
                    _say_provider()
                    provider_said = True
                try:
                    label = asyncio.run(enrich(label, parsed, model=model))
                except AIProviderNotConfiguredError:
                    err_console.print(_AI_UNAVAILABLE_NOTE)
                    ai_unavailable = True
            results.append((root, skill_path, label))
    return results


_PLAIN_PATH_CHARS = frozenset(" ._-/\\")


def _plain_path(text: str) -> bool:
    """Only letters, digits, space and `. _ - / \\`, a colon only after a drive
    letter, and no backslash at the end: nothing a shell would read as code."""
    if not text or text.endswith("\\"):
        return False
    for i, ch in enumerate(text):
        if ch.isalnum() or ch in _PLAIN_PATH_CHARS:
            continue
        if ch == ":" and i == 1 and text[0].isalpha():
            continue
        return False
    return True


def label_command(path: Path) -> str | None:
    """The command that labels a skipped linked folder on purpose, writing
    its labels to your own reports folder (never through the link), or None
    when the path holds characters a shell could read as code."""
    out = _home_reports() / "linked" / f"{path.parent.name}-{path.name}"
    cmd = f'skillabel scan "{path}" --out "{out}" && skillabel report "{out}"'
    return cmd if _plain_path(str(path)) and _plain_path(str(out)) else None


MAX_SKIPPED_SHOWN = 15


def _print_skipped(labels: list[SkillLabel], same_folder: list[Path]) -> None:
    """Name and count every skill folder that was not checked because it is
    a link, with the command that labels one on purpose."""
    from .schema import linked_folder

    skipped = [lb for lb in labels if linked_folder(lb.unscanned)]
    if same_folder:
        err_console.print(f"{len(same_folder)} linked skill folder(s) in your home skill folders point to a "
                          "skill already labelled, so they were not labelled twice.", soft_wrap=True)
    if not skipped:
        return
    err_console.print(f"[yellow]{len(skipped)} skill folder(s) not checked: a link or junction, not followed.[/yellow] "
                      "To label one on purpose, run the command shown, only if you know where the link points:",
                      soft_wrap=True)
    for lb in skipped[:MAX_SKIPPED_SHOWN]:
        cmd = label_command(Path(lb.source_path))
        err_console.print(f"  {safe(lb.source_path)}", soft_wrap=True)
        err_console.print(f"    {safe(cmd)}" if cmd else
                          "    the path holds unusual characters; label it by hand", soft_wrap=True)
    if len(skipped) > MAX_SKIPPED_SHOWN:
        err_console.print(f"  ... and {len(skipped) - MAX_SKIPPED_SHOWN} more (each is in the report)")


@app.command()
def scan(
    paths: list[Path] = typer.Argument(
        ..., exists=True, help="One or more skill directories or repo roots to scan."
    ),
    ai: bool = typer.Option(False, "--ai", help="Enrich labels with AI extraction + report card."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="AI model id (when --ai)."),
    out: Path | None = typer.Option(
        None, "--out", help="Output directory (default: first path). JSON goes in <out>/.skillabel/."
    ),
) -> None:
    """Discover skills across one or more roots and write JSON artifacts to
    .skillabel/. Each label records its source root; label filenames are
    namespaced per source so same-slug skills from different roots never clobber.
    """
    results = _scan_roots(paths, ai=ai, model=model)
    if not results:
        # Old label files must not outlive a scan, even one that found nothing.
        old = (out or paths[0]) / ".skillabel"
        if old.is_dir():
            try:
                safe_mkdir(old, _write_anchor(out, paths))
                _clear_old_labels(old)
            except UnsafePathError as exc:
                _refuse(exc)
        joined = ", ".join(str(p) for p in paths)
        err_console.print(f"[yellow]No SKILL.md found under {safe(joined)}.[/yellow]")
        raise typer.Exit(code=0)

    anchor = _write_anchor(out, paths)
    out_dir = (out or paths[0]) / ".skillabel"
    written = 0
    try:
        safe_mkdir(out_dir, anchor)
        _clear_old_labels(out_dir)
        for root, skill_path, label in results:
            slug = _slug_for(label, skill_path)
            target = out_dir / f"{_source_slug(root)}__{slug}.json"
            safe_write_text(target, label.model_dump_json(indent=2), anchor)
            console.print(f"[green]*[/green] {safe(label.name)} -> {safe(target)}")
            written += 1
    except UnsafePathError as exc:
        _refuse(exc)
    except ValueError as exc:  # a label that cannot be written as text
        err_console.print(f"[bold red]Could not write a label:[/bold red] {safe(type(exc).__name__)}", soft_wrap=True)
        raise typer.Exit(code=1)
    except OSError as exc:
        _cannot_write(exc)

    console.print(f"Wrote {written} label(s) to {safe(out_dir)}")


@app.command()
def auto(
    ai: bool = typer.Option(False, "--ai", help="Enrich labels with AI extraction + report card."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="AI model id (when --ai)."),
    out: Path | None = typer.Option(
        None, "--out", help="Output HTML file (default: ~/skillabel-reports/skills-report.html)."
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the report in your browser (use --no-open to suppress)."
    ),
    usage: bool = typer.Option(
        True,
        "--usage/--no-usage",
        help="Answer 'which skills do I actually use?' from local session logs (default on when logs exist).",
    ),
    title: str = typer.Option("Skillabel — your installed skills", "--title", help="Report title."),
) -> None:
    """Discover every place your skills live, label them all, and open one report.

    Zero required arguments: probes ~/.claude/skills, ~/.agents/skills, the
    current project's ./.claude/skills, and plugin-installed skills, scans each,
    and writes one combined HTML report. Usage is read from local Claude Code and
    Codex session logs — only skill names and timestamps, nothing leaves the box.
    """
    skipped_roots: list[Path] = []
    roots = discover_roots(skipped=skipped_roots)
    for link in skipped_roots:
        err_console.print(
            f"[yellow]Skipped a skill folder reached through a link or junction (not followed):[/yellow] "
            f"{safe(str(link))}", soft_wrap=True)
    if not roots:
        err_console.print("[yellow]No skill roots found.[/yellow] Checked:")
        for candidate in candidate_roots(Path.home(), Path.cwd()):
            err_console.print(f"  {safe(candidate)}", soft_wrap=True)
        raise typer.Exit(code=1)

    from .deterministic import linked_folder_label
    from .discover import home_skill_roots

    # Links directly inside your own home skill folders are followed; the
    # roots are matched by their path as written, never by where they lead.
    same_folder: list[Path] = []
    results = _scan_roots(roots, ai=ai, model=model, link_commands=True,
                          follow_roots=set(home_skill_roots(Path.home())) & set(roots), same_folder=same_folder)
    # A skill folder `auto` skipped because it goes through a link gets a
    # "not checked" label too, so the report names it.
    for link in skipped_roots:
        results.append((link, link, linked_folder_label(
            link, source=str(link), command=label_command(link) or "", name=f"{link.parent.name}/{link.name}")))
    labels = [label for _root, _skill_path, label in results]

    per_root: dict[str, int] = {str(r): 0 for r in roots}
    errors = warnings = 0
    for root, _skill_path, label in results:
        per_root[str(root)] = per_root.get(str(root), 0) + 1
        errors += sum(1 for lint in label.lints if lint.severity == "error")
        warnings += sum(1 for lint in label.lints if lint.severity == "warning")

    usage_index: UsageIndex | None = None
    if usage and stores_present():
        usage_index = collect_usage(known_slugs=_known_slugs(labels))

    html = render(labels, title=title, usage=usage_index)
    out_file = out or (_home_reports() / "skills-report.html")
    try:
        safe_mkdir(out_file.parent)
        safe_write_text(out_file, html)
        if usage_index is not None:
            usage_index.write(out_file.parent / "usage.json")
    except UnsafePathError as exc:
        _refuse(exc)
    except OSError as exc:
        _cannot_write(exc)

    drift = drift_summary(labels)
    err_console.print(f"[bold]Skillabel[/bold] found skills in {len(roots)} location(s):")
    for root in roots:
        err_console.print(
            f"  {safe(root)}  [dim]- {per_root[str(root)]} skill(s)[/dim]", soft_wrap=True
        )
    err_console.print(
        f"\n{len(labels)} skill(s) total - "
        f"[bold red]{errors}[/bold red] lint error(s) - "
        f"[yellow]{warnings}[/yellow] lint warning(s)"
    )
    err_console.print(
        f"{drift['duplicate_groups']} duplicate(s) (identical) - "
        f"[bold red]{drift['drift_groups']}[/bold red] drifted across sources"
    )
    _print_skipped(labels, same_folder)
    if usage_index is not None:
        _print_usage_summary(usage_index, labels)
    err_console.print(f"\nReport: {safe(out_file)}", soft_wrap=True)

    if open_browser:
        webbrowser.open(out_file.resolve().as_uri())


@app.command()
def report(
    path: Path = typer.Argument(
        Path("."), exists=True, help="Path whose .skillabel/ holds the scanned labels."
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Output HTML file (default: ~/skillabel-reports/skillabel-report.html). "
        "A report written inside <path> carries no usage numbers.",
    ),
    usage: bool = typer.Option(
        True,
        "--usage/--no-usage",
        help="Overlay skill usage from local session logs (default on when logs exist).",
    ),
    title: str = typer.Option("Skillabel report", "--title", help="Report title."),
) -> None:
    """Render the scanned labels to a single self-contained HTML report."""
    label_dir = path / ".skillabel"
    try:
        labels = _load_labels(label_dir)
    except UnsafePathError as exc:
        _refuse(exc)
    if not labels:
        err_console.print(
            f"[yellow]No labels found in {safe(label_dir)}. Run `skillabel scan {safe(path)}` first.[/yellow]"
        )


    out_file = out or (_home_reports() / "skillabel-report.html")
    # A report saved inside the scanned folder may be committed and shared, so
    # it never carries this machine's usage numbers.
    if usage and _inside(out_file, path):
        usage = False
        err_console.print(
            "[dim]Usage left out: the report is saved inside the scanned folder.[/dim]"
        )

    usage_index: UsageIndex | None = None
    if usage:
        # Usage comes only from this machine's own logs. A usage.json found in
        # the scanned folder is never read: anyone could have written it.
        if stores_present():
            usage_index = collect_usage(known_slugs=_known_slugs(labels))
            # Usage is about this machine, so it never goes into the scanned
            # folder, whose label files may be committed and shared.
            home_sidecar = _home_reports() / "usage.json"
            try:
                safe_mkdir(home_sidecar.parent)
                usage_index.write(home_sidecar)
            except UnsafePathError as exc:
                _refuse(exc)
            except OSError as exc:
                err_console.print(
                    f"[yellow]Could not save the usage sidecar to {safe(home_sidecar)} ({safe(exc.strerror)}); "
                    "the report still shows usage.[/yellow]",
                    soft_wrap=True,
                )

    html = render(labels, title=title, usage=usage_index)

    try:
        if out is None:
            safe_mkdir(out_file.parent)
        safe_write_text(out_file, html, path)
    except UnsafePathError as exc:
        _refuse(exc)
    except OSError as exc:
        _cannot_write(exc)
    console.print(f"[green]Wrote[/green] {safe(out_file)} ({len(labels)} skill(s))")


@app.command()
def lint(
    path: Path = typer.Argument(
        ..., exists=True, help="A single skill directory or a repo root to lint."
    ),
    strict: bool = typer.Option(False, "--strict", help="Also fail on warnings, not just errors."),
) -> None:
    """Run deterministic lints (never AI). Exit 1 on errors; --strict adds warnings.

    A folder that may hold a skill but was not checked (a link or junction,
    or a folder named like a .env file) is listed as a warning, so --strict
    fails on it. It is never entered or read."""
    from .deterministic import env_named_folder_label, linked_folder_label, safe_build

    env_named: list[Path] = []
    skills, linked = _skills_under(path, env_named=env_named)
    not_checked = ([linked_folder_label(p) for p in linked]
                   + [env_named_folder_label(p) for p in env_named])
    if not skills and not not_checked:
        err_console.print(f"[yellow]No SKILL.md found under {safe(path)}.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(title="Skillabel lints", show_lines=False, header_style="bold")
    table.add_column("Rule", style="cyan", no_wrap=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Line", justify="right", no_wrap=True)
    table.add_column("Skill", style="magenta", no_wrap=True)
    table.add_column("Message")

    errors = 0
    warnings = 0
    findings = 0
    labels = [safe_build(skill_path)[0] for skill_path in skills] + not_checked
    for label in labels:
        for finding in label.lints:
            findings += 1
            if finding.severity == "error":
                errors += 1
            elif finding.severity == "warning":
                warnings += 1
            style = _SEV_STYLE.get(finding.severity, "white")
            table.add_row(
                safe(finding.rule),
                f"[{style}]{safe(finding.severity)}[/{style}]",
                str(finding.line) if finding.line is not None else "—",
                safe(label.name),
                safe(finding.message),
            )

    if findings:
        console.print(table)
    else:
        console.print("[green]No lint findings — clean.[/green]")

    console.print(
        f"\n{len(skills)} skill(s) - "
        + (f"{len(not_checked)} folder(s) not checked - " if not_checked else "")
        + f"[bold red]{errors}[/bold red] error(s) - "
        f"[yellow]{warnings}[/yellow] warning(s)"
    )

    failed = errors > 0 or (strict and warnings > 0)
    raise typer.Exit(code=1 if failed else 0)


if __name__ == "__main__":  # pragma: no cover
    app()
