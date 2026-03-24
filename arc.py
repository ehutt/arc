#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "typer>=0.15",
#     "rich>=13",
#     "pyyaml>=6",
# ]
# ///
"""arc — lightweight agent project scaffolding."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import tomllib
import typer
import yaml
from rich.console import Console
from rich.table import Table
from typer._completion_classes import completion_init

completion_init()

def _find_claude() -> str:
    """Locate the claude binary, checking nvm paths if not on PATH."""
    found = shutil.which("claude")
    if found:
        return found
    # nvm installs aren't always on PATH in non-interactive shells
    nvm_dir = os.environ.get("NVM_DIR", os.path.expanduser("~/.nvm"))
    for node_bin in sorted(Path(nvm_dir, "versions", "node").glob("*/bin/claude"), reverse=True):
        if node_bin.is_file():
            return str(node_bin)
    return "claude"


CLAUDE_BIN = _find_claude()


def _set_project_env(proj: Project) -> None:
    """Set ARC_PROJECT_SLUG so Claude hooks can identify the project."""
    os.environ["ARC_PROJECT_SLUG"] = proj.slug


def _clean_env() -> dict[str, str]:
    """Return a copy of os.environ with conda/virtualenv vars stripped.

    This prevents agent subprocesses (Claude, Codex) from inheriting the
    host's Python environment, which can cause mypy/pytest to resolve
    packages from the wrong site-packages (e.g. miniconda3 instead of
    the sandbox's .venv).
    """
    env = os.environ.copy()
    # Remove conda environment variables
    for key in list(env):
        if key.startswith(("CONDA_", "_CE_")):
            del env[key]
    # Remove virtualenv / venv activation vars
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT"):
        env.pop(key, None)
    # Strip conda/venv paths from PATH
    if "PATH" in env:
        paths = env["PATH"].split(os.pathsep)
        paths = [
            p
            for p in paths
            if "miniconda" not in p
            and "anaconda" not in p
            and "conda" not in p.split(os.sep)
            and ".venv" not in p.split(os.sep)
        ]
        env["PATH"] = os.pathsep.join(paths)
    return env


app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()

# Project-level statuses (derived from stages when stages exist)
PROJECT_STATUSES = ["needs-plan", "planned", "active", "done", "archived"]

PROJECT_STATUS_COLORS = {
    "needs-plan": "dim",
    "planned": "cyan",
    "active": "yellow",
    "done": "green",
    "archived": "dim",
}

# Stage-level statuses (past tense)
STAGE_STATUSES = ["pending", "ready", "implemented", "reviewed", "pr-open", "done", "skipped"]

STAGE_STATUS_COLORS = {
    "pending": "dim",
    "ready": "green",
    "implemented": "yellow",
    "reviewed": "cyan",
    "pr-open": "magenta",
    "done": "green",
    "skipped": "dim",
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    obsidian_vault: Path
    projects_folder: str
    sandbox_root: Path
    branch_prefix: str
    github_user: str
    github_repo: str
    test_cmd: str
    lint_cmd: str

    @property
    def projects_dir(self) -> Path:
        return self.obsidian_vault / self.projects_folder

    @property
    def bare_repo(self) -> Path:
        return self.sandbox_root / ".phoenix-bare"


def load_config() -> Config:
    config_path = Path(__file__).resolve().parent / "config.toml"
    if not config_path.exists():
        console.print(f"[red]Config not found: {config_path}[/red]")
        raise typer.Exit(1)
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)
    return Config(
        obsidian_vault=Path(raw["core"]["obsidian_vault"]).expanduser(),
        projects_folder=raw["core"]["projects_folder"],
        sandbox_root=Path(raw["core"]["sandbox_root"]).expanduser(),
        branch_prefix=raw["core"]["branch_prefix"],
        github_user=raw["github"]["user"],
        github_repo=raw["github"]["repo"],
        test_cmd=raw["agent"]["test_cmd"],
        lint_cmd=raw["agent"]["lint_cmd"],
    )


# ---------------------------------------------------------------------------
# Stage + Project dataclasses + frontmatter
# ---------------------------------------------------------------------------


@dataclass
class Stage:
    id: int
    name: str
    status: str = "pending"  # pending | ready | implemented | reviewed | pr-open | done | skipped
    depends_on: list[int] = field(default_factory=list)
    github_issues: list[str] = field(default_factory=list)
    github_prs: list[str] = field(default_factory=list)

    def folder_name(self) -> str:
        """Conventional folder name: '{id}-{slugified-name}'."""
        return f"{self.id}-{slugify(self.name)}"

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "depends_on": self.depends_on,
        }
        if self.github_issues:
            d["github_issues"] = self.github_issues
        if self.github_prs:
            d["github_prs"] = self.github_prs
        return d


@dataclass
class Project:
    title: str
    slug: str
    status: str
    type: str = "engineering"
    created: str = ""
    updated: str = ""
    tags: list[str] = field(default_factory=list)
    github_issues: list[str] = field(default_factory=list)
    github_prs: list[str] = field(default_factory=list)
    pr_title: str = ""
    sandbox: str = ""
    branch: str = ""
    related_notes: list[str] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    dev_port: int = 0
    dev_session: str = ""
    path: Path | None = None

    @property
    def is_folder(self) -> bool:
        return self.path is not None and self.path.name == "index.md"

    @property
    def folder(self) -> Path | None:
        if self.is_folder:
            return self.path.parent
        return None

    @property
    def status_color(self) -> str:
        return PROJECT_STATUS_COLORS.get(self.derived_status, "white")

    @property
    def derived_status(self) -> str:
        if self.status == "archived":
            return "archived"
        if not self.stages:
            return self.status
        return derive_project_status(self.stages)

    def obsidian_url(self, cfg: Config) -> str:
        vault_name = cfg.obsidian_vault.name
        if self.is_folder:
            rel_path = f"{cfg.projects_folder}/{self.slug}/index"
        else:
            rel_path = f"{cfg.projects_folder}/{self.slug}"
        return f"obsidian://open?vault={quote(vault_name)}&file={quote(rel_path)}"

    def frontmatter_dict(self) -> dict:
        d = {
            "title": self.title,
            "slug": self.slug,
            "status": self.status,
            "type": self.type,
            "created": self.created,
            "updated": self.updated,
            "tags": self.tags,
            "github_issues": self.github_issues,
            "github_prs": self.github_prs,
            "sandbox": self.sandbox,
            "branch": self.branch,
            "related_notes": self.related_notes,
        }
        if self.stages:
            d["stages"] = [s.to_dict() for s in self.stages]
        if self.dev_port:
            d["dev_port"] = self.dev_port
        if self.dev_session:
            d["dev_session"] = self.dev_session
        return d

    def stage_progress(self) -> str:
        if not self.stages:
            return "—"
        done = sum(1 for s in self.stages if s.status == "done")
        return f"{done}/{len(self.stages)}"

    def next_available_stage(self) -> Stage | None:
        """Return the first 'ready' stage (dependencies met)."""
        for s in sorted(self.stages, key=lambda x: x.id):
            if s.status == "ready":
                return s
        return None

    def unblocked_stages(self) -> list[Stage]:
        """Return all stages that are 'ready'."""
        return [s for s in self.stages if s.status == "ready"]

    def get_stage(self, stage_id: int) -> Stage | None:
        for s in self.stages:
            if s.id == stage_id:
                return s
        return None

    def blocked_by(self, stage: Stage) -> list[Stage]:
        done_ids = {s.id for s in self.stages if s.status in ("done", "skipped")}
        blocking_ids = [d for d in stage.depends_on if d not in done_ids]
        return [s for s in self.stages if s.id in blocking_ids]


def derive_project_status(stages: list[Stage]) -> str:
    """Derive project status from stage statuses."""
    if not stages:
        return "needs-plan"
    statuses = {s.status for s in stages}
    if all(s in ("done", "skipped") for s in statuses):
        return "done"
    # Anything beyond pending/ready/done/skipped means active work
    if statuses - {"pending", "ready", "done", "skipped"}:
        return "active"
    return "planned"


def auto_promote_ready(project: Project) -> None:
    """Promote stages from 'pending' to 'ready' when all dependencies are met."""
    done_ids = {s.id for s in project.stages if s.status in ("done", "skipped")}
    for s in project.stages:
        if s.status == "pending":
            if not s.depends_on or all(d in done_ids for d in s.depends_on):
                s.status = "ready"


def parse_project(path: Path) -> Project | None:
    text = path.read_text()
    def _safe_int(val: object, default: int = 0) -> int:
        try:
            return int(val)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return default

    m = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict) or "slug" not in fm:
        return None
    stages = []
    for raw_stage in fm.get("stages", []):
        if isinstance(raw_stage, dict):
            stages.append(
                Stage(
                    id=raw_stage.get("id", 0),
                    name=raw_stage.get("name", ""),
                    status=raw_stage.get("status", "pending"),
                    depends_on=raw_stage.get("depends_on", []),
                    github_issues=raw_stage.get("github_issues", []),
                    github_prs=raw_stage.get("github_prs", []),
                )
            )
    proj = Project(
        title=fm.get("title", path.stem),
        slug=fm["slug"],
        status=fm.get("status", "needs-plan"),
        type=fm.get("type", "engineering"),
        created=str(fm.get("created", "")),
        updated=str(fm.get("updated", "")),
        tags=fm.get("tags", []),
        github_issues=fm.get("github_issues", []),
        github_prs=fm.get("github_prs", []),
        sandbox=fm.get("sandbox", ""),
        branch=fm.get("branch", ""),
        related_notes=fm.get("related_notes", []),
        stages=stages,
        dev_port=_safe_int(fm.get("dev_port", 0)),
        dev_session=fm.get("dev_session", ""),
        path=path,
    )
    if proj.stages:
        auto_promote_ready(proj)
    return proj


def load_projects(cfg: Config) -> list[Project]:
    projects_dir = cfg.projects_dir
    if not projects_dir.exists():
        return []
    projects = []
    for d in sorted(projects_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            index = d / "index.md"
            if index.exists():
                p = parse_project(index)
                if p:
                    _migrate_project(p)
                    projects.append(p)
    return projects


def _migrate_project(proj: Project) -> None:
    """Migrate old-format projects: extract ## Notes to notes.md, map old statuses."""
    if not proj.folder or not proj.path:
        return

    # Extract ## Notes from index.md into notes.md if present
    notes_file = proj.folder / "notes.md"
    if not notes_file.exists():
        text = proj.path.read_text()
        notes_match = re.search(r"^## Notes\s*$", text, re.MULTILINE)
        if notes_match:
            notes_content = text[notes_match.end():]
            # Trim at next ## heading (if any other section follows)
            next_section = re.search(r"^## (?!Notes)", notes_content, re.MULTILINE)
            if next_section:
                notes_content = notes_content[:next_section.start()]
            notes_file.write_text(notes_content.strip() + "\n")
            # Remove ## Notes from index.md
            if next_section:
                new_text = text[:notes_match.start()] + text[notes_match.end() + next_section.start():]
            else:
                new_text = text[:notes_match.start()].rstrip() + "\n"
            proj.path.write_text(new_text)

    # Map old statuses to new ones
    _status_map = {
        "ready": "planned",
        "implementing": "active",
        "reviewing": "active",
        "reviewed": "active",
        "awaiting-approval": "active",
        "ci-checking": "active",
        "ci-failing": "active",
        "ci-passing": "active",
        "pr-approved": "active",
        "pr-open": "active",
    }
    if proj.status in _status_map:
        proj.status = _status_map[proj.status]

    # Map old stage statuses
    _stage_status_map = {
        "running": "implemented",
    }
    for s in proj.stages:
        if s.status in _stage_status_map:
            s.status = _stage_status_map[s.status]

    # Create stage folders if they don't exist
    if proj.stages:
        ensure_stage_folders(proj)


def find_project(cfg: Config, slug: str) -> Project:
    for p in load_projects(cfg):
        if p.slug == slug:
            return p
    console.print(f"[red]Project not found: {slug}[/red]")
    raise typer.Exit(1)


def _quick_status(index_path: Path) -> str:
    """Extract status from frontmatter without full YAML parse."""
    try:
        text = index_path.read_text(encoding="utf-8")[:500]
        m = re.search(r"^status:\s*(\S+)", text, re.MULTILINE)
        return m.group(1) if m else ""
    except Exception:
        return ""


def complete_project(incomplete: str) -> list[str]:
    try:
        cfg = load_config()
        slugs = [
            d.name
            for d in cfg.projects_dir.iterdir()
            if d.is_dir()
            and not d.name.startswith(".")
            and (d / "index.md").exists()
            and d.name.startswith(incomplete)
            and _quick_status(d / "index.md") not in ("done", "archived")
        ]
        return sorted(slugs)
    except Exception:
        return []


def update_project_note(project: Project) -> None:
    if not project.path or not project.path.exists():
        return
    text = project.path.read_text()
    m = re.match(r"^---\n.+?\n---\n?", text, re.DOTALL)
    body = text[m.end() :] if m else text
    project.updated = datetime.now().strftime("%Y-%m-%d")
    if project.stages:
        auto_promote_ready(project)
        if project.status != "archived":
            project.status = derive_project_status(project.stages)
    fm = yaml.dump(project.frontmatter_dict(), default_flow_style=False, sort_keys=False)
    project.path.write_text(f"---\n{fm}---\n{body}")


def create_project_note(
    cfg: Config, title: str, slug: str, body: str = "", github_issue: str = ""
) -> Project:
    today = datetime.now().strftime("%Y-%m-%d")
    project_dir = cfg.projects_dir / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    note_path = project_dir / "index.md"
    project = Project(
        title=title,
        slug=slug,
        status="needs-plan",
        created=today,
        updated=today,
        tags=["phoenix"],
        github_issues=[github_issue] if github_issue else [],
        path=note_path,
    )
    fm = yaml.dump(project.frontmatter_dict(), default_flow_style=False, sort_keys=False)
    note_body = textwrap.dedent(f"""\

    ## Objective
    {body or "(to be filled during arc plan)"}

    ## Tasks
    (filled during arc plan)
    """)
    note_path.write_text(f"---\n{fm}---\n{note_body}")

    # Create project-level notes.md
    notes_path = project_dir / "notes.md"
    if not notes_path.exists():
        notes_path.write_text(
            f"# {title} — Notes\n\n### {today}\n"
            f"- Created{f' from issue {github_issue}' if github_issue else ''}\n"
        )

    return project


def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:60]


