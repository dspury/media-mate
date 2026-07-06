"""Textual TUI for media-mate.

Screens
-------
HomeScreen      — ASCII art logo, nav buttons, system stats
PipelineScreen  — Path input + step toggles + animated run
LogScreen       — Recent audit-log runs in a DataTable
SettingsScreen  — Current config values

Each capability module (probe, organize, proxy, verify) is called directly
from the worker thread so the TUI stays responsive.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    Log,
    ProgressBar,
    Static,
)

from media_mate import __version__
from media_mate.config import load_config
from media_mate.log import LogStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEDIA_MATE_DIR = Path.home() / ".media-mate"
DEFAULT_DB = MEDIA_MATE_DIR / "media-mate.db"

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

MM_THEME = Theme(
    name="media-mate-dark",
    primary="cyan",
    secondary="#DAA520",
    accent="#9B59B6",
    success="#27AE60",
    warning="#F39C12",
    error="#E74C3C",
    surface="#1a1a2e",
    panel="#16213e",
    background="#0f0f1a",
    dark=True,
)

# ---------------------------------------------------------------------------
# ASCII logo
# ---------------------------------------------------------------------------

ASCII_LOGO = r"""
  ╔════════════════════════════════════════════╗
  ║     _ __ ___   __ _ _ __   ___  _ __     ║
  ║    | '_ ` _ \ / _` | '_ \ / _ \| '_ \    ║
  ║    | | | | | | (_| | |_) | (_) | | | |   ║
  ║    |_| |_| |_|\__,_| .__/ \___/|_| |_|   ║
  ║                     |_|                   ║
  ║  zero-cost post-production media ops     ║
  ╚════════════════════════════════════════════╝
"""

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def get_ffmpeg_version() -> str:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.split("\n")[0].split()[2]
    except Exception:
        return "not found"


def get_run_counts(db: Path) -> tuple[int, int, int, int]:
    """Return (total, success, failed, running)."""
    if not db.exists():
        return 0, 0, 0, 0
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status").fetchall())
    total = sum(rows.values())
    return (
        total,
        rows.get("success", 0),
        rows.get("failed", 0),
        rows.get("running", 0),
    )


# ---------------------------------------------------------------------------
# HomeScreen
# ---------------------------------------------------------------------------


class HomeScreen(Screen[Any]):
    CSS = """
    HomeScreen {
        align: center middle;
    }
    #logo-box {
        width: 72;
        height: auto;
        background: $panel;
        border: thick solid $secondary;
        padding: 1 3;
        margin-bottom: 2;
    }
    #logo-box Static {
        color: $secondary;
        font-family: "Courier New", Courier, monospace;
        font-size: 9;
        line-height: 110%;
    }
    #menu {
        width: 52;
    }
    .nav-btn {
        width: 100%;
        margin-bottom: 1;
    }
    #status-box {
        width: 52;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        total, success, failed, running = get_run_counts(DEFAULT_DB)
        ffmpeg_ver = get_ffmpeg_version()

        with Container(id="logo-box"):
            yield Static(ASCII_LOGO)

        with Vertical(id="menu"):
            yield Button("▶  Run Pipeline", id="btn-pipeline", classes="nav-btn")
            yield Button("📋  View Log", id="btn-log", classes="nav-btn")
            yield Button("⚙   Settings", id="btn-settings", classes="nav-btn")
            yield Button("⏻   Quit", id="btn-quit", classes="nav-btn")

        with Container(id="status-box"):
            yield Static(f"ffmpeg     {ffmpeg_ver}")
            yield Static(f"db         {DEFAULT_DB}")
            yield Static(
                f"runs       {total} total  |  "
                f"[green]{success} ✓[/green]  |  "
                f"[red]{failed} ✗[/red]  |  "
                f"[cyan]{running} ⚡[/cyan]"
            )

    @on(Button.Pressed, "#btn-pipeline")
    def on_pipeline(self) -> None:
        self.app.push_screen("pipeline")

    @on(Button.Pressed, "#btn-log")
    def on_log(self) -> None:
        self.app.push_screen("log")

    @on(Button.Pressed, "#btn-settings")
    def on_settings(self) -> None:
        self.app.push_screen("settings")

    @on(Button.Pressed, "#btn-quit")
    def on_quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# PipelineScreen
# ---------------------------------------------------------------------------


