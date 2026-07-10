"""CLI entry point for media-mate.

Wires together the capability modules via Click commands. The default audit
log lives at ~/.media-mate/media-mate.db and is created on first use.

Layout:
    media-mate probe <path>
    media-mate organize <path> --root <root>
    media-mate proxy <path> --out <dir>
    media-mate resolve create <path> --project <name> [...]
    media-mate verify <path>
    media-mate log
    media-mate run <path> [--organize --proxy --resolve-project --verify]
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from media_mate import __version__
from media_mate.config import load_config
from media_mate.log import LogStore
from media_mate.models import MediaMateConfig, ResolveProjectSpec
from media_mate.organize import organize_path
from media_mate.probe import probe_path
from media_mate.proxy import generate_proxies
from media_mate.resolve import create_resolve_project
from media_mate.verify import verify_folder

DEFAULT_DB_PATH = Path.home() / ".media-mate" / "media-mate.db"


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="media-mate")
@click.option(
    "--db",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    envvar="MEDIA_MATE_DB",
    help=f"Audit log SQLite database (default: {DEFAULT_DB_PATH})",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    envvar="MEDIA_MATE_CONFIG",
    help="Path to media-mate.toml config file.",
)
@click.pass_context
def main(ctx: click.Context, db: Path, config_path: Path | None) -> None:
    """media-mate: zero-cost CLI for post-production media ops."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db
    ctx.obj["config"] = load_config(config_path)


def _get_store(ctx: click.Context) -> LogStore:
    """Get or initialize the LogStore from the Click context."""
    db_path: Path = ctx.obj["db_path"]
    store = LogStore(db_path)
    store.initialize()  # idempotent
    return store


def _get_config(ctx: click.Context) -> MediaMateConfig:
    """Get the loaded MediaMateConfig from the Click context."""
    cfg: MediaMateConfig = ctx.obj["config"]
    return cfg


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def probe(ctx: click.Context, path: Path) -> None:
    """Probe a file or directory and write results to the audit log."""
    store = _get_store(ctx)
    results = probe_path(path, store)

    console = Console()
    console.print(f"[green]Probed {len(results)} file(s)[/green]")
    if results:
        with console.status("[bold]Showing first 10...[/bold]"):
            table = Table(show_header=True, header_style="bold")
            table.add_column("File")
            table.add_column("Codec")
            table.add_column("Resolution")
            table.add_column("Duration")
            for r in results[:10]:
                codec = r.video_codec or r.audio_codec or "?"
                res = f"{r.width}x{r.height}" if r.width else "?"
                dur = f"{r.duration_seconds:.1f}s" if r.duration_seconds else "?"
                table.add_row(r.path, codec, res, dur)
            console.print(table)
        if len(results) > 10:
            console.print(f"[dim]... and {len(results) - 10} more[/dim]")


# ---------------------------------------------------------------------------
# organize
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    required=True,
    help="Destination root for organized output.",
)
@click.option(
    "--move",
    "do_move",
    is_flag=True,
    default=False,
    help="Move files instead of copying (raw folder is left intact by default).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what would be organized without moving or copying any files.",
)
@click.pass_context
def organize(ctx: click.Context, path: Path, root: Path, do_move: bool, dry_run: bool) -> None:
    """Organize media files into a structured folder layout.

    Sources are copied by default; pass --move to relocate them.
    """
    store = _get_store(ctx)
    cfg = _get_config(ctx)
    result = organize_path(path, root, store, config=cfg, move=do_move or None, dry_run=dry_run)

    console = Console()
    if result.files_moved == 0 and result.files_skipped == 0:
        console.print("[yellow]No files to organize[/yellow]")
        return
    moved = do_move or cfg.organize.mode == "move"
    verb = "Moved" if moved else "Copied"
    console.print(
        f"[green]{verb} {result.files_moved} file(s)[/green], "
        f"[yellow]skipped {result.files_skipped}[/yellow], "
        f"{result.bytes_moved:,} bytes total"
    )
    if result.dry_run:
        console.print("[dim](dry run — no files were actually moved)[/dim]")
    if result.errors:
        console.print("[red]Errors:[/red]")
        for err in result.errors[:5]:
            console.print(f"  {err}")
        if len(result.errors) > 5:
            console.print(f"  ... and {len(result.errors) - 5} more")
    if result.span_warnings:
        console.print("[yellow]Spanned clip warnings:[/yellow]")
        for w in result.span_warnings:
            console.print(f"  {w}")