# ---------------------------------------------------------------------------
# Project folder helpers
# ---------------------------------------------------------------------------


def stage_folder(project: Project, stage: Stage) -> Path:
    """Return the path to a stage's folder: Projects/<slug>/stages/<id>-<name>/"""
    assert project.folder is not None
    return project.folder / "stages" / stage.folder_name()


def stage_plan_path(project: Project, stage: Stage) -> Path:
    return stage_folder(project, stage) / "plan.md"


def stage_notes_path(project: Project, stage: Stage) -> Path:
    return stage_folder(project, stage) / "notes.md"


def project_notes_path(project: Project) -> Path:
    """Return the path to the project-level notes.md."""
    assert project.folder is not None
    return project.folder / "notes.md"


def ensure_stage_folders(project: Project) -> None:
    """Create stage folders with plan.md and notes.md if they don't exist."""
    if not project.folder:
        return
    for s in project.stages:
        sf = stage_folder(project, s)
        sf.mkdir(parents=True, exist_ok=True)
        for fname in ("plan.md", "notes.md"):
            fpath = sf / fname
            if not fpath.exists():
                fpath.write_text("")


def _append_to_notes_file(notes_path: Path, session_type: str, summary: str) -> None:
    """Append a dated entry to a notes.md file."""
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    entry = f"- **{session_type}**: {summary}"

    if not notes_path.exists() or notes_path.stat().st_size == 0:
        notes_path.write_text(f"### {today}\n{entry}\n")
        return

    text = notes_path.read_text()
    today_pattern = re.compile(rf"^### {re.escape(today)}\s*$", re.MULTILINE)
    today_match = today_pattern.search(text)
    if today_match:
        next_heading = re.search(r"^###", text[today_match.end():], re.MULTILINE)
        if next_heading:
            insert_pos = today_match.end() + next_heading.start()
        else:
            insert_pos = len(text)
        text = text[:insert_pos].rstrip() + f"\n{entry}\n" + text[insert_pos:]
    else:
        text = text.rstrip() + f"\n\n### {today}\n{entry}\n"

    notes_path.write_text(text)


def append_session_note(
    project: Project, session_type: str, summary: str, stage: Stage | None = None
) -> None:
    """Append a dated session entry to the appropriate notes.md file."""
    if not project.folder:
        return
    if stage is not None:
        _append_to_notes_file(stage_notes_path(project, stage), session_type, summary)
    else:
        _append_to_notes_file(project_notes_path(project), session_type, summary)


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------


def tmux_session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def tmux_sessions() -> list[str]:
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return result.stdout.strip().splitlines()