class PipelineScreen(Screen[Any]):
    path_value = reactive("")
    running = reactive(False)

    CSS = """
    PipelineScreen {
        align: center middle;
    }
    #panel {
        width: 74;
        height: auto;
        background: $panel;
        border: solid $primary;
        padding: 2 3;
    }
    .step-row {
        margin-bottom: 0;
    }
    #progress-wrap {
        height: 5;
        margin-top: 1;
        border: solid $surface;
        padding: 0 1;
    }
    #btn-row {
        height: 3;
        align: center bottom;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="panel"):
            yield Static("[bold cyan]▶  Run Pipeline[/bold cyan]")
            yield Input(
                placeholder="Enter path to media folder…",
                id="path-input",
            )
            yield Label("[b]Steps:[/b]")
            yield Vertical(
                Checkbox("Probe — extract metadata", True, id="chk-probe"),
                Checkbox("Organize — sort by codec / resolution", id="chk-organize"),
                Checkbox("Proxy — generate H.264 edit proxies", id="chk-proxy"),
                Checkbox("Verify — checksum audit", id="chk-verify"),
                id="step-col",
            )
            yield Label("[b]Progress:[/b]")
            with Container(id="progress-wrap"):
                yield ProgressBar(id="progress", show_eta=True)
                yield Log(id="log-area", max_lines=50, auto_scroll=True)
            with Horizontal(id="btn-row"):
                yield Button("← Back", id="btn-back", variant="default")
                yield Button("▶  Run", id="btn-run", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#path-input").focus()

    @on(Input.Changed, "#path-input")
    def on_path_changed(self, event: Input.Changed) -> None:
        self.path_value = event.value

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn-run")
    def on_run(self) -> None:
        if self.running:
            return
        path_str = self.path_value.strip()
        if not path_str:
            self.app.push_screen(_error_dialog("Enter a folder path first."))
            return
        path = Path(path_str)
        if not path.exists():
            self.app.push_screen(_error_dialog(f"Path not found:\n{path_str}"))
            return
        self._execute(path)

    def _execute(self, path: Path) -> None:
        self.running = True
        btn = self.query_one("#btn-run", Button)
        btn.disabled = True
        progress = self.query_one("#progress", ProgressBar)
        log_area = self.query_one("#log-area", Log)

        steps: list[tuple[str, str]] = [
            ("probe", "Probe — extract metadata"),
            ("organize", "Organize — sort media"),
            ("proxy", "Proxy — generate proxies"),
            ("verify", "Verify — checksum audit"),
        ]
        enabled = [
            (name, label)
            for name, label in steps
            if (
                (name == "probe" and self.query_one("#chk-probe", Checkbox).value)
                or (name == "organize" and self.query_one("#chk-organize", Checkbox).value)
                or (name == "proxy" and self.query_one("#chk-proxy", Checkbox).value)
                or (name == "verify" and self.query_one("#chk-verify", Checkbox).value)
            )
        ]
        total = len(enabled) or 1

        progress.update(total=total, progress=0)
        log_area.write(f"[cyan]▶ Starting pipeline on[/cyan]  {path}")

        # Import here so the TUI can start even if these fail to import
        from media_mate.organize import organize_path
        from media_mate.probe import probe_path
        from media_mate.proxy import generate_proxies
        from media_mate.verify import verify_folder

        def _run() -> None:
            store = LogStore(DEFAULT_DB)
            store.initialize()
            cfg = load_config(None)
            overall_ok = True

            for i, (step, label) in enumerate(enabled):
                log_area.write(f"[cyan]→[/cyan] {label} …")
                try:
                    if step == "probe":
                        results = probe_path(path, store)
                        log_area.write(f"  [green]✓[/green]  probed {len(results)} file(s)")

                    elif step == "organize":
                        root = path / "organized"
                        result = organize_path(path, root, store, config=cfg)
                        moved = result.files_moved
                        skipped = result.files_skipped
                        log_area.write(
                            f"  [green]✓[/green]  moved [b]{moved}[/b] file(s)"
                            + (f", skipped {skipped}" if skipped else "")
                        )

                    elif step == "proxy":
                        proxy_dir = path / "proxies"
                        presults = generate_proxies(path, proxy_dir, store, config=cfg)
                        log_area.write(
                            f"  [green]✓[/green]  generated [b]{len(presults)}[/b] proxy file(s)"
                        )

                    elif step == "verify":
                        report = verify_folder(path, store)
                        if report.is_clean:
                            log_area.write(
                                f"  [green]✓[/green]  [b]{report.files_checked}[/b] file(s) clean"
                            )
                        else:
                            diffs = []
                            if report.files_missing:
                                diffs.append(f"missing={report.files_missing}")
                            if report.files_modified:
                                diffs.append(f"modified={report.files_modified}")
                            log_area.write(f"  [yellow]![/yellow]  {', '.join(diffs)}")
                            overall_ok = False

                except Exception as exc:
                    log_area.write(f"  [red]✗[/red]  {step}: {exc}")
                    overall_ok = False

                # Update progress from background thread via call_from_thread
                self.app.call_from_thread(progress.update, progress=i + 1)

            log_area.write("")
            if overall_ok:
                log_area.write("[bold green]✓ Pipeline complete.[/bold green]")
            else:
                log_area.write("[bold yellow]⚠ Finished with errors.[/bold yellow]")

            self.app.call_from_thread(self._set_running_false)

        self.app.run_worker(_run, exclusive=True)

    def _set_running_false(self) -> None:
        self.running = False
        self.query_one("#btn-run", Button).disabled = False


# ---------------------------------------------------------------------------
# LogScreen
# ---------------------------------------------------------------------------


class LogScreen(Screen[Any]):
    CSS = """
    LogScreen {
        align: center middle;
    }
    #panel {
        width: 90;
        height: 90%;
        background: $panel;
        border: solid $primary;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="panel"):
            yield Static("[bold cyan]📋  Audit Log[/bold cyan]")
            with Horizontal():
                yield Button("← Back", id="btn-back")
                yield Button("🔄 Refresh", id="btn-refresh")
            yield DataTable(id="log-table")

    def on_mount(self) -> None:
        table = self.query_one("#log-table", DataTable)
        table.add_column("ID", width=5, key="id")
        table.add_column("Started", width=28, key="started")
        table.add_column("Status", width=10, key="status")
        table.add_column("Command", width=None, key="command")
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#log-table", DataTable)
        table.clear()

        if not DEFAULT_DB.exists():
            return

        with sqlite3.connect(DEFAULT_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, started_at, status, command FROM runs ORDER BY id DESC LIMIT 200"
            ).fetchall()

        for row in rows:
            row_key = table.add_row(
                str(row["id"]),
                row["started_at"][:28],
                row["status"],
                row["command"],
            )
            # Color-code the status column by painting the row
            status = row["status"]
            if status == "success":
                table.update_cell(row_key, "status", "[green]success[/green]")
            elif status == "failed":
                table.update_cell(row_key, "status", "[red]failed[/red]")
            elif status == "running":
                table.update_cell(row_key, "status", "[cyan]running[/cyan]")
            else:
                table.update_cell(row_key, "status", f"[yellow]{status}[/yellow]")

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn-refresh")
    def on_refresh(self) -> None:
        self._refresh()


