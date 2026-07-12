"""Keyboard-first Textual interface for media-mate."""

from __future__ import annotations

import os
import platform
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from os import replace
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Literal, cast

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RichLog,
    Select,
    Static,
)
from textual.widgets.option_list import Option
from textual.worker import NoActiveWorker, get_current_worker

from media_mate import __version__
from media_mate.config import load_config
from media_mate.log import LogStore
from media_mate.models import ChecksumAlgo, MediaMateConfig, OrganizeConfig
from media_mate.probe import SYSTEM_ARTIFACT_NAMES

DEFAULT_DB = Path.home() / ".media-mate" / "media-mate.db"

MM_THEME = Theme(
    name="media-mate-studio",
    primary="#ff7a45",
    secondary="#a970ff",
    accent="#35c5f0",
    success="#52d273",
    warning="#ffc857",
    error="#ff5c5c",
    surface="#171922",
    panel="#20232f",
    background="#0d0f14",
    dark=True,
)

ASCII_LOGO = r"""
                       ___                             __
   ____ ___  ___  ____/ (_)___ _      ____ ___  ____ _/ /____
  / __ `__ \/ _ \/ __  / / __ `/_____/ __ `__ \/ __ `/ __/ _ \
 / / / / / /  __/ /_/ / / /_/ /_____/ / / / / / /_/ / /_/  __/
/_/ /_/ /_/\___/\__,_/_/\__,_/     /_/ /_/ /_/\__,_/\__/\___/
"""

STRAP = "INGEST  ·  ORGANIZE  ·  PROXY  ·  RESOLVE  ·  VERIFY"
TAGLINE = "Zero-cost post-production media ops  ·  every step audited"

_STATUS_GLYPH = {
    "success": "[green]●[/]",
    "failed": "[red]●[/]",
    "running": "[cyan]●[/]",
    "partial": "[yellow]●[/]",
}


def _format_size(num_bytes: int) -> str:
    """Render a byte count as a short human-readable string (e.g. ``1.2T``)."""
    scaled = float(num_bytes)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(scaled) < 1024:
            return f"{scaled:0.1f}{unit}"
        scaled /= 1024
    return f"{scaled:0.1f}P"