def tmux_session_alive(name: str) -> bool:
    """Check if a tmux session has any live (non-dead) panes."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", name, "-F", "#{pane_dead}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return any(line.strip() == "0" for line in result.stdout.strip().splitlines())


def cleanup_stale_sessions(cfg: Config) -> None:
    """Clear dev_port/dev_session for projects whose tmux session is gone."""
    active = tmux_sessions()
    for proj in load_projects(cfg):
        if proj.dev_session and proj.dev_session not in active:
            proj.dev_port = 0
            proj.dev_session = ""
            update_project_note(proj)


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.1)
        return s.connect_ex(("localhost", port)) != 0


def next_free_port(start: int = 6006) -> int:
    port = start
    while not is_port_free(port):
        port += 1
    return port


def notify(title: str, message: str) -> None:
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{message}" with title "{title}"',
        ],
        capture_output=True,
    )
    print("\a", end="", flush=True)


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------


def relative_time(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str
    delta = datetime.now() - dt
    if delta.days == 0:
        return "today"
    elif delta.days == 1:
        return "1d ago"
    else:
        return f"{delta.days}d ago"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def set_tab_title(title: str) -> None:
    """Set the iTerm/terminal tab title via escape sequence."""
    sys.stdout.write(f"\033]1;{title}\007")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.callback()
def dashboard(ctx: typer.Context):
    """arc — project dashboard and scaffolding."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = load_config()
    cleanup_stale_sessions(cfg)
    projects = load_projects(cfg)
    projects = [p for p in projects if p.derived_status != "archived"]
    if not projects:
        console.print("[dim]No projects found. Run [bold]arc sync[/bold] to pull issues.[/dim]")
        return

    active_sessions = tmux_sessions()

    term_width = min(console.width, 140)
    table = Table(show_header=True, header_style="bold", padding=(0, 2), width=term_width)
    table.add_column("Project", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Progress", no_wrap=True)
    table.add_column("PR", no_wrap=True)
    table.add_column("Branch", no_wrap=True, overflow="ellipsis", max_width=30)
    table.add_column("Sessions", no_wrap=True)
    table.add_column("Updated", justify="right", no_wrap=True)

    status_order = {s: i for i, s in enumerate(PROJECT_STATUSES)}
    projects.sort(
        key=lambda p: (
            0 if p.derived_status == "active" else 1,
            status_order.get(p.derived_status, 99),
        )
    )

    has_tmux_sessions = False

    for p in projects:
        # PR link — aggregate from project + all stages
        all_prs = list(p.github_prs)
        for s in p.stages:
            all_prs.extend(s.github_prs)
        pr_display = "[dim]—[/dim]"
        if all_prs:
            pr_url = all_prs[-1]
            pr_match = re.search(r"/pull/(\d+)", pr_url)
            if pr_match:
                pr_display = f"[link={pr_url}]#{pr_match.group(1)}[/link]"

        # Branch — clickable, opens sandbox in VS Code
        branch_display = "[dim]—[/dim]"
        if p.branch:
            if p.sandbox and Path(p.sandbox).exists():
                branch_display = f"[link=vscode://file/{p.sandbox}]{p.branch}[/link]"
            else:
                branch_display = p.branch

        # Sessions — compact indicators for tmux + dev
        session_parts = []
        tmux_matches = [
            s for s in active_sessions
            if s.startswith(f"arc-{p.slug}") and s != f"arc-{p.slug}-dev"
        ]
        for s in tmux_matches:
            has_tmux_sessions = True
            # Extract session type from name: arc-slug -> "impl", arc-slug-ci -> "ci"
            suffix = s[len(f"arc-{p.slug}"):]
            label = suffix.lstrip("-") if suffix else "impl"
            if tmux_session_alive(s):
                session_parts.append(f"[green]{label}[/green]")
            else:
                session_parts.append(f"[yellow]{label}[/yellow]")
        if p.dev_port and p.dev_session and p.dev_session in active_sessions:
            if not is_port_free(p.dev_port):
                session_parts.append(f"[cyan]:{p.dev_port}[/cyan]")
            else:
                session_parts.append(f"[yellow]:{p.dev_port}?[/yellow]")
        session_info = " ".join(session_parts)

        url = p.obsidian_url(cfg)
        status = p.derived_status
        color = PROJECT_STATUS_COLORS.get(status, "white")

        status_display = status

        table.add_row(
            f"[link={url}]{p.slug}[/link]",
            f"[{color}]{status_display}[/{color}]",
            p.stage_progress(),
            pr_display,
            branch_display,
            session_info or "[dim]—[/dim]",
            relative_time(p.updated),
        )

    console.print(table)
    if has_tmux_sessions:
        console.print("[dim]  attach sessions: tmux attach -t arc-<slug>[-ci][/dim]")


@app.command(rich_help_panel="Utilities")
def init():
    """Interactive setup — create config.toml from prompts."""
    config_path = Path(__file__).resolve().parent / "config.toml"
    if config_path.exists():
        overwrite = typer.confirm(f"config.toml already exists. Overwrite?", default=False)
        if not overwrite:
            raise typer.Exit(0)

    console.print("[bold]arc setup[/bold]\n")

    # Core
    vault = typer.prompt(
        "Obsidian vault path",
        default="~/Documents/Obsidian",
    )
    projects_folder = typer.prompt("Projects folder inside vault", default="Projects")
    sandbox_root = typer.prompt(
        "Sandbox root (where git clones go)",
        default="~/Projects/sandboxes",
    )
    branch_prefix = typer.prompt("Git branch prefix (e.g. your username)")

    # GitHub
    github_user = typer.prompt("GitHub username")
    github_repo = typer.prompt("GitHub repo (org/name format)", default=f"{github_user}/my-repo")

    # Agent
    test_cmd = typer.prompt("Test command for sandboxes", default="pytest tests/")
    lint_cmd = typer.prompt("Lint command for sandboxes", default="ruff check .")

    # Organize
    console.print("\n[dim]Vault organizer settings (press Enter to accept defaults):[/dim]")
    max_notes = typer.prompt("Max notes per organizer run", default="20")
    organize_model = typer.prompt("Claude model for organizer", default="claude-sonnet-4-20250514")

    config_content = f"""\
[core]
obsidian_vault = "{vault}"
projects_folder = "{projects_folder}"
sandbox_root = "{sandbox_root}"
branch_prefix = "{branch_prefix}"

[github]
user = "{github_user}"
repo = "{github_repo}"

[agent]
test_cmd = "{test_cmd}"
lint_cmd = "{lint_cmd}"

[organize]
skip_folders = ["Templates", ".obsidian", "Assets"]
max_notes_per_run = {max_notes}
model = "{organize_model}"
"""

    config_path.write_text(config_content)
    console.print(f"\n[green]Config written to {config_path}[/green]")
    console.print(f"  Edit anytime: [bold]{config_path}[/bold]")
    console.print(f"  Next: [bold]arc sync[/bold] to pull issues, or [bold]arc new <title>[/bold] to start a project")


@app.command(rich_help_panel="Project Management")
def sync():
    """Pull GitHub issues and sync PR status from the configured repo."""
    cfg = load_config()
    projects = load_projects(cfg)
    existing_issues: set[str] = set()
    for p in projects:
        for iss in p.github_issues:
            existing_issues.add(iss)

    console.print("[bold]Fetching assigned issues...[/bold]")
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--assignee",
            cfg.github_user,
            "--repo",
            cfg.github_repo,
            "--json",
            "number,title,body,labels",
            "--limit",
            "50",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]gh issue list failed: {result.stderr}[/red]")
        raise typer.Exit(1)

    import json

    issues = json.loads(result.stdout)
    console.print(f"Found {len(issues)} assigned issues.")

    for issue in issues:
        issue_ref = f"{cfg.github_repo}#{issue['number']}"
        if issue_ref in existing_issues:
            console.print(f"  [dim]{issue_ref}: {issue['title']} — already tracked[/dim]")
            continue

        console.print(f"\n[bold]{issue_ref}[/bold]: {issue['title']}")

        options: list[tuple[str, str]] = []
        matching = [
            p
            for p in projects
            if any(
                word in p.title.lower() for word in issue["title"].lower().split() if len(word) > 3
            )
        ]
        for p in matching:
            options.append((f'attach to "{p.slug}" ({p.status})', f"attach:{p.slug}"))
        options.append(("create new project", "create"))
        options.append(("skip", "skip"))

        for i, (label, _) in enumerate(options, 1):
            console.print(f"  {i}. {label}")
        raw = input("  > ").strip()

        action = "skip"
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            action = options[int(raw) - 1][1]
        elif raw.lower().startswith("s"):
            action = "skip"
        elif raw.lower().startswith("c"):
            action = "create"

        if action == "skip":
            continue
        elif action.startswith("attach:"):
            slug = action.split(":", 1)[1]
            proj = None
            for p in projects:
                if p.slug == slug:
                    proj = p
                    break
            if proj:
                proj.github_issues.append(issue_ref)
                update_project_note(proj)
                console.print(f"  [green]Attached to {slug}[/green]")
            else:
                console.print(f"  [red]Project {slug} not found[/red]")
        elif action == "create":
            slug = slugify(issue["title"])
            console.print(f"  Suggested slug: [bold]{slug}[/bold]")
            custom = input("  Enter to accept, or type a custom slug: ").strip()
            if custom:
                slug = custom
            body = (issue.get("body") or "")[:500]
            proj = create_project_note(cfg, issue["title"], slug, body=body, github_issue=issue_ref)
            projects.append(proj)
            existing_issues.add(issue_ref)
            console.print(f"  [green]Created project: {slug}[/green]")

    console.print("\n[bold]Checking open PRs...[/bold]")
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--author",
            cfg.github_user,
            "--repo",
            cfg.github_repo,
            "--json",
            "number,title,url,headRefName",
            "--limit",
            "50",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        prs = json.loads(result.stdout)
        for pr in prs:
            for p in projects:
                if p.branch and p.branch == pr["headRefName"]:
                    pr_ref = pr["url"]
                    if pr_ref not in p.github_prs:
                        p.github_prs.append(pr_ref)
                        update_project_note(p)
                        console.print(f"  Updated {p.slug} with PR #{pr['number']}")

    bare = cfg.bare_repo
    if bare.exists():
        console.print("\n[bold]Updating reference repo...[/bold]")
        subprocess.run(["git", "fetch", "--all"], cwd=bare, capture_output=True)
        console.print("  [green]Done[/green]")

    console.print("\n[green]Sync complete.[/green]")


