"""Keyboard-first Textual interface for media-mate."""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass
from os import replace
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Literal, cast

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
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
    Log,
    ProgressBar,
    Select,
    Static,
)

from media_mate import __version__
from media_mate.config import load_config
from media_mate.log import LogStore
from media_mate.models import ChecksumAlgo, MediaMateConfig, OrganizeConfig

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
 __  __ _____ ____ ___    _      __  __    _  _____ _____
|  \/  | ____|  _ \_ _|  / \    |  \/  |  / \|_   _| ____|
| |\/| |  _| | | | | |  / _ \   | |\/| | / _ \ | | |  _|
| |  | | |___| |_| | | / ___ \  | |  | |/ ___ \| | | |___
|_|  |_|_____|____/___/_/   \_\ |_|  |_/_/   \_\_| |_____|
"""


def get_ffmpeg_version() -> str:
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        return result.stdout.splitlines()[0].split()[2]
    except Exception:
        return "not found"


def get_run_counts(db: Path) -> tuple[int, int, int, int]:
    if not db.exists():
        return 0, 0, 0, 0
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status"))
    return sum(rows.values()), rows.get("success", 0), rows.get("failed", 0), rows.get("running", 0)


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
        total, success, failed, running = get_run_counts(cast("MediaMateApp", self.app).db_path)
        yield Header()
        with Container(id="hero"):
            yield Static(ASCII_LOGO, id="logo")
            yield Static("INGEST  •  ORGANIZE  •  PROXY  •  VERIFY", id="strap")
            with Horizontal(id="home-actions"):
                yield Button("RUN PIPELINES  [R]", id="pipeline", variant="primary")
                yield Button("AUDIT LOG  [L]", id="logs")
                yield Button("SETTINGS  [S]", id="settings")
            yield Static(
                f"FFMPEG {get_ffmpeg_version()}   RUNS {total}   [green]OK {success}[/]   [red]FAIL {failed}[/]   [cyan]LIVE {running}[/]",
                id="system",
            )
        yield Footer()

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
        self.cancel_requested = False
        self.started = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            with Vertical(id="browser-pane"):
                yield Label("MEDIA BROWSER", classes="section-title")
                yield Input(value=str(Path.home()), placeholder="Path", id="browser-path")
                yield DirectoryTree(Path.home(), id="tree")
                yield Button("ADD FOLDER  [A]", id="add", variant="primary")
            with Vertical(id="run-pane"):
                yield Label("PIPELINE QUEUE", classes="section-title")
                yield DataTable(id="queue")
                with Horizontal(id="steps"):
                    yield Checkbox("Probe", True, id="probe")
                    yield Checkbox("Organize", id="organize")
                    yield Checkbox("Proxy", id="proxy")
                    yield Checkbox("Resolve", id="resolve")
                    yield Checkbox("Verify", id="verify")
                yield Input(placeholder="Output root (default: <source>-output)", id="output-root")
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
                            for value in ("23.976", "24", "25", "29.97", "30", "50", "59.94", "60")
                        ],
                        value="24",
                        id="frame-rate",
                    )
                    yield Input(value="Rec.709", placeholder="Color space", id="color-space")
                yield ProgressBar(id="progress", show_eta=False)
                yield Static("IDLE  •  select a folder and press A", id="stats")
                yield Log(id="activity", max_lines=1000, auto_scroll=True, highlight=True)
                with Horizontal():
                    yield Button("RUN QUEUE  [Ctrl+R]", id="run", variant="primary")
                    yield Button("REMOVE  [Del]", id="remove")
                    yield Button("BACK  [Esc]", id="back")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#queue", DataTable)
        table.cursor_type = "row"
        table.add_columns("#", "Folder", "State")

    @on(Input.Submitted, "#browser-path")
    def browse_path(self, event: Input.Submitted) -> None:
        path = Path(event.value).expanduser()
        if path.is_dir():
            self.query_one("#tree", DirectoryTree).path = path
        else:
            self.app.push_screen(MessageDialog(f"[red]Folder not found:[/]\n{path}"))

    @on(DirectoryTree.DirectorySelected)
    def selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#browser-path", Input).value = str(event.path)

    def action_add(self) -> None:
        path = Path(self.query_one("#browser-path", Input).value).expanduser().resolve()
        if not path.is_dir():
            self.app.push_screen(MessageDialog(f"[red]Not a directory:[/]\n{path}"))
            return
        if any(item.path == path for item in self.items):
            return
        self.items.append(QueueItem(path))
        self._draw_queue()

    def action_remove(self) -> None:
        table = self.query_one("#queue", DataTable)
        if self.items and table.cursor_row is not None:
            self.items.pop(table.cursor_row)
            self._draw_queue()

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
                str(i), str(item.path), f"[{colors[item.status]}]{item.status.upper()}[/]"
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
        if not self.items or any(i.status == "running" for i in self.items):
            return
        self.cancel_requested = False
        self.started = monotonic()
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
        self.app.run_worker(lambda: self._run_queue(enabled, options), thread=True, exclusive=True)

    def action_cancel(self) -> None:
        self.cancel_requested = True
        self.query_one("#activity", Log).write(
            "[yellow]CANCEL REQUESTED — current capability call will finish safely[/]"
        )

    def _ui(self, message: str, completed: int, total: int) -> None:
        self.query_one("#activity", Log).write(message)
        self.query_one("#progress", ProgressBar).update(total=max(total, 1), progress=completed)
        self.query_one("#stats", Static).update(
            f"ACTIVE  •  {completed}/{total} steps  •  elapsed {monotonic() - self.started:0.1f}s"
        )
        self._draw_queue()

    def _run_queue(self, enabled: list[str], options: PipelineOptions) -> None:
        from media_mate.models import ResolveProjectSpec
        from media_mate.organize import organize_path
        from media_mate.probe import probe_path
        from media_mate.proxy import generate_proxies
        from media_mate.resolve import create_resolve_project
        from media_mate.verify import verify_folder

        app = cast("MediaMateApp", self.app)
        store = LogStore(app.db_path)
        store.initialize()
        cfg = load_config(app.config_path)
        total = len(self.items) * len(enabled)
        completed = 0
        for item in self.items:
            if self.cancel_requested:
                item.status = "cancelled"
                continue
            item.status = "running"
            self.app.call_from_thread(self._ui, f"[cyan]▶ {item.path}[/]", completed, total)
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
                    if self.cancel_requested:
                        item.status = "cancelled"
                        break
                    self.app.call_from_thread(
                        self._ui, f"[purple]→ {step.upper()}[/] {item.path.name}", completed, total
                    )
                    if step == "probe":
                        probe_result = probe_path(item.path, store, config=cfg)
                        detail = f"{len(probe_result)} file(s)"
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
                            detail = f"{verify_result.files_checked} checked" + (
                                "" if verify_result.is_clean else " — differences"
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
                    self._ui, f"[red]✗ {type(exc).__name__}: {exc}[/]", completed, total
                )
        self.app.call_from_thread(
            self._ui,
            "[bold green]QUEUE COMPLETE[/]"
            if not self.cancel_requested
            else "[bold yellow]QUEUE STOPPED[/]",
            completed,
            total,
        )


class LogScreen(Screen[Any]):
    BINDINGS: ClassVar = [Binding("/", "search", "Search"), Binding("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="log-panel"):
            yield Label("AUDIT LOG", classes="section-title")
            yield Input(placeholder="Search command or status…", id="search")
            yield DataTable(id="log-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#log-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("ID", "Started", "Status", "Command")
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
        if not app.db_path.exists():
            return
        with sqlite3.connect(app.db_path) as conn:
            rows = conn.execute(
                "SELECT id, started_at, status, command FROM runs ORDER BY id DESC LIMIT 500"
            ).fetchall()
        colors = {"success": "green", "failed": "red", "running": "cyan", "partial": "yellow"}
        for rid, started, status, command in rows:
            if needle and needle not in f"{status} {command}".lower():
                continue
            table.add_row(
                str(rid), started[:19], f"[{colors.get(status, 'yellow')}]{status}[/]", command
            )


class SettingsScreen(Screen[Any]):
    def compose(self) -> ComposeResult:
        app = cast("MediaMateApp", self.app)
        cfg = load_config(app.config_path)
        yield Header()
        with Container(id="settings-panel"):
            yield Label("PROJECT SETTINGS", classes="section-title")
            yield Label("Proxy codec")
            yield Select(
                [(x, x) for x in ("ProRes422Proxy", "ProRes422LT", "ProRes422", "ProRes422HQ")],
                value=cfg.proxy_codec,
                id="codec",
            )
            yield Label("Proxy height")
            yield Select(
                [(f"{x}p", x) for x in (540, 720, 1080, 2160)], value=cfg.proxy_height, id="height"
            )
            yield Label("Checksum")
            yield Select(
                [("xxHash (fast)", "xxhash"), ("SHA-256", "sha256")],
                value=cfg.checksum_algo.value,
                id="checksum",
            )
            yield Label("FFmpeg path (blank = PATH)")
            yield Input(value=cfg.ffmpeg_path or "", id="ffmpeg")
            yield Label("Resolve path (blank = auto)")
            yield Input(value=cfg.resolve_path or "", id="resolve-path")
            yield Label("Organize template")
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
            with Horizontal():
                yield Button("SAVE  [Ctrl+S]", id="save", variant="primary")
                yield Button("BACK", id="back")
            yield Static(f"Writes {config_target(app.config_path)}", id="config-target")
        yield Footer()

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
    Screen { background: $background; }
    Header { background: $surface; color: $text; }
    Footer { background: $surface; }
    #hero { width: 84; height: auto; align: center middle; margin: 4 0; padding: 2 4; border: tall $primary; background: $panel; align-horizontal: center; }
    #logo { color: $primary; text-align: center; } #strap { text-align: center; color: $text-muted; margin: 1; }
    #home-actions { height: 5; align: center middle; } #home-actions Button { margin: 0 1; } #system { text-align: center; border-top: solid $surface-lighten-2; padding-top: 1; }
    #workspace { height: 1fr; padding: 1; } #browser-pane { width: 38%; border-right: solid $surface-lighten-2; padding: 0 1; } #run-pane { width: 62%; padding: 0 1; }
    .section-title { color: $primary; text-style: bold; margin-bottom: 1; } #tree { height: 1fr; } #queue { height: 12; } #steps, #pipeline-options, #resolve-options { height: 3; } #progress { margin-top: 1; }
    #stats { color: $accent; height: 2; } #activity { height: 1fr; border: solid $surface-lighten-2; background: $surface; }
    #log-panel { margin: 1 3; } #search { width: 60; margin-bottom: 1; } #log-table { height: 1fr; }
    #settings-panel { width: 70; height: auto; margin: 2 0; padding: 2 4; border: tall $secondary; background: $panel; align-horizontal: center; } #settings-panel Select, #settings-panel Input { margin-bottom: 1; } #organize-options Select { width: 1fr; margin-right: 1; }
    MessageDialog { align: center middle; background: rgba(0,0,0,0.7); } #dialog { width: 60; height: auto; padding: 2 3; border: tall $error; background: $panel; }
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

    async def action_back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()


def main(db_path: Path = DEFAULT_DB, config_path: Path | None = None) -> None:
    MediaMateApp(db_path, config_path).run()


if __name__ == "__main__":
    main()