# ---------------------------------------------------------------------------
# proxy
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--out",
    "output_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for proxies (subpaths preserved).",
)
@click.pass_context
def proxy(ctx: click.Context, path: Path, output_dir: Path) -> None:
    """Generate edit-friendly proxy files via ffmpeg."""
    store = _get_store(ctx)
    cfg = _get_config(ctx)
    batch = generate_proxies(path, output_dir, store, config=cfg)

    console = Console()
    console.print(f"[green]Generated {len(batch.results)} proxy file(s)[/green]")
    if batch.skipped:
        console.print(f"[dim]Skipped {len(batch.skipped)} non-video file(s)[/dim]")
    if batch.already_existed:
        console.print(f"[dim]Already existed {len(batch.already_existed)} file(s) (no-op)[/dim]")
    if batch.failures:
        console.print(f"[red]Failed {len(batch.failures)} file(s):[/red]")
        for failure in batch.failures[:5]:
            console.print(f"  {Path(failure.source_path).name}: {failure.reason}")
        if len(batch.failures) > 5:
            console.print(f"  ... and {len(batch.failures) - 5} more")
    if not batch.results and not batch.failures:
        console.print("[yellow]No video files found to proxy.[/yellow]")


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


@main.group()
def resolve() -> None:
    """DaVinci Resolve project commands."""


@resolve.command("create")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--project", required=True, help="Project name.")
@click.option(
    "--resolution",
    default="1080",
    type=click.Choice(["1080", "4K", "720"]),
    help="Project resolution.",
)
@click.option(
    "--fps",
    "frame_rate",
    default="24",
    type=click.Choice(["23.976", "24", "25", "29.97", "30", "50", "59.94", "60"]),
    help="Project frame rate.",
)
@click.option(
    "--color-space",
    default="Rec.709",
    help="Color space (default: Rec.709).",
)
@click.option(
    "--proxy-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional directory of proxies to reference in the timeline.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to write the Resolve project (.drp). Defaults to <project>.drp in source folder.",
)
@click.pass_context
def resolve_create(
    ctx: click.Context,
    path: Path,
    project: str,
    resolution: str,
    frame_rate: str,
    color_space: str,
    proxy_dir: Path | None,
    output_path: Path | None,
) -> None:
    """Create a Resolve project from a folder of media files."""
    store = _get_store(ctx)
    cfg = _get_config(ctx)

    output = output_path or (path / f"{project}.drp")
    spec = ResolveProjectSpec(
        name=project,
        source_folder=str(path),
        output_path=str(output),
        resolution=resolution,  # type: ignore[arg-type]
        frame_rate=frame_rate,  # type: ignore[arg-type]
        color_space=color_space,
    )
    result = create_resolve_project(spec, path, proxy_dir, store, config=cfg)

    console = Console()
    if result.resolve_version:
        console.print(
            f"[green]Created project {result.name!r} (Resolve {result.resolve_version})[/green]"
        )
    else:
        console.print(
            f"[yellow]Resolve not available; wrote manifest file at {output}.manifest.json[/yellow]"
        )
    console.print(f"  bins: {result.bin_count}, timelines: {result.timeline_count}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--accept-changes",
    is_flag=True,
    default=False,
    help="Acknowledge and record the current state as the new baseline after a mismatch.",
)
@click.pass_context
def verify(ctx: click.Context, path: Path, accept_changes: bool) -> None:
    """Verify a folder against its previous checksum snapshot."""
    store = _get_store(ctx)
    report = verify_folder(path, store, accept_changes=accept_changes)

    console = Console()
    if report.is_clean:
        console.print(f"[green]Clean: {report.files_checked} file(s) verified[/green]")
    else:
        console.print("[yellow]Differences found:[/yellow]")
        if report.files_missing:
            console.print(f"  Missing: {report.files_missing}")
        if report.files_modified:
            console.print(f"  Modified: {report.files_modified}")
        if report.files_added:
            console.print(f"  Added: {report.files_added}")
        console.print(
            "\n[dim]Run with --accept-changes to acknowledge these differences "
            "and set a new baseline.[/dim]"
        )

    # Exit with the report's exit code so scripts can switch on it.
    ctx.exit(report.exit_code)


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


@main.command(name="log")
@click.option(
    "--limit",
    type=int,
    default=20,
    help="Max runs to show (default: 20).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.pass_context
def log_cmd(ctx: click.Context, limit: int, fmt: str) -> None:
    """Show recent audit-log runs."""
    _get_store(ctx)  # ensures DB exists
    db_path = ctx.obj["db_path"]

    rows = _fetch_recent_runs(db_path, limit)

    if fmt == "json":
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        Console().print("[dim]No runs recorded yet.[/dim]")
        return

    console = Console()
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("Started")
    table.add_column("Status")
    table.add_column("Command")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["started_at"],
            row["status"],
            row["command"],
        )
    console.print(table)