@app.command(rich_help_panel="Planning")
def plan(project: str = typer.Argument(autocompletion=complete_project)):
    """Launch interactive Claude session to plan a project."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if proj.derived_status not in ("needs-plan", "planned"):
        console.print(
            f"[yellow]Warning: project status is '{proj.derived_status}', not 'needs-plan'[/yellow]"
        )
        if not typer.confirm("Continue anyway?"):
            raise typer.Exit(0)

    # Plans go in the project folder
    today = datetime.now().strftime("%Y-%m-%d")
    plan_path = proj.folder / f"plan-{today}.md" if proj.folder else cfg.projects_dir / f"{proj.slug}-plan.md"
    notes_path = project_notes_path(proj) if proj.folder else None

    stage_instructions = textwrap.dedent("""\

    If this project has multiple stages/phases, define them in the project
    frontmatter:

    stages:
      - id: 1
        name: Stage name
        status: pending
        depends_on: []
      - id: 2
        name: Another stage
        status: pending
        depends_on: [1]

    For each stage, write the plan to:
      {project_folder}/stages/{id}-{slugified-name}/plan.md
    Create these folders if they don't exist.
    """)

    system_prompt = textwrap.dedent(f"""\
        You are planning the project: {proj.title}

        Your job is to:
        1. Read the project note at {proj.path}
        2. Search the codebase and Obsidian vault for relevant context
        3. Discuss the approach with the user
        4. Write a detailed implementation plan with tasks
        {stage_instructions}
        When done:

        1. Write the detailed plan to: {plan_path}
           For staged projects, also write per-stage plans to stages/<id>-<name>/plan.md

        2. Update the project note at {proj.path}:
           - Fill in the Objective and Tasks sections
           - Add any relevant Obsidian note paths to the `related_notes` frontmatter field

        Do NOT update the status in frontmatter — arc handles that.
        Write session notes to: {notes_path or proj.path}

        Sandbox (if exists): {proj.sandbox or cfg.sandbox_root}
        Obsidian vault: {cfg.obsidian_vault}
    """)

    initial_msg = f"Read the project note at {proj.path} and let's plan this project."

    console.print(f"[bold]Launching planning session for: {proj.title}[/bold]")
    console.print("[dim]Chat with Claude to refine the plan. Exit when done.[/dim]\n")

    set_tab_title(f"arc: {project} (plan)")
    _set_project_env(proj)
    plan_dir = Path(proj.sandbox) if proj.sandbox and Path(proj.sandbox).exists() else cfg.obsidian_vault
    subprocess.run(
        [
            CLAUDE_BIN,
            "--dangerously-skip-permissions",
            "--permission-mode",
            "plan",
            "--system-prompt",
            system_prompt,
            initial_msg,
        ],
        cwd=plan_dir,
    )
    set_tab_title("")

    append_session_note(proj, "plan", f"Planning session for {proj.title}")

    # Post-command: arc owns the status transition
    proj = find_project(cfg, project)  # reload to pick up any agent changes
    if proj.status == "needs-plan":
        proj.status = "planned"
    if proj.stages:
        auto_promote_ready(proj)
        ensure_stage_folders(proj)
    update_project_note(proj)


@app.command("stage", rich_help_panel="Planning")
def stage_cmd(
    project: str = typer.Argument(autocompletion=complete_project),
    add: str = typer.Option(None, "--add", help="Add a new stage with this name"),
    depends_on: str = typer.Option(
        None, "--depends-on", help="Comma-separated stage IDs this depends on"
    ),
    plan_stage: int = typer.Option(None, "--plan", help="Launch Claude planning for this stage ID"),
):
    """Manage stages for a project."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if add:
        deps = []
        if depends_on:
            deps = [int(x.strip()) for x in depends_on.split(",")]
        next_id = max((s.id for s in proj.stages), default=0) + 1
        new_stage = Stage(
            id=next_id,
            name=add,
            status="pending",
            depends_on=deps,
        )
        proj.stages.append(new_stage)
        update_project_note(proj)
        # Create stage folder with plan.md and notes.md
        if proj.folder:
            sf = stage_folder(proj, new_stage)
            sf.mkdir(parents=True, exist_ok=True)
            for fname in ("plan.md", "notes.md"):
                fpath = sf / fname
                if not fpath.exists():
                    fpath.write_text("")
        console.print(f"[green]Added stage {next_id}: {add}[/green]")
        if deps:
            console.print(f"  Depends on: {', '.join(str(d) for d in deps)}")
        if proj.folder:
            console.print(f"  Folder: stages/{new_stage.folder_name()}/")
        return

    if plan_stage is not None:
        s = proj.get_stage(plan_stage)
        if not s:
            console.print(f"[red]Stage {plan_stage} not found[/red]")
            raise typer.Exit(1)
        if not proj.is_folder:
            console.print(
                "[red]Stage planning requires folder-per-project format. Run: arc migrate[/red]"
            )
            raise typer.Exit(1)

        plan_path = stage_plan_path(proj, s)
        notes_path = stage_notes_path(proj, s)
        system_prompt = textwrap.dedent(f"""\
            You are planning stage {s.id} of project: {proj.title}

            Stage: {s.name}

            Your job is to write a detailed plan for this stage.
            Save the plan to: {plan_path}

            The plan should include:
            - Goal
            - Steps (specific, actionable)
            - Expected outputs
            - Done criteria

            Write any session notes to: {notes_path}
            Do NOT update status in frontmatter — arc handles that.

            Project note: {proj.path}
            Sandbox (if exists): {proj.sandbox or cfg.sandbox_root}
            Obsidian vault: {cfg.obsidian_vault}
        """)

        initial_msg = f"Read the project note at {proj.path} and let's plan stage {s.id}: {s.name}"

        console.print(f"[bold]Planning stage {s.id}: {s.name}[/bold]")
        _set_project_env(proj)
        os.execvp(
            CLAUDE_BIN,
            [
                CLAUDE_BIN,
                "--dangerously-skip-permissions",
                "--permission-mode",
                "plan",
                "--system-prompt",
                system_prompt,
                initial_msg,
            ],
        )
        return

    # Default: list stages
    if not proj.stages:
        console.print(f"[dim]No stages defined for {proj.slug}.[/dim]")
        console.print(f'Add with: [bold]arc stage {proj.slug} --add "Stage name"[/bold]')
        return

    active_sessions = tmux_sessions()

    table = Table(show_header=True, header_style="bold", padding=(0, 2))
    table.add_column("#", justify="right")
    table.add_column("Stage")
    table.add_column("Status")
    table.add_column("PR", no_wrap=True)
    table.add_column("Depends")
    table.add_column("Sessions", no_wrap=True)

    for s in sorted(proj.stages, key=lambda x: x.id):
        color = STAGE_STATUS_COLORS.get(s.status, "white")
        status_str = f"[{color}]{s.status}[/{color}]"

        # PR display
        pr_display = "[dim]—[/dim]"
        if s.github_prs:
            pr_url = s.github_prs[-1]
            pr_match = re.search(r"/pull/(\d+)", pr_url)
            if pr_match:
                pr_display = f"[link={pr_url}]#{pr_match.group(1)}[/link]"

        deps_str = ", ".join(str(d) for d in s.depends_on) if s.depends_on else "—"

        # Sessions for this stage
        stage_sessions = [
            sess for sess in active_sessions
            if sess.startswith(f"arc-{proj.slug}")
        ]
        sess_str = "[dim]—[/dim]"
        if stage_sessions:
            parts = []
            for sess in stage_sessions:
                suffix = sess[len(f"arc-{proj.slug}"):]
                label = suffix.lstrip("-") if suffix else "impl"
                parts.append(f"[green]{label}[/green]")
            sess_str = " ".join(parts)

        table.add_row(str(s.id), s.name, status_str, pr_display, deps_str, sess_str)

    console.print(f"\n[bold]{proj.title}[/bold] — {proj.stage_progress()} stages done\n")
    console.print(table)


@app.command(rich_help_panel="Development")
def sandbox(project: str = typer.Argument(autocompletion=complete_project)):
    """Create an isolated Phoenix sandbox (worktree clone) for a project."""
    cfg = load_config()
    proj = find_project(cfg, project)
    sandbox_path = cfg.sandbox_root / proj.slug
    branch_name = f"{cfg.branch_prefix}/{proj.slug}"

    if sandbox_path.exists():
        console.print(f"[yellow]Sandbox already exists: {sandbox_path}[/yellow]")
        proj.sandbox = str(sandbox_path)
        proj.branch = branch_name
        update_project_note(proj)
        console.print(f"  Branch: {branch_name}")
        return

    bare = cfg.bare_repo
    if not bare.exists():
        console.print(
            "[bold]Creating bare reference repo (first time, may take a minute)...[/bold]"
        )
        subprocess.run(
            ["git", "clone", "--bare", f"https://github.com/{cfg.github_repo}.git", str(bare)],
            check=True,
        )
    else:
        console.print("[dim]Updating reference repo...[/dim]")
        subprocess.run(["git", "fetch", "--all"], cwd=bare, capture_output=True)

    console.print(f"[bold]Cloning to {sandbox_path}...[/bold]")
    subprocess.run(
        [
            "git",
            "clone",
            "--reference",
            str(bare),
            "--shared",
            f"https://github.com/{cfg.github_repo}.git",
            str(sandbox_path),
        ],
        check=True,
    )

    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=sandbox_path,
        check=True,
    )

    # Set up Python environment
    console.print("[dim]Running uv sync --python 3.10...[/dim]")
    subprocess.run(
        ["uv", "sync", "--python", "3.10"],
        cwd=sandbox_path,
        check=False,
    )

    proj.sandbox = str(sandbox_path)
    proj.branch = branch_name
    update_project_note(proj)

    console.print(f"\n[green]Sandbox ready:[/green] {sandbox_path}")
    console.print(f"[green]Branch:[/green] {branch_name}")