# ---------------------------------------------------------------------------
# SettingsScreen
# ---------------------------------------------------------------------------


class SettingsScreen(Screen[Any]):
    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #panel {
        width: 62;
        height: auto;
        background: $panel;
        border: solid $secondary;
        padding: 2 3;
    }
    """

    def compose(self) -> ComposeResult:
        cfg = load_config(None)
        with Container(id="panel"):
            yield Static("[bold gold]⚙  Settings[/bold gold]")
            yield Label("")
            yield Static("[b]Active configuration:[/b]")
            yield Label("")
            yield Horizontal(
                Static("proxy codec", id="lbl-proxy"),
                Static(f"[cyan]{cfg.proxy_codec}[/cyan]", id="val-proxy"),
            )
            yield Horizontal(
                Static("proxy height", id="lbl-height"),
                Static(f"[cyan]{cfg.proxy_height}p[/cyan]", id="val-height"),
            )
            yield Horizontal(
                Static("checksum", id="lbl-cs"),
                Static(f"[cyan]{cfg.checksum_algo.value}[/cyan]", id="val-cs"),
            )
            yield Horizontal(
                Static("resolve path", id="lbl-res"),
                Static(f"[cyan]{cfg.resolve_path or 'not set'}[/cyan]", id="val-res"),
            )
            yield Horizontal(
                Static("ffmpeg path", id="lbl-ff"),
                Static(f"[cyan]{cfg.ffmpeg_path or 'default'}[/cyan]", id="val-ff"),
            )
            yield Label("")
            yield Button("← Back", id="btn-back")

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# ErrorDialog  (factory function — simpler than a full class)
# ---------------------------------------------------------------------------


def _error_dialog(message: str) -> Screen[Any]:
    class ErrorDialog(Screen[Any]):
        CSS = """
        ErrorDialog { align: center middle; }
        #dialog {
            width: 60;
            height: auto;
            background: $surface;
            border: thick solid $error;
            padding: 2 3;
        }
        """

        def compose(self) -> ComposeResult:
            with Container(id="dialog"):
                yield Static(f"[red]![/red]  {message}", id="err-msg")
                yield Button("OK", id="btn-ok")

        @on(Button.Pressed, "#btn-ok")
        def on_ok(self) -> None:
            self.app.pop_screen()

    return ErrorDialog()


# ---------------------------------------------------------------------------
# MediaMateApp
# ---------------------------------------------------------------------------


class MediaMateApp(App[Any]):
    TITLE = "media-mate"
    SUB_TITLE = f"v{__version__}"
    THEMES: ClassVar = [MM_THEME]
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "pop_screen", "Back", show=True),
    ]
    SCREENS: ClassVar = {
        "home": HomeScreen,
        "pipeline": PipelineScreen,
        "log": LogScreen,
        "settings": SettingsScreen,
    }

    def on_mount(self) -> None:
        self.push_screen("home")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app = MediaMateApp()
    app.run()


if __name__ == "__main__":
    main()