def _fetch_recent_runs(db_path: Path, limit: int) -> list[dict[str, Any]]:
    """Fetch the most recent N runs from the audit log."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, started_at, status, command FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# run (pipeline orchestration)
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--organize", "do_organize", is_flag=True, help="Organize step.")
@click.option("--proxy", "do_proxy", is_flag=True, help="Proxy step.")
@click.option(
    "--resolve-project",
    "do_resolve",
    is_flag=True,
    help="Resolve project creation step.",
)
@click.option("--verify", "do_verify", is_flag=True, help="Verify step.")
@click.option(
    "--project-name",
    default="MediaProject",
    help="Name for the Resolve project (used with --resolve-project).",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Output root for organized media, proxies, and project files "
    "(default: <source>-output next to the source folder).",
)
@click.pass_context
def run_cmd(
    ctx: click.Context,
    path: Path,
    do_organize: bool,
    do_proxy: bool,
    do_resolve: bool,
    do_verify: bool,
    project_name: str,
    out_dir: Path | None,
) -> None:
    """Run a media-ops pipeline on a folder.

    Always probes first (idempotent — already-probed files just get re-probed).
    Then runs each enabled step in order: organize → proxy → resolve-project → verify.

    The source folder is read-only for the whole pipeline: organize copies
    into the output root (see --out) unless [organize] mode = "move" is set
    in config.
    """
    store = _get_store(ctx)
    cfg = _get_config(ctx)
    console = Console()
    out_root = out_dir if out_dir is not None else path.parent / f"{path.name}-output"

    # Step 1: probe (always)
    console.print("[bold]Step 1: probe[/bold]")
    results = probe_path(path, store)
    console.print(f"  Probed {len(results)} file(s)")

    # Step 2: organize (optional)
    if do_organize:
        console.print("[bold]Step 2: organize[/bold]")
        root = out_root / "organized"
        org_result = organize_path(path, root, store, config=cfg)
        verb = "Moved" if cfg.organize.mode == "move" else "Copied"
        console.print(f"  {verb} {org_result.files_moved}, skipped {org_result.files_skipped}")
        organized_root: Path | None = root
    else:
        organized_root = None

    # Step 3: proxy (optional)
    proxy_dir: Path | None = None
    if do_proxy:
        source_for_proxy = organized_root or path
        console.print("[bold]Step 3: proxy[/bold]")
        proxy_dir = out_root / "proxies"
        proxy_batch = generate_proxies(source_for_proxy, proxy_dir, store, config=cfg)
        console.print(f"  Generated {len(proxy_batch.results)} proxy file(s)")
        if proxy_batch.skipped:
            console.print(f"  Skipped {len(proxy_batch.skipped)} non-video file(s)")
        if proxy_batch.failures:
            console.print(f"  [red]Failed {len(proxy_batch.failures)} file(s)[/red]")

    # Step 4: resolve project (optional)
    if do_resolve:
        console.print("[bold]Step 4: resolve-project[/bold]")
        source_for_resolve = organized_root or path
        out_root.mkdir(parents=True, exist_ok=True)
        spec = ResolveProjectSpec(
            name=project_name,
            source_folder=str(source_for_resolve),
            output_path=str(out_root / f"{project_name}.drp"),
        )
        resolve_result = create_resolve_project(
            spec, source_for_resolve, proxy_dir, store, config=cfg
        )
        if resolve_result.resolve_version:
            console.print(f"  Created Resolve project (v{resolve_result.resolve_version})")
        else:
            console.print("  Wrote manifest (Resolve not available)")

    # Step 5: verify (optional)
    if do_verify:
        console.print("[bold]Step 5: verify[/bold]")
        report = verify_folder(organized_root or path, store)
        if report.is_clean:
            console.print(f"  Clean: {report.files_checked} file(s) verified")
        else:
            console.print(
                f"  Differences: missing={report.files_missing}, "
                f"modified={report.files_modified}, added={report.files_added}"
            )

    console.print("[green]Done.[/green]")


@main.command(name="tui")
@click.pass_context
def tui(ctx: click.Context) -> None:
    """Launch the interactive TUI (alternative to subcommands)."""
    from media_mate.tui import main as tui_main

    tui_main()