@app.command(rich_help_panel="Development")
def implement(
    project: str = typer.Argument(autocompletion=complete_project),
    stage_id: int = typer.Argument(None),
    bg: bool = typer.Option(False, "--bg", help="Run autonomously in background (tmux)"),
):
    """Implement a project or stage. Interactive by default, --bg for autonomous."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if not proj.sandbox or not Path(proj.sandbox).exists():
        console.print(f"[red]No sandbox found. Run: arc sandbox {proj.slug}[/red]")
        raise typer.Exit(1)

    sandbox_path = Path(proj.sandbox)

    # Source model API keys from main phoenix .env
    _load_env_keys(cfg)

    # --- Background mode: autonomous implement → review pipeline in tmux ---
    if bg:
        _implement_bg(cfg, proj, sandbox_path)
        return

    # --- Interactive mode (default) ---
    if proj.stages:
        _implement_interactive_staged(cfg, proj, sandbox_path, stage_id)
    else:
        _implement_interactive_simple(cfg, proj, sandbox_path)


DEFAULT_REVIEW_TOOL = "codex"
DEFAULT_REVIEW_MODEL = ""  # empty = tool default


def _run_review(
    cfg: Config,
    proj: Project,
    sandbox_path: Path,
    stage_context: str = "",
    tool: str = DEFAULT_REVIEW_TOOL,
    model: str = DEFAULT_REVIEW_MODEL,
) -> None:
    """Run AI code review using claude or codex."""
    console.print()
    tool_label = tool
    if model:
        tool_label += f" ({model})"
    console.print(f"[bold cyan]Starting code review with {tool_label}...[/bold cyan]")

    # Gather stage plans context
    plans_context = ""
    if proj.stages and proj.folder:
        for s in proj.stages:
            plan_path = stage_plan_path(proj, s)
            if plan_path.exists() and plan_path.stat().st_size > 0:
                plans_context += f"\n### Stage {s.id}: {s.name}\n{plan_path.read_text()}\n"

    # Determine notes path
    notes_path = project_notes_path(proj) if proj.folder else proj.path

    review_prompt = textwrap.dedent(f"""\
        You are reviewing an implementation for project: {proj.title}

        Read the project note at: {proj.path}

        {f"## Stage Context{chr(10)}{stage_context}" if stage_context else ""}

        {f"## Implementation Plans{plans_context}" if plans_context else ""}

        ## Review Instructions
        Your primary job is a **code quality review**: focus on feature behavior correctness, edge cases, API ergonomics, logic errors, and subtle bugs.

        1. Run `git diff main` to see all changes
        2. Review each changed file for correctness against the project requirements and plans above
        3. Look for: logic errors, missed edge cases, off-by-one bugs, incorrect API usage, poor error messages, naming issues
        4. As a secondary check, run tests (`{cfg.test_cmd}`) and lint (`{cfg.lint_cmd}`)
        5. Fix any issues you find and commit fixes: stage ONLY the files you modified (never `git add .` or `git add -A`) with simple one-line commit messages (no co-authors). Before ending, commit any remaining modified files.
        6. Write review notes to: {notes_path}

        Do NOT update status in frontmatter — arc handles that.
        Do NOT write summary files into the codebase/sandbox.
    """)

    if tool == "codex":
        cmd = ["codex"]
        if model:
            cmd += ["-m", model]
        cmd += ["-C", str(sandbox_path), review_prompt]
    else:
        cmd = [CLAUDE_BIN, "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        cmd += ["--system-prompt", review_prompt, "Review the implementation."]

    subprocess.run(cmd, cwd=sandbox_path, env=_clean_env())

    console.print(f"[green]{tool} review complete.[/green]")


def _implement_interactive_staged(
    cfg: Config, proj: Project, sandbox_path: Path, stage_id: int | None
) -> None:
    """Interactive implementation of a single stage."""
    # Pick stage
    if stage_id is None:
        stage = proj.next_available_stage()
        if not stage:
            console.print("[yellow]No available stages. All done or blocked.[/yellow]")
            raise typer.Exit(0)
        stage_id = stage.id
    else:
        stage = proj.get_stage(stage_id)

    if not stage:
        console.print(f"[red]Stage {stage_id} not found[/red]")
        raise typer.Exit(1)

    # Validate deps
    if stage.status == "pending":
        console.print(f"[red]Stage {stage_id} is pending (dependencies not met).[/red]")
        raise typer.Exit(1)

    if stage.status == "done":
        if not typer.confirm(f"Stage {stage_id} is done. Re-run?", default=False):
            raise typer.Exit(0)

    if stage.status in ("implemented", "reviewed"):
        if not typer.confirm(f"Stage {stage_id} is '{stage.status}'. Re-implement?", default=False):
            raise typer.Exit(0)

    # Build context from dependent stages
    dep_context = ""
    if proj.folder:
        for dep_id in stage.depends_on:
            dep_stage = proj.get_stage(dep_id)
            if dep_stage:
                for path_fn, label in [(stage_plan_path, "Plan"), (stage_notes_path, "Notes")]:
                    p = path_fn(proj, dep_stage)
                    if p.exists() and p.stat().st_size > 0:
                        dep_context += f"\n### Stage {dep_stage.id} ({dep_stage.name}) — {label}\n{p.read_text()}\n"

    # Current stage plan
    plan_content = ""
    notes_path = ""
    if proj.folder:
        plan_path = stage_plan_path(proj, stage)
        notes_path = str(stage_notes_path(proj, stage))
        if plan_path.exists():
            plan_content = plan_path.read_text()

    system_prompt = textwrap.dedent(f"""\
        You are working on stage {stage.id} of project: {proj.title}

        Stage: {stage.name}

        ## Plan
        {plan_content if plan_content else "No plan file found. Ask the user what to do."}

        {"## Prior Stages" + dep_context if dep_context else ""}

        Project sandbox: {sandbox_path}
        Branch: {proj.branch}

        ## Git
        Use git to track your changes. After every meaningful change, stage ONLY the files you modified for this feature (never `git add .` or `git add -A`) and commit with a simple one-line message (no co-authors). Before ending your session, commit any remaining modified files.

        ## Notes
        Write session notes to: {notes_path}
        Do NOT update status in frontmatter — arc handles that.

        When you've completed all steps, tell the user.
    """)

    console.print(f"[bold]Implementing stage {stage.id}: {stage.name}[/bold]")
    console.print(f"[dim]Sandbox: {sandbox_path}[/dim]\n")

    set_tab_title(f"arc: {proj.slug} (impl)")
    _set_project_env(proj)
    initial_msg = f"Let's work on stage {stage.id}: {stage.name}"
    subprocess.run(
        [CLAUDE_BIN, "--dangerously-skip-permissions", "--system-prompt", system_prompt, initial_msg],
        cwd=sandbox_path,
        env=_clean_env(),
    )
    set_tab_title("")

    # Post-command: arc owns the status transition
    append_session_note(proj, "implement", f"Stage {stage.id} ({stage.name}) implementation completed", stage=stage)
    stage.status = "implemented"
    auto_promote_ready(proj)
    update_project_note(proj)

    console.print(f"[green]Stage {stage.id} marked implemented.[/green]")
    console.print(f"  Review: [bold]arc review {proj.slug}[/bold]")


def _implement_interactive_simple(cfg: Config, proj: Project, sandbox_path: Path) -> None:
    """Interactive implementation for non-staged projects."""
    notes_path = project_notes_path(proj) if proj.folder else None

    system_prompt = textwrap.dedent(f"""\
        You are working on project: {proj.title}

        Read the project note at: {proj.path}

        Project sandbox: {sandbox_path}
        Branch: {proj.branch}

        ## Git
        Use git to track your changes. After every meaningful change, stage ONLY the files you modified for this feature (never `git add .` or `git add -A`) and commit with a simple one-line message (no co-authors). Before ending your session, commit any remaining modified files.

        ## Notes
        Write session notes to: {notes_path or proj.path}
        Do NOT update status in frontmatter — arc handles that.

        When you've completed all tasks, tell the user.
    """)

    console.print(f"[bold]Implementing: {proj.title}[/bold]")
    console.print(f"[dim]Sandbox: {sandbox_path}[/dim]\n")

    set_tab_title(f"arc: {proj.slug} (impl)")
    _set_project_env(proj)
    subprocess.run(
        [
            CLAUDE_BIN,
            "--dangerously-skip-permissions",
            "--system-prompt",
            system_prompt,
            f"Let's implement {proj.title}.",
        ],
        cwd=sandbox_path,
        env=_clean_env(),
    )
    set_tab_title("")

    append_session_note(proj, "implement", f"Implementation session for {proj.title}")

    console.print()
    console.print(f"[green]Implementation complete.[/green]")
    console.print(f"  Review: [bold]arc review {proj.slug}[/bold]")
    console.print(f"  Approve: [bold]arc approve {proj.slug}[/bold]")


def _implement_bg(cfg: Config, proj: Project, sandbox_path: Path) -> None:
    """Autonomous implementation in tmux background."""
    notes_path = project_notes_path(proj) if proj.folder else proj.path

    claude_md = textwrap.dedent(f"""\
        # Project: {proj.title}

        You are implementing this project autonomously. Follow the plan below.

        ## Instructions
        1. Read this file carefully
        2. Implement all tasks listed below
        3. Run tests: `{cfg.test_cmd}`
        4. Run lint: `{cfg.lint_cmd}`
        5. Fix any failures
        6. Commit your work after every meaningful change: stage ONLY the files you modified for this feature (never `git add .` or `git add -A`) and commit with a simple one-line message (no co-authors). Before ending your session, commit any remaining modified files.
        7. When done, write session notes to: {notes_path}

        Do NOT update status in frontmatter — arc handles that.
        Do NOT write summary files into the codebase/sandbox.

        ## Project Note
        Read the project note at: {proj.path}
    """)
    (sandbox_path / "CLAUDE.md").write_text(claude_md)

    # Build export lines for model API keys
    env_exports = ""
    for key in MODEL_API_KEYS:
        val = os.environ.get(key, "")
        if val:
            escaped = val.replace("'", "'\\''")
            env_exports += f"export {key}='{escaped}'\n"

    orchestrator = textwrap.dedent(f"""\
        #!/bin/bash
        set -e
        cd "{sandbox_path}"
        export ARC_PROJECT_SLUG="{proj.slug}"

        # Model API keys
        {env_exports}
        # Strip conda/virtualenv env vars to avoid package resolution issues
        unset CONDA_DEFAULT_ENV CONDA_PREFIX CONDA_SHLVL CONDA_EXE
        unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT
        export PATH=$(echo "$PATH" | tr ':' '\\n' | grep -v -e miniconda -e anaconda -e '\\.venv' | tr '\\n' ':' | sed 's/:$//')

        echo "=== arc: Starting implementation (Claude) for {proj.slug} ==="

        {CLAUDE_BIN} --dangerously-skip-permissions -p "Read CLAUDE.md. Implement all tasks. Run tests. Track your changes with git: after every meaningful change, stage ONLY the files you modified (never git add . or git add -A) and commit with a simple one-line message (no co-authors). Before finishing, commit any remaining modified files. Write session notes to {notes_path}. NEVER write summary/notes/review files into the codebase."

        osascript -e 'display notification "Implementation complete for {proj.slug}. Run: arc review {proj.slug}" with title "arc: {proj.slug}"'
        echo -e "\\a"
        echo "=== arc: {proj.slug} implementation complete. Review with: arc review {proj.slug} ==="
    """)

    script_path = sandbox_path / ".arc-orchestrate.sh"
    script_path.write_text(orchestrator)
    script_path.chmod(0o755)

    sess_name = f"arc-{proj.slug}"
    if tmux_session_exists(sess_name):
        console.print(f"[yellow]tmux session '{sess_name}' already exists.[/yellow]")
        console.print(f"Attach with: [bold]tmux attach -t {sess_name}[/bold]")
        raise typer.Exit(1)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", sess_name, f"bash {script_path}"],
        check=True,
    )

    update_project_note(proj)

    console.print(f"[green]Pipeline launched in tmux session: {sess_name}[/green]")
    console.print(f"  Attach: [bold]tmux attach -t {sess_name}[/bold]")
    console.print(f"  When done, run: [bold]arc approve {proj.slug}[/bold]")


@app.command(rich_help_panel="Review & Ship")
def approve(
    project: str = typer.Argument(autocompletion=complete_project),
    stage: int = typer.Option(None, "--stage", "-s", help="Stage ID to approve (auto-detects first 'reviewed' stage if omitted)"),
):
    """Push branch, create PR, and launch CI monitor."""
    cfg = load_config()
    proj = find_project(cfg, project)

    # For staged projects, resolve which stage is being approved
    approving_stage: Stage | None = None
    if proj.stages:
        if stage is not None:
            approving_stage = proj.get_stage(stage)
            if not approving_stage:
                console.print(f"[red]Stage {stage} not found[/red]")
                raise typer.Exit(1)
            if approving_stage.status != "reviewed":
                console.print(
                    f"[red]Stage {stage} ({approving_stage.name}) status is "
                    f"'{approving_stage.status}', expected 'reviewed'[/red]"
                )
                raise typer.Exit(1)
        else:
            # Auto-detect first reviewed stage
            reviewed = [s for s in proj.stages if s.status == "reviewed"]
            if reviewed:
                approving_stage = reviewed[0]
                console.print(
                    f"[dim]Auto-detected stage {approving_stage.id}: "
                    f"{approving_stage.name}[/dim]"
                )

    if approving_stage:
        if approving_stage.status == "pr-open":
            console.print(f"[yellow]Stage {approving_stage.id} already has a PR open[/yellow]")
            if approving_stage.github_prs:
                console.print(f"  {approving_stage.github_prs[-1]}")
            raise typer.Exit(0)

    sandbox_path = Path(proj.sandbox)
    if not sandbox_path.exists():
        console.print("[red]Sandbox not found[/red]")
        raise typer.Exit(1)

    # Generate PR summary from diff stat and commit log
    diff_stat = subprocess.run(
        ["git", "diff", "--stat", "origin/main"],
        cwd=sandbox_path, capture_output=True, text=True,
    ).stdout.strip()
    commit_log = subprocess.run(
        ["git", "log", "--oneline", "origin/main..HEAD"],
        cwd=sandbox_path, capture_output=True, text=True,
    ).stdout.strip()

    summary = f"Implementation of: {proj.title}"
    if diff_stat or commit_log:
        ai_prompt = (
            f"Given this diff stat and commit log for a PR titled '{proj.title}', "
            f"write a brief PR description as 3-5 bullet points. "
            f"Be concise and technical. Do not reference project management tools or notes.\n\n"
            f"Diff stat:\n{diff_stat}\n\nCommit log:\n{commit_log}"
        )
        ai_result = subprocess.run(
            [CLAUDE_BIN, "-p", ai_prompt],
            capture_output=True, text=True, timeout=60,
        )
        if ai_result.returncode == 0 and ai_result.stdout.strip():
            summary = ai_result.stdout.strip()

    # Safety check: warn if any arc working files ended up tracked in git
    arc_files = list(sandbox_path.glob(".arc-*.md")) + list(sandbox_path.glob(".arc-*.sh"))
    tracked_arc_files = []
    for f in arc_files:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(f)],
            cwd=sandbox_path,
            capture_output=True,
        )
        if result.returncode == 0:
            tracked_arc_files.append(str(f))
    if tracked_arc_files:
        console.print(
            f"[red bold]ERROR: {len(tracked_arc_files)} arc working file(s) are tracked in git:[/red bold]"
        )
        for f in tracked_arc_files:
            console.print(f"  [red]{f}[/red]")
        console.print("[red]These must not be in the codebase. Removing from git and committing...[/red]")
        subprocess.run(["git", "rm", "--cached", *tracked_arc_files], cwd=sandbox_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: remove accidentally tracked arc working files"],
            cwd=sandbox_path,
            check=True,
        )

    console.print(f"[bold]Pushing branch {proj.branch}...[/bold]")
    subprocess.run(
        ["git", "push", "-u", "origin", proj.branch],
        cwd=sandbox_path,
        check=True,
    )

    console.print("[bold]Creating PR...[/bold]")
    # Use pr_title from frontmatter, or prompt for a conventional commit title
    if proj.pr_title:
        pr_title = proj.pr_title
    else:
        # Infer a default conventional commit title from project metadata
        _TAG_TO_TYPE = {
            "bug": "fix",
            "bugfix": "fix",
            "fix": "fix",
            "docs": "docs",
            "documentation": "docs",
            "refactor": "refactor",
            "perf": "perf",
            "performance": "perf",
            "test": "test",
            "tests": "test",
            "ci": "ci",
            "build": "build",
            "style": "style",
            "chore": "chore",
        }
        cc_type = "feat"
        for tag in proj.tags:
            if tag.lower() in _TAG_TO_TYPE:
                cc_type = _TAG_TO_TYPE[tag.lower()]
                break
        # Use first tag that looks like a scope (not a type keyword)
        scope_candidates = [t for t in proj.tags if t.lower() not in _TAG_TO_TYPE]
        scope = f"({scope_candidates[0]})" if scope_candidates else ""
        stage_suffix = f" (stage {approving_stage.id}: {approving_stage.name.lower()})" if approving_stage else ""
        default_title = f"{cc_type}{scope}: {proj.title.lower()}{stage_suffix}"
        if len(default_title) > 70:
            default_title = default_title[:67] + "..."
        console.print(
            "[dim]PR title must follow conventional commits "
            "(e.g. feat(scope): description, fix: description)[/dim]"
        )
        pr_title = typer.prompt("PR title", default=default_title)
    if len(pr_title) > 70:
        pr_title = pr_title[:67] + "..."

    issue_refs = ""
    if proj.github_issues:
        # Use "Part of" for stage PRs with remaining stages, "Closes" for final
        remaining = approving_stage and any(
            s.status not in ("done", "skipped") and s.id != approving_stage.id
            for s in proj.stages
        )
        verb = "Part of" if remaining else "Closes"
        issue_refs = f"\n\n{verb} " + ", ".join(
            f"#{ref.split('#')[-1]}" if "#" in ref else ref for ref in proj.github_issues
        )

    pr_body = f"{summary}{issue_refs}"

    result = subprocess.run(
        ["gh", "pr", "create", "--repo", cfg.github_repo, "--title", pr_title, "--body", pr_body],
        cwd=sandbox_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]PR creation failed: {result.stderr}[/red]")
        raise typer.Exit(1)

    pr_url = result.stdout.strip()
    console.print(f"[green]PR created: {pr_url}[/green]")

    # Track PR on the stage (if staged) and on the project
    if approving_stage:
        approving_stage.status = "pr-open"
        approving_stage.github_prs.append(pr_url)
    proj.github_prs.append(pr_url)
    update_project_note(proj)

    sess_name = f"arc-{proj.slug}-ci"
    project_note_path = str(proj.path) if proj.path else ""

    # Determine notes file for CI events
    if approving_stage and proj.folder:
        ci_notes_file = str(stage_notes_path(proj, approving_stage))
    elif proj.folder:
        ci_notes_file = str(project_notes_path(proj))
    else:
        ci_notes_file = ""

    # Build the merge handler
    if approving_stage:
        merge_handler = f'''
                echo "=== arc: PR merged! ==="
                python3 -c "
import yaml, re
from pathlib import Path
p = Path('{project_note_path}')
text = p.read_text()
m = re.match(r'^---\\n(.+?)\\n---', text, re.DOTALL)
if m:
    fm = yaml.safe_load(m.group(1))
    body = text[m.end():]
    for s in fm.get('stages', []):
        if s['id'] == {approving_stage.id}:
            s['status'] = 'done'
    # Auto-promote: pending stages with met deps become ready
    done_ids = {{s['id'] for s in fm.get('stages', []) if s['status'] in ('done', 'skipped')}}
    for s in sorted(fm.get('stages', []), key=lambda x: x['id']):
        if s['status'] == 'pending' and all(d in done_ids for d in s.get('depends_on', [])):
            s['status'] = 'ready'
    # Derive project status
    statuses = {{s['status'] for s in fm.get('stages', [])}}
    if all(st in ('done', 'skipped') for st in statuses):
        fm['status'] = 'done'
    elif statuses - {{'pending', 'ready', 'done', 'skipped'}}:
        fm['status'] = 'active'
    else:
        fm['status'] = 'planned'
    new_fm = yaml.dump(fm, default_flow_style=False, sort_keys=False)
    p.write_text('---\\n' + new_fm + '---\\n' + body)
"
                [ -n "{ci_notes_file}" ] && echo "- **ci**: PR merged" >> "{ci_notes_file}"
                osascript -e 'display notification "Stage {approving_stage.id} merged!" with title "arc: {proj.slug}"'
                break'''
    else:
        merge_handler = f'''
                echo "=== arc: PR merged! ==="
                tmux kill-session -t "arc-{proj.slug}-dev" 2>/dev/null || true
                python3 -c "
import re
from pathlib import Path
p = Path('{project_note_path}')
text = p.read_text()
text = re.sub(r'status: \\w[\\w-]*', 'status: done', text, count=1)
text = re.sub(r'\\ndev_port:.*', '', text)
text = re.sub(r'\\ndev_session:.*', '', text)
p.write_text(text)
"
                [ -n "{ci_notes_file}" ] && echo "- **ci**: PR merged" >> "{ci_notes_file}"
                osascript -e 'display notification "PR merged!" with title "arc: {proj.slug}"'
                break'''

    ci_script = textwrap.dedent(f"""\
        #!/bin/bash
        cd "{sandbox_path}"

        echo "=== arc: Monitoring CI for {proj.slug} ==="

        append_note() {{
            [ -n "{ci_notes_file}" ] && echo "- **ci**: $1" >> "{ci_notes_file}"
        }}

        prev_status=""

        while true; do
            sleep 60

            state=$(gh pr view --repo {cfg.github_repo} --json state -q '.state' 2>/dev/null || echo "unknown")

            if [ "$state" = "MERGED" ]; then
{merge_handler}
            fi

            # Check review/approval status
            review_decision=$(gh pr view --repo {cfg.github_repo} --json reviewDecision -q '.reviewDecision' 2>/dev/null || echo "")

            checks=$(gh pr checks --repo {cfg.github_repo} 2>/dev/null || echo "pending")

            # Determine current status
            if [ "$review_decision" = "APPROVED" ]; then
                new_status="pr-approved"
            elif echo "$checks" | grep -qi "fail"; then
                new_status="ci-failing"
            elif echo "$checks" | grep -qi "pass"; then
                new_status="ci-passing"
            else
                new_status="ci-checking"
            fi

            # Only update + notify on status change
            if [ "$new_status" != "$prev_status" ]; then
                echo "=== arc: status -> $new_status ==="
                append_note "$new_status"

                case "$new_status" in
                    ci-failing)
                        osascript -e 'display notification "CI failing — launching fix agent" with title "arc: {proj.slug}"'
                        {CLAUDE_BIN} --dangerously-skip-permissions -p "CI is failing on this PR. Run gh pr checks to see failures. Read the failing logs. Fix the issues. Run tests locally. Stage ONLY the files you modified (never git add . or git add -A) and commit with a simple one-line message (no co-authors). Push."
                        append_note "Auto-fix pushed"
                        prev_status=""
                        continue
                        ;;
                    ci-passing)
                        osascript -e 'display notification "CI passing" with title "arc: {proj.slug}"'
                        ;;
                    pr-approved)
                        osascript -e 'display notification "PR approved!" with title "arc: {proj.slug}"'
                        ;;
                esac
                prev_status="$new_status"
            fi
        done
    """)

    ci_script_path = sandbox_path / ".arc-ci-monitor.sh"
    ci_script_path.write_text(ci_script)
    ci_script_path.chmod(0o755)

    if not tmux_session_exists(sess_name):
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", sess_name, f"bash {ci_script_path}"],
            check=True,
        )
        console.print(f"[green]CI monitor launched: {sess_name}[/green]")

    console.print(f"\n[bold]Done![/bold] PR: {pr_url}")


@app.command("note", rich_help_panel="Project Management")
def note_cmd(project: str = typer.Argument(autocompletion=complete_project)):
    """Open a project's Obsidian note."""
    cfg = load_config()
    proj = find_project(cfg, project)
    url = proj.obsidian_url(cfg)
    subprocess.run(["open", url])
    console.print(f"[green]Opened {proj.slug} in Obsidian[/green]")