def _drive_label(path: Path) -> str:
    """Best-effort display label for a mount, including free/total space."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return path.name or str(path)
    free = _format_size(usage.free)
    total = _format_size(usage.total)
    name = path.name or str(path)
    return f"{name}  ·  {free} free / {total}"


def list_external_drives() -> list[Path]:
    """Return mount points for connected external / removable volumes.

    Cross-platform:
      - macOS:   every entry under ``/Volumes/`` except the system volume
        (detected by realpath resolving to ``/``). This correctly excludes
        ``Macintosh HD`` while still showing the user's data volume and any
        attached camera cards, backup disks, or USB sticks.
      - Linux:   every directory under ``/media/$USER/`` and
        ``/run/media/$USER/`` (modern GNOME/udisks2), deduplicated by
        realpath. Fallback to ``/media`` if the user-scoped path is empty.
      - Windows: every drive letter that exists except ``%SYSTEMDRIVE%``.

    Returned paths are sorted alphabetically for stable display order.
    """
    system = platform.system()
    drives: list[Path] = []

    if system == "Darwin":
        root_real = Path("/").resolve()
        volumes = Path("/Volumes")
        if volumes.is_dir():
            for child in sorted(volumes.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir():
                    continue
                try:
                    if child.resolve() == root_real:
                        continue  # skip system volume
                except OSError:
                    continue
                drives.append(child)
        return drives

    if system == "Linux":
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        bases: list[Path] = []
        if user:
            bases.append(Path(f"/media/{user}"))
            bases.append(Path(f"/run/media/{user}"))
        bases.append(Path("/media"))
        seen: set[str] = set()
        for base in bases:
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir():
                    continue
                try:
                    real = str(child.resolve())
                except OSError:
                    continue
                if real in seen:
                    continue
                seen.add(real)
                drives.append(child)
        return drives

    if system == "Windows":
        system_drive = (os.environ.get("SYSTEMDRIVE") or "C:").rstrip(":").upper()[:1] or "C"
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if letter == system_drive:
                continue
            root = Path(f"{letter}:/")
            if root.exists() and root.is_dir():
                drives.append(root)
        return drives

    return drives


def get_ffmpeg_version() -> str:
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        return result.stdout.splitlines()[0].split()[2]
    except Exception:
        return "not found"


#: Cached ffmpeg version — the subprocess check can take seconds; the Home
#: screen fetches it once in a background worker and reuses it afterwards.
_ffmpeg_version: str | None = None


def _cached_ffmpeg_version() -> str:
    global _ffmpeg_version
    if _ffmpeg_version is None:
        _ffmpeg_version = get_ffmpeg_version()
    return _ffmpeg_version


def get_run_counts(db: Path) -> tuple[int, int, int, int]:
    if not db.exists():
        return 0, 0, 0, 0
    try:
        with sqlite3.connect(db) as conn:
            rows = dict(conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status"))
    except sqlite3.Error:
        # A corrupt/foreign db file must not crash the dashboard.
        return 0, 0, 0, 0
    return sum(rows.values()), rows.get("success", 0), rows.get("failed", 0), rows.get("running", 0)


class MediaDirectoryTree(DirectoryTree):
    """Directory tree that hides OS/system junk (dotfiles, $RECYCLE.BIN, …).

    Camera cards and backup drives are littered with .Trashes, .Spotlight-V100,
    AppleDouble sidecars and recycle bins; none of them are valid pipeline
    sources, so the browser doesn't show them.
    """

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if not (p.name.startswith(".") or p.name in SYSTEM_ARTIFACT_NAMES)]


def config_target(explicit: Path | None) -> Path:
    if explicit:
        return explicit
    local = Path.cwd() / "media-mate.toml"
    return local if local.exists() else Path.home() / ".media-mate" / "config.toml"


def save_config(config: MediaMateConfig, path: Path) -> None:
    """Persist the existing TOML schema atomically while retaining comments."""

    def q(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    values = {
        ("", "proxy_codec"): q(config.proxy_codec),
        ("", "proxy_height"): str(config.proxy_height),
        ("", "checksum_algo"): q(config.checksum_algo.value),
        ("", "resolve_path"): q(config.resolve_path) if config.resolve_path else None,
        ("", "ffmpeg_path"): q(config.ffmpeg_path) if config.ffmpeg_path else None,
        ("organize", "template"): q(config.organize.template),
        ("organize", "on_conflict"): q(config.organize.on_conflict),
        ("organize", "mode"): q(config.organize.mode),
    }
    content = (
        _merge_config_text(path.read_text(encoding="utf-8"), values)
        if path.exists()
        else _default_config_text(values)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(content, encoding="utf-8")
    replace(temporary, path)


def _default_config_text(values: dict[tuple[str, str], str | None]) -> str:
    lines = [
        f"proxy_codec = {values[('', 'proxy_codec')]}",
        f"proxy_height = {values[('', 'proxy_height')]}",
        f"checksum_algo = {values[('', 'checksum_algo')]}",
        f"resolve_path = {values[('', 'resolve_path')]}" if values[("", "resolve_path")] else "",
        f"ffmpeg_path = {values[('', 'ffmpeg_path')]}" if values[("", "ffmpeg_path")] else "",
        "",
        "[organize]",
        f"template = {values[('organize', 'template')]}",
        f"on_conflict = {values[('organize', 'on_conflict')]}",
        f"mode = {values[('organize', 'mode')]}",
        "",
    ]
    return "\n".join(line for line in lines if line != "") + "\n"


def _merge_config_text(existing: str, values: dict[tuple[str, str], str | None]) -> str:
    """Update known TOML values without discarding comments or unrelated layout."""
    if not existing.strip():
        return _default_config_text(values)

    section = ""
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    section_re = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
    assignment_re = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\s+#.*)?$")
    for line in existing.splitlines():
        section_match = section_re.match(line)
        if section_match:
            section = section_match.group(1)
            lines.append(line)
            continue
        assignment_match = assignment_re.match(line)
        if assignment_match:
            indent, key, equals, _old_value, inline_comment = assignment_match.groups()
            target = (section, key)
            if target in values:
                seen.add(target)
                value = values[target]
                if value is not None:
                    lines.append(f"{indent}{key}{equals}{value}{inline_comment or ''}")
                continue
        lines.append(line)

    missing_top = [
        f"{key} = {value}"
        for (section_name, key), value in values.items()
        if section_name == "" and value is not None and (section_name, key) not in seen
    ]
    first_section = next((i for i, line in enumerate(lines) if section_re.match(line)), len(lines))
    lines[first_section:first_section] = missing_top

    missing_organize = [
        f"{key} = {value}"
        for (section_name, key), value in values.items()
        if section_name == "organize" and value is not None and (section_name, key) not in seen
    ]
    if missing_organize:
        organize_start = next(
            (
                i
                for i, line in enumerate(lines)
                if section_re.match(line) and line.strip().startswith("[organize]")
            ),
            None,
        )
        if organize_start is None:
            if lines and lines[-1]:
                lines.append("")
            lines.extend(["[organize]", *missing_organize])
        else:
            organize_end = next(
                (i for i in range(organize_start + 1, len(lines)) if section_re.match(lines[i])),
                len(lines),
            )
            lines[organize_end:organize_end] = missing_organize
    return "\n".join(lines).rstrip() + "\n"


class MessageDialog(ModalScreen[None]):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(self.message)
            yield Button("OK", id="ok", variant="primary")

    @on(Button.Pressed, "#ok")
    def close(self) -> None:
        self.dismiss(None)


class HomeScreen(Screen[Any]):
    BINDINGS: ClassVar = [
        ("r", "pipeline", "Run"),
        ("l", "logs", "Logs"),
        ("s", "settings", "Settings"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="home"):
            with Container(id="hero"):
                yield Static(ASCII_LOGO, id="logo")
                yield Static(STRAP, id="strap")
                yield Static(TAGLINE, id="tagline")
            with Horizontal(id="home-actions"):
                yield Button("RUN PIPELINES  [R]", id="pipeline", variant="primary")
                yield Button("AUDIT LOG  [L]", id="logs")
                yield Button("SETTINGS  [S]", id="settings")
            with Horizontal(id="stats-row"):
                yield Static("", id="stat-total", classes="stat-tile")
                yield Static("", id="stat-success", classes="stat-tile")
                yield Static("", id="stat-failed", classes="stat-tile")
                yield Static("", id="stat-live", classes="stat-tile")
            yield Static("", id="system")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hero", Container).border_title = "media-mate"
        self._refresh_stats()
        # The ffmpeg version check spawns a subprocess (up to 5s on a cold
        # PATH) — fetch it off the UI thread so startup never blocks.
        if _ffmpeg_version is None:
            self.run_worker(self._load_ffmpeg_version, thread=True)

    def on_screen_resume(self) -> None:
        # Screens are cached by the app; refresh counts when the user returns
        # from a pipeline run so the dashboard is never stale.
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        app = cast("MediaMateApp", self.app)
        total, success, failed, running = get_run_counts(app.db_path)
        self.query_one("#stat-total", Static).update(
            f"[dim]TOTAL RUNS[/]\n[bright_white bold]{total}[/]"
        )
        self.query_one("#stat-success", Static).update(
            f"[dim]SUCCEEDED[/]\n[green bold]{success}[/]"
        )
        self.query_one("#stat-failed", Static).update(f"[dim]FAILED[/]\n[red bold]{failed}[/]")
        self.query_one("#stat-live", Static).update(f"[dim]LIVE[/]\n[cyan bold]{running}[/]")
        self._update_system_line(_ffmpeg_version or "checking…")

    def _update_system_line(self, ffmpeg_version: str) -> None:
        app = cast("MediaMateApp", self.app)
        self.query_one("#system", Static).update(
            f"[dim]FFMPEG[/] [bold]{ffmpeg_version}[/]   [dim]DB[/] {app.db_path}"
        )

    def _load_ffmpeg_version(self) -> None:
        version = _cached_ffmpeg_version()
        self.app.call_from_thread(self._update_system_line, version)

    def action_pipeline(self) -> None:
        self.app.push_screen("pipeline")

    def action_logs(self) -> None:
        self.app.push_screen("logs")

    def action_settings(self) -> None:
        self.app.push_screen("settings")

    @on(Button.Pressed)
    def buttons(self, event: Button.Pressed) -> None:
        {
            "pipeline": self.action_pipeline,
            "logs": self.action_logs,
            "settings": self.action_settings,
        }.get(event.button.id or "", lambda: None)()


@dataclass
class QueueItem:
    path: Path
    status: str = "queued"


@dataclass(frozen=True)
class PipelineOptions:
    output_root: Path | None
    move: bool
    dry_run: bool
    accept_changes: bool
    project_name: str
    resolution: str
    frame_rate: str
    color_space: str


def compute_output_tree(output_root: Path | None, item_path: Path) -> Path:
    """Compute the per-source output tree for a single queue item.

    Each queued source gets its OWN subtree so same-named clips from separate
    folders (e.g. two camera cards both containing ``clip.MP4``) never collide
    in a shared ``<root>/organized`` or ``<root>/proxies``. With a shared
    output_root, card_a's organized output lands at ``<root>/card_a/organized``
    and card_b's at ``<root>/card_b/organized``. Without a root, each source's
    tree sits next to the source under its parent.
    """
    base = output_root if output_root is not None else item_path.parent
    return base / item_path.name


class PipelineScreen(Screen[Any]):
    BINDINGS: ClassVar = [
        Binding("a", "add", "Add folder"),
        Binding("ctrl+r", "run", "Run queue"),
        Binding("ctrl+c", "cancel", "Cancel"),
        Binding("delete", "remove", "Remove"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.items: list[QueueItem] = []
        self.drives: list[Path] = []
        self._drive_labels: list[str] = []
        self.busy = False
        self.cancel_requested = False
        self.started = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            with Vertical(id="browser-pane"):
                yield Input(
                    value=str(Path.home()), placeholder="Path to browse…", id="browser-path"
                )
                yield Static("EXTERNAL DRIVES", id="drives-label", classes="browser-section-label")
                yield OptionList(id="drives-list")
                yield MediaDirectoryTree(Path.home(), id="tree")
                yield Button("ADD FOLDER  [A]", id="add", variant="primary")
            with Vertical(id="run-pane"):
                with Vertical(id="queue-panel"):
                    yield DataTable(id="queue")
                with Vertical(id="config-panel"):
                    with Horizontal(id="steps"):
                        yield Checkbox("1 Probe", True, id="probe")
                        yield Checkbox("2 Organize", id="organize")
                        yield Checkbox("3 Proxy", id="proxy")
                        yield Checkbox("4 Resolve", id="resolve")
                        yield Checkbox("5 Verify", id="verify")
                    yield Input(
                        placeholder="Output root  (blank = beside source)",
                        id="output-root",
                    )
                    with Horizontal(id="pipeline-options"):
                        yield Checkbox("Move originals", id="move")
                        yield Checkbox("Dry-run organize", id="dry-run")
                        yield Checkbox("Accept verify changes", id="accept-changes")
                    with Horizontal(id="resolve-options"):
                        yield Input(placeholder="Resolve project name", id="project-name")
                        yield Select(
                            [("720p", "720"), ("1080p", "1080"), ("4K", "4K")],
                            value="1080",
                            id="resolution",
                        )
                        yield Select(
                            [
                                (value, value)
                                for value in (
                                    "23.976",
                                    "24",
                                    "25",
                                    "29.97",
                                    "30",
                                    "50",
                                    "59.94",
                                    "60",
                                )
                            ],
                            value="24",
                            id="frame-rate",
                        )
                        yield Input(value="Rec.709", placeholder="Color space", id="color-space")
                with Vertical(id="activity-panel"):
                    yield ProgressBar(id="progress", show_eta=False)
                    yield Static("IDLE  •  select a folder and press A", id="stats")
                    yield RichLog(
                        id="activity", max_lines=1000, auto_scroll=True, markup=True, wrap=True
                    )
                with Horizontal(id="run-actions"):
                    yield Button("RUN QUEUE  [Ctrl+R]", id="run", variant="primary")
                    yield Button("REMOVE  [Del]", id="remove")
                    yield Button("BACK  [Esc]", id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#browser-pane", Vertical).border_title = "MEDIA BROWSER"
        self.query_one("#queue-panel", Vertical).border_title = "QUEUE"
        self.query_one("#config-panel", Vertical).border_title = "CONFIGURE"
        self.query_one("#activity-panel", Vertical).border_title = "ACTIVITY"
        table = self.query_one("#queue", DataTable)
        table.cursor_type = "row"
        # State before Folder so it stays visible however long the path is.
        table.add_columns("#", "State", "Folder")
        self.query_one("#progress", ProgressBar).update(total=1, progress=0)
        # Drive detection touches disk (mount resolution + free-space stat) and
        # can stall on spun-down or flaky media — run it off the UI thread, and
        # re-scan periodically so hot-plugged cards appear without a restart.
        # The initial scan comes from on_screen_resume, which also fires on the
        # first push — scanning here too would cancel it (exclusive group).
        self.set_interval(10.0, self._start_drive_scan)

    def on_screen_resume(self) -> None:
        self._start_drive_scan()

    def _start_drive_scan(self) -> None:
        self.run_worker(self._scan_drives, thread=True, exclusive=True, group="drive-scan")

    def _scan_drives(self) -> None:
        drives = list_external_drives()
        labels = [_drive_label(d) for d in drives]
        try:
            if get_current_worker().is_cancelled:
                return  # a newer scan superseded this one — drop the stale result
        except NoActiveWorker:
            pass
        self.app.call_from_thread(self._show_drives, drives, labels)

    def _show_drives(self, drives: list[Path], labels: list[str]) -> None:
        if drives == self.drives and labels == self._drive_labels:
            return  # unchanged — don't rebuild the list under the user's cursor
        self.drives = drives
        self._drive_labels = labels
        drive_list = self.query_one("#drives-list", OptionList)
        drive_list.clear_options()
        drive_list.add_options(
            Option(Text(label, no_wrap=True, overflow="ellipsis"), id=f"drive-{i}")
            for i, label in enumerate(labels)
        )
        drive_list.display = bool(drives)
        self.query_one("#drives-label", Static).display = bool(drives)

    @on(Input.Submitted, "#browser-path")
    def browse_path(self, event: Input.Submitted) -> None:
        path = Path(event.value).expanduser()
        if path.is_dir():
            self.query_one("#tree", DirectoryTree).path = path
        else:
            self.app.push_screen(MessageDialog(f"[red]Folder not found:[/]\n{escape(str(path))}"))

    @on(OptionList.OptionSelected, "#drives-list")
    def drive_selected(self, event: OptionList.OptionSelected) -> None:
        """Navigate the tree (and path input) to the clicked external drive."""
        option_id = event.option.id or ""
        if not option_id.startswith("drive-"):
            return
        try:
            index = int(option_id.split("-", 1)[1])
        except ValueError:
            return
        if not 0 <= index < len(self.drives):
            return
        path = self.drives[index]
        if not path.is_dir():
            self.app.push_screen(
                MessageDialog(f"[red]Drive no longer mounted:[/]\n{escape(str(path))}")
            )
            return
        self.query_one("#browser-path", Input).value = str(path)
        self.query_one("#tree", DirectoryTree).path = path

    @on(DirectoryTree.DirectorySelected)
    def selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#browser-path", Input).value = str(event.path)

    def action_add(self) -> None:
        path = Path(self.query_one("#browser-path", Input).value).expanduser().resolve()
        if not path.is_dir():
            self.app.push_screen(MessageDialog(f"[red]Not a directory:[/]\n{escape(str(path))}"))
            return
        if any(item.path == path for item in self.items):
            return
        self.items.append(QueueItem(path))
        self._draw_queue()

    def action_remove(self) -> None:
        if self.busy:
            self.app.push_screen(
                MessageDialog("[yellow]Queue is running[/] — cancel it before removing items.")
            )
            return
        table = self.query_one("#queue", DataTable)
        if self.items and table.cursor_row is not None:
            self.items.pop(table.cursor_row)
            self._draw_queue()

    @staticmethod
    def _short_path(path: Path, max_len: int = 48) -> str:
        text = str(path)
        if len(text) <= max_len:
            return text
        return "…" + text[-(max_len - 1) :]

    def _draw_queue(self) -> None:
        table = self.query_one("#queue", DataTable)
        table.clear()
        colors = {
            "queued": "yellow",
            "running": "cyan",
            "done": "green",
            "failed": "red",
            "cancelled": "magenta",
        }
        for i, item in enumerate(self.items, 1):
            table.add_row(
                str(i),
                f"[{colors[item.status]}]{item.status.upper()}[/]",
                # Text, not str: literal rendering — bracketed clip/folder names
                # must not be parsed as markup.
                Text(self._short_path(item.path)),
            )

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        if event.button.id == "add":
            self.action_add()
        elif event.button.id == "run":
            self.action_run()
        elif event.button.id == "remove":
            self.action_remove()
        elif event.button.id == "back":
            self.app.pop_screen()

    def action_run(self) -> None:
        if self.busy or not self.items:
            return
        enabled = [
            s
            for s in ("probe", "organize", "proxy", "resolve", "verify")
            if self.query_one(f"#{s}", Checkbox).value
        ]
        if not enabled:
            self.app.push_screen(MessageDialog("Select at least one pipeline step."))
            return
        output_root = self.query_one("#output-root", Input).value.strip()
        options = PipelineOptions(
            output_root=Path(output_root).expanduser() if output_root else None,
            move=self.query_one("#move", Checkbox).value,
            dry_run=self.query_one("#dry-run", Checkbox).value,
            accept_changes=self.query_one("#accept-changes", Checkbox).value,
            project_name=self.query_one("#project-name", Input).value.strip(),
            resolution=str(self.query_one("#resolution", Select).value),
            frame_rate=str(self.query_one("#frame-rate", Select).value),
            color_space=self.query_one("#color-space", Input).value.strip() or "Rec.709",
        )
        self.busy = True
        self.cancel_requested = False
        self.started = monotonic()
        self.query_one("#run", Button).disabled = True
        self.app.run_worker(lambda: self._run_queue(enabled, options), thread=True, exclusive=True)

    def action_cancel(self) -> None:
        if not self.busy:
            return
        self.cancel_requested = True
        self._log("[yellow]CANCEL REQUESTED — current capability call will finish safely[/]")

    def _cancelled(self) -> bool:
        """True when the user asked to cancel or the app is shutting the worker down."""
        if self.cancel_requested:
            return True
        try:
            return get_current_worker().is_cancelled
        except NoActiveWorker:
            return False

    def _finish_run(self) -> None:
        self.busy = False
        self.query_one("#run", Button).disabled = False

    def _log(self, message: str) -> None:
        self.query_one("#activity", RichLog).write(message)

    def _ui(self, message: str, completed: int, total: int) -> None:
        self._log(message)
        self.query_one("#progress", ProgressBar).update(total=max(total, 1), progress=completed)
        self.query_one("#stats", Static).update(
            f"ACTIVE  •  {completed}/{total} steps  •  elapsed {monotonic() - self.started:0.1f}s"
        )
        self._draw_queue()

    def _log_failure_lines(self, failures: list[str], limit: int = 5) -> None:
        """Surface per-file failure reasons in the activity log (bounded)."""
        for line in failures[:limit]:
            self.app.call_from_thread(self._log, f"[red]    ✗ {escape(line)}[/]")
        if len(failures) > limit:
            self.app.call_from_thread(self._log, f"[red]    … {len(failures) - limit} more[/]")

    def _run_queue(self, enabled: list[str], options: PipelineOptions) -> None:
        try:
            self._run_queue_inner(enabled, options)
        finally:
            self.app.call_from_thread(self._finish_run)

    def _run_queue_inner(self, enabled: list[str], options: PipelineOptions) -> None:
        from media_mate.models import ResolveProjectSpec
        from media_mate.organize import organize_path
        from media_mate.probe import probe_path
        from media_mate.proxy import generate_proxies
        from media_mate.resolve import create_resolve_project
        from media_mate.verify import verify_folder

        app = cast("MediaMateApp", self.app)
        try:
            store = LogStore(app.db_path)
            store.initialize()
            cfg = load_config(app.config_path)
        except Exception as exc:
            self.app.call_from_thread(
                self._ui,
                f"[red]✗ Could not start queue — {type(exc).__name__}: {escape(str(exc))}[/]",
                0,
                1,
            )
            return
        # Snapshot: items added/removed mid-run must not shift the live batch.
        items = list(self.items)
        total = len(items) * len(enabled)
        completed = 0
        for item in items:
            if self._cancelled():
                item.status = "cancelled"
                continue
            item.status = "running"
            self.app.call_from_thread(
                self._ui, f"[cyan]▶ {escape(str(item.path))}[/]", completed, total
            )
            # Each queue item gets its own output tree, even when a shared output_root
            # is configured. This prevents same-named clips from separate source folders
            # (e.g. multiple camera cards) from colliding in <root>/organized.
            out = compute_output_tree(options.output_root, item.path)
            organized: Path | None = None
            proxy_dir: Path | None = None
            organize_ran = False
            organize_dry = options.dry_run
            try:
                for step in enabled:
                    if self._cancelled():
                        item.status = "cancelled"
                        break
                    self.app.call_from_thread(
                        self._ui,
                        f"[purple]→ {step.upper()}[/] {escape(item.path.name)}",
                        completed,
                        total,
                    )
                    if step == "probe":
                        probe_failures: list[str] = []

                        def on_probe_file(
                            file: Path, error: str | None, _failures: list[str] = probe_failures
                        ) -> None:
                            if error is None:
                                self.app.call_from_thread(
                                    self._log, f"[dim]    · {escape(file.name)}[/]"
                                )
                            else:
                                _failures.append(f"{file.name} — {error}")

                        probe_result = probe_path(
                            item.path, store, config=cfg, on_file=on_probe_file
                        )
                        self._log_failure_lines(probe_failures)
                        if not probe_result:
                            if probe_failures:
                                raise RuntimeError(
                                    f"probe failed for all {len(probe_failures)} file(s)"
                                )
                            raise RuntimeError(
                                "no media files found — is the folder still mounted?"
                            )
                        detail = f"{len(probe_result)} ok" + (
                            f", [red]{len(probe_failures)} failed[/]" if probe_failures else ""
                        )
                    elif step == "organize":
                        organized = out / "organized"
                        organize_result = organize_path(
                            item.path,
                            organized,
                            store,
                            config=cfg,
                            dry_run=organize_dry,
                            move=True if options.move else None,
                        )
                        organize_ran = True
                        self._log_failure_lines(organize_result.errors)
                        detail = (
                            f"{organize_result.files_moved} ok, {organize_result.files_skipped} skipped"
                            + (" [dry-run]" if organize_dry else "")
                        )
                    elif step == "proxy":
                        # Skip proxy if organize is in the pipeline and was a dry-run —
                        # there is no organized output to proxy; operating on the source
                        # folder would proxy raw footage instead of the organized proxy.
                        if organize_ran and organize_dry:
                            detail = "skipped — organize was dry-run"
                        else:
                            proxy_source: Path = item.path if organized is None else organized
                            proxy_dir = out / "proxies"
                            proxy_result = generate_proxies(
                                proxy_source, proxy_dir, store, config=cfg
                            )
                            self._log_failure_lines(
                                [
                                    f"{Path(fail.source_path).name} — {fail.reason}"
                                    for fail in proxy_result.failures
                                ]
                            )
                            detail = f"{len(proxy_result.results)} ok, {len(proxy_result.already_existed) + len(proxy_result.skipped)} skipped, {len(proxy_result.failures)} failed"
                    elif step == "resolve":
                        # Same guard as proxy: don't resolve from an organize dry-run.
                        if organize_ran and organize_dry:
                            detail = "skipped — organize was dry-run"
                        else:
                            out.mkdir(parents=True, exist_ok=True)
                            resolve_source: Path = item.path if organized is None else organized
                            spec = ResolveProjectSpec(
                                name=options.project_name or item.path.name,
                                source_folder=str(resolve_source),
                                output_path=str(
                                    out / f"{options.project_name or item.path.name}.drp"
                                ),
                                resolution=cast(Any, options.resolution),
                                frame_rate=cast(Any, options.frame_rate),
                                color_space=options.color_space,
                            )
                            resolve_result = create_resolve_project(
                                spec, resolve_source, proxy_dir, store, config=cfg
                            )
                            detail = f"{resolve_result.bin_count} bins"
                    else:
                        # Skip verify if organize was a dry-run (same reasoning as proxy).
                        if organize_ran and organize_dry:
                            detail = "skipped — organize was dry-run"
                        else:
                            verify_source: Path = item.path if organized is None else organized
                            verify_result = verify_folder(
                                verify_source,
                                store,
                                config=cfg,
                                accept_changes=options.accept_changes,
                            )
                            detail = f"{verify_result.files_checked} checked"
                            if not verify_result.is_clean:
                                detail += (
                                    f" — [red]{verify_result.files_missing} missing, "
                                    f"{verify_result.files_modified} modified, "
                                    f"{verify_result.files_added} added[/]"
                                )
                    completed += 1
                    self.app.call_from_thread(
                        self._ui, f"[green]✓ {step}[/]  {detail}", completed, total
                    )
                if item.status != "cancelled":
                    item.status = "done"
            except Exception as exc:
                item.status = "failed"
                completed += 1
                self.app.call_from_thread(
                    self._ui,
                    f"[red]✗ {type(exc).__name__}: {escape(str(exc))}[/]",
                    completed,
                    total,
                )
        failed = sum(1 for item in items if item.status == "failed")
        if self._cancelled():
            summary = "[bold yellow]QUEUE STOPPED[/]"
        elif failed:
            summary = f"[bold red]QUEUE COMPLETE — {failed} of {len(items)} folder(s) failed[/]"
        else:
            summary = "[bold green]QUEUE COMPLETE[/]"
        self.app.call_from_thread(self._ui, summary, completed, total)


class LogScreen(Screen[Any]):
    BINDINGS: ClassVar = [Binding("/", "search", "Search"), Binding("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="log-panel"):
            yield Input(placeholder="Search command or status…", id="search")
            yield DataTable(id="log-table")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#log-panel", Vertical).border_title = "AUDIT LOG"
        table = self.query_one("#log-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("ID", "Started", "Status", "Command")
        self._refresh()

    def on_screen_resume(self) -> None:
        # Screens are cached by the app — refresh so runs finished since the
        # last visit show up without pressing R.
        self._refresh()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_refresh(self) -> None:
        self._refresh()

    @on(Input.Changed, "#search")
    def filter(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#log-table", DataTable)
        table.clear()
        needle = self.query_one("#search", Input).value.lower()
        app = cast("MediaMateApp", self.app)
        panel = self.query_one("#log-panel", Vertical)
        if not app.db_path.exists():
            panel.border_subtitle = "no runs yet"
            return
        try:
            with sqlite3.connect(app.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, started_at, status, command FROM runs ORDER BY id DESC LIMIT 500"
                ).fetchall()
        except sqlite3.Error as exc:
            panel.border_subtitle = f"db error: {exc}"
            return
        shown = 0
        for rid, started, status, command in rows:
            if needle and needle not in f"{status} {command}".lower():
                continue
            # Friendlier timestamp: "2026-07-11T20:00:29…" -> "2026-07-11 20:00:29"
            when = f"{started[:10]} {started[11:19]}" if len(started) >= 19 else started
            glyph = _STATUS_GLYPH.get(status, "[yellow]●[/]")
            table.add_row(str(rid), when, f"{glyph} {status}", command)
            shown += 1
        total = len(rows)
        panel.border_subtitle = f"{shown} shown · {total} total" if needle else f"{total} runs"


class SettingsScreen(Screen[Any]):
    def compose(self) -> ComposeResult:
        app = cast("MediaMateApp", self.app)
        cfg = load_config(app.config_path)
        yield Header()
        with VerticalScroll(id="settings-scroll"), Container(id="settings-panel"):
            with Vertical(id="proxy-section"):
                yield Label("Proxy codec", classes="field-label")
                yield Select(
                    [(x, x) for x in ("ProRes422Proxy", "ProRes422LT", "ProRes422", "ProRes422HQ")],
                    value=cfg.proxy_codec,
                    id="codec",
                )
                yield Label("Proxy height", classes="field-label")
                yield Select(
                    [(f"{x}p", x) for x in (540, 720, 1080, 2160)],
                    value=cfg.proxy_height,
                    id="height",
                )
                yield Label("Checksum", classes="field-label")
                yield Select(
                    [("xxHash (fast)", "xxhash"), ("SHA-256", "sha256")],
                    value=cfg.checksum_algo.value,
                    id="checksum",
                )
            with Vertical(id="organize-section"):
                yield Label("Organize template", classes="field-label")
                yield Input(value=cfg.organize.template, id="organize-template")
                with Horizontal(id="organize-options"):
                    yield Select(
                        [
                            ("Skip conflicts", "skip"),
                            ("Overwrite conflicts", "overwrite"),
                            ("Rename conflicts", "rename"),
                        ],
                        value=cfg.organize.on_conflict,
                        id="on-conflict",
                    )
                    yield Select(
                        [("Copy originals", "copy"), ("Move originals", "move")],
                        value=cfg.organize.mode,
                        id="organize-mode",
                    )
            with Vertical(id="paths-section"):
                yield Label("FFmpeg path  (blank = PATH)", classes="field-label")
                yield Input(value=cfg.ffmpeg_path or "", id="ffmpeg")
                yield Label("Resolve path  (blank = auto)", classes="field-label")
                yield Input(value=cfg.resolve_path or "", id="resolve-path")
            with Horizontal(id="settings-actions"):
                yield Button("SAVE  [Ctrl+S]", id="save", variant="primary")
                yield Button("BACK", id="back")
            yield Static(f"Writes {config_target(app.config_path)}", id="config-target")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#settings-panel", Container).border_title = "PROJECT SETTINGS"
        self.query_one("#proxy-section", Vertical).border_title = "PROXY"
        self.query_one("#organize-section", Vertical).border_title = "ORGANIZE"
        self.query_one("#paths-section", Vertical).border_title = "PATHS"

    BINDINGS: ClassVar = [Binding("ctrl+s", "save", "Save")]

    def action_save(self) -> None:
        app = cast("MediaMateApp", self.app)
        old = load_config(app.config_path)
        height = self.query_one("#height", Select).value
        assert isinstance(height, int)
        cfg = MediaMateConfig.model_validate(
            {
                **old.model_dump(),
                "proxy_codec": str(self.query_one("#codec", Select).value),
                "proxy_height": height,
                "checksum_algo": ChecksumAlgo(str(self.query_one("#checksum", Select).value)),
                "ffmpeg_path": self.query_one("#ffmpeg", Input).value.strip() or None,
                "resolve_path": self.query_one("#resolve-path", Input).value.strip() or None,
                "organize": OrganizeConfig(
                    template=self.query_one("#organize-template", Input).value,
                    on_conflict=cast(
                        Literal["skip", "overwrite", "rename"],
                        str(self.query_one("#on-conflict", Select).value),
                    ),
                    mode=cast(
                        Literal["copy", "move"],
                        str(self.query_one("#organize-mode", Select).value),
                    ),
                ),
            }
        )
        target = config_target(app.config_path)
        save_config(cfg, target)
        app.config_path = target
        self.app.push_screen(MessageDialog(f"[green]Settings saved[/]\n{target}"))

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        self.action_save() if event.button.id == "save" else self.app.pop_screen()


class MediaMateApp(App[Any]):
    TITLE = "MEDIA MATE"
    SUB_TITLE = f"POST WORKSTATION  /  v{__version__}"
    THEMES: ClassVar = [MM_THEME]
    CSS = """
    Screen { background: $background; color: $text; }
    Header { background: $surface; }
    Footer { background: $surface; }

    /* ---- Home dashboard ---- */
    #home { height: 1fr; align-horizontal: center; padding: 1 2; }
    #hero { width: 90; padding: 1 4; border: round $primary; background: $panel; align-horizontal: center; }
    #logo { color: $primary; text-align: center; text-style: bold; }
    #strap { text-align: center; color: $accent; text-style: bold; margin: 1 0 0 0; }
    #tagline { text-align: center; color: $text-muted; text-style: italic; }
    #home-actions { height: 3; align: center middle; margin: 1 0; }
    #home-actions Button { margin: 0 1; }
    #stats-row { height: auto; align: center middle; margin: 1 0; }
    .stat-tile { width: 18; height: 3; border: round $surface-lighten-2; background: $surface; padding: 0 2; text-align: center; margin: 0 1; }
    #system { text-align: center; color: $text-muted; margin: 1 0 0 0; }

    /* ---- Pipeline workspace ---- */
    #workspace { height: 1fr; }
    #browser-pane { width: 34%; height: 1fr; border: round $primary; background: $panel; padding: 0 1; margin: 0 1 0 0; }
    #browser-pane Input { margin: 0 0 1 0; }
    .browser-section-label { color: $accent; text-style: bold; height: 1; margin: 0; }
    #drives-list { display: none; height: auto; max-height: 6; margin: 0 0 1 0; padding: 0 1; border: solid $surface-lighten-2; background: $background; }
    #drives-label { display: none; }
    #drives-list > .option-list--option-highlighted { background: $primary 30%; color: $text; }
    #tree { height: 1fr; border: solid $surface-lighten-2; background: $background; }
    #browser-pane Button { margin: 1 0 0 0; }
    #run-pane { width: 1fr; height: 1fr; padding: 0 0 0 1; }
    #queue-panel { height: auto; border: round $surface-lighten-2; background: $panel; padding: 0 1; margin: 0 0 1 0; }
    #queue { height: 5; }
    #config-panel { height: auto; border: round $surface-lighten-2; background: $panel; padding: 0 1; margin: 0 0 1 0; }
    /* Checkboxes default to a 3-row tall border; compact them to one row so
       the step and option rows are actually visible. */
    #config-panel Checkbox { border: none; padding: 0 1; height: 1; background: transparent; }
    #config-panel Checkbox:focus { background: $primary 20%; text-style: bold; }
    #steps { height: auto; } #steps Checkbox { margin: 0 1 0 0; }
    #config-panel Input { margin: 1 0; }
    #pipeline-options { height: auto; } #pipeline-options Checkbox { margin: 0 2 0 0; }
    /* Inputs default to width 100% — inside a Horizontal that pushes every
       sibling Select off-screen. Share the row instead. */
    #resolve-options { height: auto; }
    #resolve-options Input { width: 1fr; margin: 0 1 0 0; }
    #resolve-options Select { width: 13; margin: 0 1 0 0; }
    #activity-panel { height: 1fr; border: round $surface-lighten-2; background: $panel; padding: 0 1; }
    #progress { margin: 1 0; }
    #stats { color: $accent; text-style: bold; height: 1; }
    #activity { height: 1fr; border: solid $surface-lighten-2; background: $background; }
    #run-actions { height: auto; dock: bottom; padding: 1 0 0 0; } #run-actions Button { margin: 0 1 0 0; }

    /* ---- Logs ---- */
    #log-panel { height: 1fr; border: round $primary; background: $panel; padding: 0 1; margin: 1 2; }
    #search { margin: 0 0 1 0; }
    #log-table { height: 1fr; }

    /* ---- Settings ---- */
    #settings-scroll { height: 1fr; align-horizontal: center; padding: 1 2; }
    #settings-panel { width: 80; border: round $secondary; background: $panel; padding: 0 1; }
    #proxy-section, #organize-section, #paths-section { border: round $surface-lighten-2; background: $surface; padding: 0 1; margin: 0 0 1 0; }
    .field-label { color: $accent; text-style: bold; margin: 1 0 0 0; }
    #organize-options { height: auto; } #organize-options Select { width: 1fr; margin: 0 1 0 0; }
    #settings-actions { height: auto; align-horizontal: center; padding: 1 0; } #settings-actions Button { margin: 0 1; }
    #config-target { text-align: center; color: $text-muted; margin: 1 0; }

    /* ---- Dialog ---- */
    MessageDialog { align: center middle; background: $background; }
    #dialog { width: 64; padding: 1 2; border: round $primary; background: $panel; }
    """
    BINDINGS: ClassVar = [Binding("q", "quit", "Quit"), Binding("escape", "back", "Back")]
    SCREENS: ClassVar = {
        "home": HomeScreen,
        "pipeline": PipelineScreen,
        "logs": LogScreen,
        "settings": SettingsScreen,
    }

    def __init__(self, db_path: Path = DEFAULT_DB, config_path: Path | None = None) -> None:
        super().__init__()
        self.db_path = db_path
        self.config_path = config_path

    def on_mount(self) -> None:
        self.register_theme(MM_THEME)
        self.theme = MM_THEME.name
        self.push_screen("home")

    def _busy_pipeline(self) -> PipelineScreen | None:
        """The pipeline screen, if it exists and a queue is currently running."""
        for screen in self.screen_stack:
            if isinstance(screen, PipelineScreen) and screen.busy:
                return screen
        return None

    async def action_back(self) -> None:
        if isinstance(self.screen, PipelineScreen) and self.screen.busy:
            self.push_screen(
                MessageDialog(
                    "[yellow]Queue is running.[/]\nPress Ctrl+C to cancel it before leaving."
                )
            )
            return
        if len(self.screen_stack) > 1:
            self.pop_screen()

    async def action_quit(self) -> None:
        # Quitting mid-copy can leave a half-written organize tree or truncated
        # proxy on disk — require an explicit cancel first.
        if self._busy_pipeline() is not None:
            self.push_screen(
                MessageDialog(
                    "[yellow]Queue is running.[/]\nPress Ctrl+C to cancel it before quitting."
                )
            )
            return
        await super().action_quit()


def main(db_path: Path = DEFAULT_DB, config_path: Path | None = None) -> None:
    MediaMateApp(db_path, config_path).run()


if __name__ == "__main__":
    main()