@app.command(rich_help_panel="Utilities")
def chat(project: str = typer.Argument(autocompletion=complete_project)):
    """Launch an informal Claude chat with project context."""
    cfg = load_config()
    proj = find_project(cfg, project)

    note_path = proj.path
    sandbox_info = f"Sandbox path: {proj.sandbox}" if proj.sandbox else "No sandbox configured."

    notes_path = project_notes_path(proj) if proj.folder else note_path

    system_prompt = textwrap.dedent(f"""\
        Project: {proj.title}
        Project note: {note_path}
        Session notes: {notes_path}
        {sandbox_info}
        Obsidian vault: {cfg.obsidian_vault}

        You have context about this project. The user wants to chat informally — answer questions, brainstorm, help think through problems. If they reference code, you can read files in the sandbox path.
    """)

    initial_msg = f"Read the project note at {note_path}"

    console.print(f"[bold]Chatting about: {proj.title}[/bold]")
    console.print("[dim]Informal Claude session with project context.[/dim]\n")

    set_tab_title(f"arc: {project} (chat)")
    _set_project_env(proj)
    chat_dir = Path(proj.sandbox) if proj.sandbox and Path(proj.sandbox).exists() else cfg.obsidian_vault
    subprocess.run(
        [
            CLAUDE_BIN,
            "--dangerously-skip-permissions",
            "--system-prompt",
            system_prompt,
            initial_msg,
        ],
        cwd=chat_dir,
    )
    set_tab_title("")

    append_session_note(proj, "chat", f"Chat session about {proj.title}")


@app.command(rich_help_panel="Review & Ship")
def review(
    project: str = typer.Argument(autocompletion=complete_project),
    tool: str = typer.Option(
        DEFAULT_REVIEW_TOOL, "--tool", "-t", help="Review tool: 'claude' or 'codex'"
    ),
    model: str = typer.Option(
        DEFAULT_REVIEW_MODEL, "--model", "-m", help="Model override (e.g. 'gpt-5.3-codex', 'claude-sonnet-4-20250514')"
    ),
):
    """Run AI code review on a project sandbox."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if tool not in ("claude", "codex"):
        console.print(f"[red]Unknown tool '{tool}'. Use 'claude' or 'codex'.[/red]")
        raise typer.Exit(1)

    if not proj.sandbox or not Path(proj.sandbox).exists():
        console.print(f"[red]No sandbox found. Run: arc sandbox {proj.slug}[/red]")
        raise typer.Exit(1)

    sandbox_path = Path(proj.sandbox)

    set_tab_title(f"arc: {project} (review)")
    _run_review(cfg, proj, sandbox_path, tool=tool, model=model)
    set_tab_title("")

    # Post-command: mark the first 'implemented' stage as 'reviewed'
    proj = find_project(cfg, project)  # reload
    if proj.stages:
        for s in proj.stages:
            if s.status == "implemented":
                s.status = "reviewed"
                append_session_note(proj, "review", f"Code review completed for stage {s.id} ({s.name})", stage=s)
                break
    else:
        append_session_note(proj, "review", f"Code review completed for {proj.title}")
    update_project_note(proj)


@app.command(rich_help_panel="Development")
def editor(
    project: str = typer.Argument(autocompletion=complete_project),
    use_cursor: bool = typer.Option(False, "--cursor", help="Open in Cursor instead of VS Code"),
):
    """Open project sandbox in VS Code (or Cursor with --cursor) with changed files."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if not proj.sandbox or not Path(proj.sandbox).exists():
        console.print(f"[red]No sandbox found. Run: arc sandbox {proj.slug}[/red]")
        raise typer.Exit(1)

    sandbox_path = Path(proj.sandbox)
    editor_bin = "cursor" if use_cursor else "code"
    editor_name = "Cursor" if use_cursor else "VS Code"

    # Fetch latest main so diff is accurate
    subprocess.run(
        ["git", "fetch", "origin", "main"],
        cwd=sandbox_path,
        capture_output=True,
    )

    # Get files changed in this branch since diverging from main (committed + uncommitted)
    changed = set()
    # Committed changes on this branch
    result = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=sandbox_path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        changed.update(result.stdout.strip().splitlines())
    # Uncommitted changes (staged + unstaged)
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=sandbox_path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        changed.update(result.stdout.strip().splitlines())
    changed_files = [str(sandbox_path / f) for f in sorted(changed)]

    cmd = [editor_bin, str(sandbox_path)] + changed_files
    console.print(f"[green]Opening {proj.slug} in {editor_name}[/green]")
    if changed_files:
        console.print(f"[dim]{len(changed_files)} changed file(s)[/dim]")
    subprocess.run(cmd)


# Env var names to carry over from the main phoenix .env
MODEL_API_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "KAGGLE_USERNAME",
    "KAGGLE_KEY",
]

# Phoenix cloud vars to unset for local dev
PHOENIX_CLOUD_VARS = [
    "PHOENIX_HOST",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "PHOENIX_API_KEY",
]


def _load_env_keys(cfg: Config) -> None:
    """Load MODEL_API_KEYS from the main phoenix .env into os.environ."""
    env_file = cfg.sandbox_root.parent / "phoenix" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if key in MODEL_API_KEYS:
            os.environ[key] = value


@app.command(rich_help_panel="Development")
def dev(project: str = typer.Argument(autocompletion=complete_project)):
    """Launch local Phoenix dev server in tmux for a project sandbox."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if not proj.sandbox or not Path(proj.sandbox).exists():
        console.print(f"[red]No sandbox found. Run: arc sandbox {proj.slug}[/red]")
        raise typer.Exit(1)

    sandbox_path = Path(proj.sandbox)
    set_tab_title(f"arc: {project} (dev)")

    # Source model API keys from main phoenix .env
    _load_env_keys(cfg)

    # Unset cloud-targeting vars
    for var in PHOENIX_CLOUD_VARS:
        os.environ.pop(var, None)

    # Point to shared local DB
    os.environ["PHOENIX_WORKING_DIR"] = str(Path.home() / ".phoenix")

    # Pull latest from main and rebase
    console.print("[dim]Pulling latest from main...[/dim]")
    subprocess.run(["git", "fetch", "origin", "main"], cwd=sandbox_path, capture_output=True)
    result = subprocess.run(
        ["git", "rebase", "origin/main"],
        cwd=sandbox_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print("[yellow]Rebase onto main failed — resolve conflicts manually[/yellow]")
        console.print(f"[dim]{result.stderr.strip()}[/dim]")
        raise typer.Exit(1)

    # Run setup if needed (check for .venv as proxy)
    if not (sandbox_path / ".venv").exists():
        console.print("[bold]Running initial setup (make setup)...[/bold]")
        result = subprocess.run(["make", "setup"], cwd=sandbox_path)
        if result.returncode != 0:
            console.print("[red]Setup failed[/red]")
            raise typer.Exit(1)

    # Step 1: Stale session cleanup
    if proj.dev_session and not tmux_session_exists(proj.dev_session):
        proj.dev_port = 0
        proj.dev_session = ""
        update_project_note(proj)

    # Step 2: Discover active servers
    all_projs = load_projects(cfg)
    active_servers = {
        p.dev_session: p.dev_port
        for p in all_projs
        if p.dev_port and p.dev_session and tmux_session_exists(p.dev_session)
    }

    # Step 3: Show status if servers are running
    if active_servers:
        console.print("[dim]Running Phoenix servers:[/dim]")
        for sess, port in active_servers.items():
            marker = " ← this project" if sess == f"arc-{proj.slug}-dev" else ""
            console.print(f"  [cyan]{sess}[/cyan]  http://localhost:{port}{marker}")

    # Step 4: Determine launch action
    if not active_servers:
        port = 6006
    else:
        next_port = next_free_port(6007)
        default_choice = "1" if f"arc-{proj.slug}-dev" in active_servers else "3"
        console.print("\n[bold]Phoenix launch options:[/bold]")
        console.print("  [1] Skip — don't launch Phoenix")
        console.print("  [2] Replace — kill all servers, launch on :6006")
        console.print(f"  [3] New port — launch alongside existing on :{next_port}")
        while True:
            choice = typer.prompt("Choice", default=default_choice)
            if choice in ("1", "2", "3"):
                break
            console.print("[yellow]Enter 1, 2, or 3[/yellow]")

        if choice == "1":
            console.print(f"[dim]Sandbox: {sandbox_path}[/dim]")
            console.print("[dim]DB: ~/.phoenix[/dim]")
            return

        if choice == "2":
            for sess, p_port in list(active_servers.items()):
                subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
                for p in all_projs:
                    if p.dev_session == sess:
                        p.dev_port = 0
                        p.dev_session = ""
                        update_project_note(p)
            port = 6006
            if not is_port_free(6006):
                console.print(
                    "[yellow]Warning: port 6006 not yet free, server may take a moment to bind[/yellow]"
                )
        else:
            port = next_port

    # Step 5: Launch in tmux background
    dev_script = Path(f"/tmp/arc-dev-{proj.slug}.sh")
    lines = ["#!/usr/bin/env bash"]
    for key in MODEL_API_KEYS:
        val = os.environ.get(key, "")
        if val:
            escaped = val.replace("'", "'\\''")
            lines.append(f"export {key}='{escaped}'")
    for var in PHOENIX_CLOUD_VARS:
        lines.append(f"unset {var}")
    lines.append(f'export PHOENIX_WORKING_DIR="{Path.home() / ".phoenix"}"')
    lines.append(f"export PHOENIX_PORT={port}")
    lines.append(f'exec make -C "{sandbox_path}" dev')
    dev_script.write_text("\n".join(lines) + "\n")
    dev_script.chmod(0o755)

    session_name = f"arc-{proj.slug}-dev"
    if tmux_session_exists(session_name):
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, str(dev_script)], check=True)

    proj.dev_port = port
    proj.dev_session = session_name
    update_project_note(proj)

    console.print(f"\n[bold green]Phoenix started for {proj.slug}[/bold green]")
    console.print(f"  [dim]UI:[/dim]      http://localhost:{port}")
    console.print(f"  [dim]Session:[/dim] {session_name}")
    console.print(f"  [dim]Attach:[/dim]  tmux attach -t {session_name}")
    console.print("  [dim]DB:[/dim]      ~/.phoenix")


@app.command(rich_help_panel="Project Management")
def new(title: str = typer.Argument(..., help="Project title")):
    """Create a new project note without a GitHub issue."""
    cfg = load_config()
    slug = slugify(title)
    console.print(f"Suggested slug: [bold]{slug}[/bold]")
    custom = typer.prompt("Enter to accept, or type a custom slug", default=slug)
    if custom and custom != slug:
        slug = custom

    existing = [p.slug for p in load_projects(cfg)]
    if slug in existing:
        console.print(f"[red]Project '{slug}' already exists[/red]")
        raise typer.Exit(1)

    proj = create_project_note(cfg, title, slug)
    console.print(f"[green]Created:[/green] {proj.path}")
    console.print(f"  Next: [bold]arc plan {slug}[/bold]")


def _kill_sessions(proj: "Project") -> None:
    """Kill all tmux sessions associated with a project."""
    # Dev server
    if proj.dev_session and tmux_session_exists(proj.dev_session):
        subprocess.run(["tmux", "kill-session", "-t", proj.dev_session], capture_output=True)
        console.print(f"[dim]Killed dev session: {proj.dev_session}[/dim]")
    # CI monitor
    ci_sess = f"arc-{proj.slug}-ci"
    if tmux_session_exists(ci_sess):
        subprocess.run(["tmux", "kill-session", "-t", ci_sess], capture_output=True)
        console.print(f"[dim]Killed CI session: {ci_sess}[/dim]")
    # Implement session
    impl_sess = f"arc-{proj.slug}"
    if tmux_session_exists(impl_sess):
        subprocess.run(["tmux", "kill-session", "-t", impl_sess], capture_output=True)
        console.print(f"[dim]Killed implement session: {impl_sess}[/dim]")


@app.command(rich_help_panel="Project Management")
def done(
    project: str = typer.Argument(autocompletion=complete_project),
    stage_id: int = typer.Argument(None),
    skip: bool = typer.Option(False, "--skip", help="Mark as skipped instead of done"),
):
    """Mark a stage (or whole project) as done and clean up sessions."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if proj.stages:
        if stage_id is None:
            # Find first non-done/skipped stage
            active = [s for s in proj.stages if s.status not in ("done", "skipped", "pending")]
            stage = active[0] if active else proj.next_available_stage()
            if stage is None:
                console.print("[yellow]No active stages to mark done.[/yellow]")
                raise typer.Exit(0)
        else:
            stage = proj.get_stage(stage_id)
            if stage is None:
                console.print(f"[red]Stage {stage_id} not found[/red]")
                raise typer.Exit(1)

        new_status = "skipped" if skip else "done"
        stage.status = new_status
        auto_promote_ready(proj)
        update_project_note(proj)
        console.print(f"[green]Stage {stage.id} ({stage.name}) marked {new_status}.[/green]")

        # Show newly ready stages
        newly_ready = [s for s in proj.stages if s.status == "ready"]
        if newly_ready and not skip:
            names = [f"{s.id} ({s.name})" for s in newly_ready]
            console.print(f"[green]Now ready: {', '.join(names)}[/green]")

        if all(s.status in ("done", "skipped") for s in proj.stages):
            _kill_sessions(proj)
            proj.status = "done"
            proj.dev_port = 0
            proj.dev_session = ""
            update_project_note(proj)
            console.print(f"[green bold]All stages complete — {proj.slug} done.[/green bold]")
    else:
        _kill_sessions(proj)
        proj.status = "done"
        proj.dev_port = 0
        proj.dev_session = ""
        update_project_note(proj)
        console.print(f"[green]{proj.slug} done.[/green]")


@app.command(rich_help_panel="Project Management")
def archive(project: str = typer.Argument(autocompletion=complete_project)):
    """Shelve a project — kill sessions but preserve sandbox."""
    cfg = load_config()
    proj = find_project(cfg, project)

    _kill_sessions(proj)

    proj.status = "archived"
    proj.dev_port = 0
    proj.dev_session = ""
    update_project_note(proj)
    console.print(f"[green]{proj.slug} archived.[/green]")


@app.command(rich_help_panel="Utilities")
def organize(
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Preview changes without modifying files"
    ),
    force: bool = typer.Option(False, "--force", help="Run even if already ran today"),
):
    """Run the vault organizer to tag and link notes."""
    script = Path(__file__).resolve().parent / "organize.py"
    if not script.exists():
        console.print("[red]organize.py not found[/red]")
        raise typer.Exit(1)
    cmd = [sys.executable, str(script)]
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd)
    raise typer.Exit(result.returncode)


@app.command(hidden=True)
def tui():
    """Launch the TUI workspace control panel in tmux."""
    session = "arc-workspace"

    # If session exists, just attach
    if tmux_session_exists(session):
        console.print(f"[dim]Attaching to existing {session}...[/dim]")
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])

    cfg = load_config()
    tui_script = str(Path(__file__).resolve().parent / "tui.py")

    # Create session with a plain shell in window 0, then send the TUI command.
    # This ensures the shell initializes with full PATH (uv, etc.) from .zshrc.
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-n", "tui"],
        check=True,
    )
    # Add "home" hint to the tmux status bar so it's visible from any window
    subprocess.run(
        ["tmux", "set-option", "-t", session, "status-right", " Ctrl-b 0 = home "],
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{session}:tui", f"uv run --script {tui_script}", "Enter"],
        capture_output=True,
    )

    # Create a window per active project (skip archived/needs-plan)
    for proj in load_projects(cfg):
        if proj.derived_status in ("archived", "needs-plan"):
            continue
        sandbox = proj.sandbox or str(cfg.sandbox_root / proj.slug)
        sandbox_path = Path(sandbox).expanduser()
        start_dir = str(sandbox_path) if sandbox_path.exists() else str(Path.home())
        subprocess.run(
            ["tmux", "new-window", "-t", session, "-n", proj.slug, "-c", start_dir],
            capture_output=True,
        )

    # Select window 0 (tui) and attach
    subprocess.run(["tmux", "select-window", "-t", f"{session}:tui"], capture_output=True)
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])


if __name__ == "__main__":
    app()
