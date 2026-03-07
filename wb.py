#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "typer>=0.15",
#     "rich>=13",
#     "pyyaml>=6",
# ]
# ///
"""wb — lightweight agent project scaffolding."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import textwrap
from urllib.parse import quote
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

app = typer.Typer(invoke_without_command=True, no_args_is_help=False)
console = Console()

STATUSES = [
    "needs-plan",
    "ready",
    "implementing",
    "reviewing",
    "awaiting-approval",
    "pr-open",
    "archived",
]

STATUS_COLORS = {
    "needs-plan": "dim",
    "ready": "green",
    "implementing": "yellow",
    "reviewing": "yellow",
    "awaiting-approval": "cyan",
    "pr-open": "magenta",
    "archived": "dim",
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
    status: str = "pending"  # pending | running | done | skipped
    plan: str = ""           # relative path within project folder
    depends_on: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "plan": self.plan,
            "depends_on": self.depends_on,
        }


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
    sandbox: str = ""
    branch: str = ""
    related_notes: list[str] = field(default_factory=list)
    plans: list[str] = field(default_factory=list)
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
        return STATUS_COLORS.get(self.derived_status, "white")

    @property
    def derived_status(self) -> str:
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
            "plans": self.plans,
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
        return f"{done}/{len(self.stages)} done"

    def next_available_stage(self) -> Stage | None:
        done_ids = {s.id for s in self.stages if s.status in ("done", "skipped")}
        for s in sorted(self.stages, key=lambda x: x.id):
            if s.status == "pending" and all(d in done_ids for d in s.depends_on):
                return s
        return None

    def unblocked_stages(self) -> list[Stage]:
        done_ids = {s.id for s in self.stages if s.status in ("done", "skipped")}
        return [
            s for s in self.stages
            if s.status == "pending" and all(d in done_ids for d in s.depends_on)
        ]

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
    if not stages:
        return "needs-plan"
    if all(s.status in ("done", "skipped") for s in stages):
        return "awaiting-approval"
    if "running" in {s.status for s in stages}:
        return "implementing"
    if all(s.status == "pending" for s in stages):
        return "ready"
    return "implementing"


def parse_project(path: Path) -> Project | None:
    text = path.read_text()
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
            stages.append(Stage(
                id=raw_stage.get("id", 0),
                name=raw_stage.get("name", ""),
                status=raw_stage.get("status", "pending"),
                plan=raw_stage.get("plan", ""),
                depends_on=raw_stage.get("depends_on", []),
            ))
    return Project(
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
        plans=fm.get("plans", []),
        stages=stages,
        dev_port=int(fm.get("dev_port", 0)),
        dev_session=fm.get("dev_session", ""),
        path=path,
    )


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
                    projects.append(p)
    return projects


def find_project(cfg: Config, slug: str) -> Project:
    for p in load_projects(cfg):
        if p.slug == slug:
            return p
    console.print(f"[red]Project not found: {slug}[/red]")
    raise typer.Exit(1)


def complete_project(incomplete: str) -> list[str]:
    try:
        cfg = load_config()
        slugs = [
            d.name for d in cfg.projects_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
            and (d / "index.md").exists()
            and d.name.startswith(incomplete)
        ]
        return sorted(slugs)
    except Exception:
        return []


def update_project_note(project: Project) -> None:
    if not project.path or not project.path.exists():
        return
    text = project.path.read_text()
    m = re.match(r"^---\n.+?\n---\n?", text, re.DOTALL)
    body = text[m.end():] if m else text
    project.updated = datetime.now().strftime("%Y-%m-%d")
    if project.stages:
        project.status = derive_project_status(project.stages)
    fm = yaml.dump(project.frontmatter_dict(), default_flow_style=False, sort_keys=False)
    project.path.write_text(f"---\n{fm}---\n{body}")


def create_project_note(cfg: Config, title: str, slug: str, body: str = "",
                        github_issue: str = "") -> Project:
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
    {body or '(to be filled during wb plan)'}

    ## Tasks
    (filled during wb plan)

    ## Spec
    (filled during wb plan)

    ## Notes
    ### {today}
    - Created{f' from issue {github_issue}' if github_issue else ''}
    """)
    note_path.write_text(f"---\n{fm}---\n{note_body}")
    return project


def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:60]


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
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return result.stdout.strip().splitlines()


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
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "{title}"',
    ], capture_output=True)
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
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def dashboard(ctx: typer.Context):
    """wb — project dashboard and scaffolding."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = load_config()
    projects = load_projects(cfg)
    if not projects:
        console.print("[dim]No projects found. Run [bold]wb sync[/bold] to pull issues.[/dim]")
        return

    active_sessions = tmux_sessions()

    table = Table(show_header=True, header_style="bold", padding=(0, 2))
    table.add_column("Project", style="bold")
    table.add_column("Stage")
    table.add_column("Progress")
    table.add_column("Branch")
    table.add_column("Tmux")
    table.add_column("Dev")
    table.add_column("Updated", justify="right")

    status_order = {s: i for i, s in enumerate(STATUSES)}
    projects.sort(key=lambda p: (
        0 if p.derived_status in ("implementing", "reviewing", "awaiting-approval") else 1,
        status_order.get(p.derived_status, 99),
    ))

    for p in projects:
        branch = p.branch
        if branch and len(branch) > 28:
            branch = branch[:25] + "..."

        tmux_info = ""
        if p.derived_status in ("implementing", "reviewing"):
            sess_name = f"wb-{p.slug}"
            if sess_name in active_sessions:
                tmux_info = f"[green]{sess_name}[/green]"
            review_sess = f"wb-{p.slug}-review"
            if review_sess in active_sessions:
                tmux_info = f"[green]{review_sess}[/green]"

        dev_info = ""
        if p.dev_port and p.dev_session and p.dev_session in active_sessions:
            dev_info = f"[cyan]:{p.dev_port}[/cyan]"

        url = p.obsidian_url(cfg)
        status = p.derived_status
        color = STATUS_COLORS.get(status, "white")
        table.add_row(
            f"[link={url}]{p.slug}[/link]",
            f"[{color}]{status}[/{color}]",
            p.stage_progress(),
            branch or "[dim]—[/dim]",
            tmux_info or "[dim]—[/dim]",
            dev_info or "[dim]—[/dim]",
            relative_time(p.updated),
        )

    console.print(table)


@app.command()
def sync():
    """Pull GitHub issues, create/attach project notes."""
    cfg = load_config()
    projects = load_projects(cfg)
    existing_issues: set[str] = set()
    for p in projects:
        for iss in p.github_issues:
            existing_issues.add(iss)

    console.print("[bold]Fetching assigned issues...[/bold]")
    result = subprocess.run(
        ["gh", "issue", "list", "--assignee", cfg.github_user,
         "--repo", cfg.github_repo,
         "--json", "number,title,body,labels", "--limit", "50"],
        capture_output=True, text=True,
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
        matching = [p for p in projects if any(
            word in p.title.lower() for word in issue["title"].lower().split()
            if len(word) > 3
        )]
        for p in matching:
            options.append((f"attach to \"{p.slug}\" ({p.status})", f"attach:{p.slug}"))
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
            proj = create_project_note(cfg, issue["title"], slug,
                                       body=body, github_issue=issue_ref)
            projects.append(proj)
            existing_issues.add(issue_ref)
            console.print(f"  [green]Created project: {slug}[/green]")

    console.print("\n[bold]Checking open PRs...[/bold]")
    result = subprocess.run(
        ["gh", "pr", "list", "--author", cfg.github_user,
         "--repo", cfg.github_repo,
         "--json", "number,title,url,headRefName", "--limit", "50"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        prs = json.loads(result.stdout)
        for pr in prs:
            for p in projects:
                if p.branch and p.branch == pr["headRefName"]:
                    pr_ref = pr["url"]
                    if pr_ref not in p.github_prs:
                        p.github_prs.append(pr_ref)
                        if p.status not in ("pr-open", "archived"):
                            p.status = "pr-open"
                        update_project_note(p)
                        console.print(f"  Updated {p.slug} with PR #{pr['number']}")

    bare = cfg.bare_repo
    if bare.exists():
        console.print("\n[bold]Updating reference repo...[/bold]")
        subprocess.run(["git", "fetch", "--all"], cwd=bare, capture_output=True)
        console.print("  [green]Done[/green]")

    console.print("\n[green]Sync complete.[/green]")


@app.command()
def plan(project: str = typer.Argument(autocompletion=complete_project)):
    """Launch interactive Claude session to plan a project."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if proj.status not in ("needs-plan", "ready"):
        console.print(f"[yellow]Warning: project status is '{proj.status}', not 'needs-plan'[/yellow]")
        if not typer.confirm("Continue anyway?"):
            raise typer.Exit(0)

    note_content = proj.path.read_text() if proj.path else ""

    # For folder-per-project, plans go in the project folder
    if proj.is_folder:
        plans_dir = proj.folder
        today = datetime.now().strftime("%Y-%m-%d")
        plan_name = f"plan-{today}"
        plan_path = plans_dir / f"{plan_name}.md"
        stage_instructions = textwrap.dedent("""\

        If this project has multiple stages/phases, define them in the project
        frontmatter and create a separate plan file for each stage:

        In index.md frontmatter, add:
        stages:
          - id: 1
            name: Stage name
            status: pending
            plan: stage-1-name.md
            depends_on: []
          - id: 2
            name: Another stage
            status: pending
            plan: stage-2-name.md
            depends_on: [1]

        Write each stage plan as a file in the project folder.
        """)
    else:
        plans_dir = cfg.projects_dir / "plans"
        today = datetime.now().strftime("%Y-%m-%d")
        plan_name = f"{proj.slug}-plan-{today}"
        plan_path = plans_dir / f"{plan_name}.md"
        stage_instructions = ""

    system_prompt = textwrap.dedent(f"""\
        You are planning the project: {proj.title}

        Your job is to:
        1. Understand the problem described in the project note
        2. Search the codebase and Obsidian vault for relevant context
        3. Discuss the approach with the user
        4. Write a detailed implementation plan with tasks
        {stage_instructions}
        When done:

        1. Write the detailed plan as a separate note at:
           {plan_path}
           This is an Obsidian vault, so the plan will be viewable and clickable.
           Include the full plan with context, architecture decisions, and rationale.

        2. Update the project note at:
           {proj.path}
           - Fill in the Objective, Tasks (as checkboxes), and Spec sections
           - Add the Obsidian wikilink "[[{plan_name}]]" to the `plans` list in frontmatter
           - Add any relevant Obsidian note paths to the `related_notes` frontmatter field
           - Change the status in frontmatter to 'ready'

        Create the plans directory if it doesn't exist: {plans_dir}

        Phoenix repo (if sandbox exists): {proj.sandbox or cfg.sandbox_root}
        Obsidian vault: {cfg.obsidian_vault}
    """)

    initial_msg = f"Read the project note at {proj.path} and let's plan this project."

    console.print(f"[bold]Launching planning session for: {proj.title}[/bold]")
    console.print("[dim]Chat with Claude to refine the plan. Exit when done.[/dim]\n")

    os.execvp("claude", [
        "claude", "--dangerously-skip-permissions",
        "--permission-mode", "plan",
        "--system-prompt", system_prompt,
        initial_msg,
    ])


@app.command("stage")
def stage_cmd(
    project: str = typer.Argument(autocompletion=complete_project),
    add: str = typer.Option(None, "--add", help="Add a new stage with this name"),
    depends_on: str = typer.Option(None, "--depends-on", help="Comma-separated stage IDs this depends on"),
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
            plan=f"stage-{next_id}-{slugify(add)}.md",
            depends_on=deps,
        )
        proj.stages.append(new_stage)
        update_project_note(proj)
        console.print(f"[green]Added stage {next_id}: {add}[/green]")
        if deps:
            console.print(f"  Depends on: {', '.join(str(d) for d in deps)}")
        console.print(f"  Plan file: {new_stage.plan}")
        return

    if plan_stage is not None:
        s = proj.get_stage(plan_stage)
        if not s:
            console.print(f"[red]Stage {plan_stage} not found[/red]")
            raise typer.Exit(1)
        if not proj.is_folder:
            console.print("[red]Stage planning requires folder-per-project format. Run: wb migrate[/red]")
            raise typer.Exit(1)

        plan_path = proj.folder / s.plan
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

            Project note: {proj.path}
            Phoenix repo (if sandbox exists): {proj.sandbox or cfg.sandbox_root}
            Obsidian vault: {cfg.obsidian_vault}
        """)

        initial_msg = f"Read the project note at {proj.path} and let's plan stage {s.id}: {s.name}"

        console.print(f"[bold]Planning stage {s.id}: {s.name}[/bold]")
        os.execvp("claude", [
            "claude", "--dangerously-skip-permissions",
            "--permission-mode", "plan",
            "--system-prompt", system_prompt,
            initial_msg,
        ])
        return

    # Default: list stages
    if not proj.stages:
        console.print(f"[dim]No stages defined for {proj.slug}.[/dim]")
        console.print(f"Add with: [bold]wb stage {proj.slug} --add \"Stage name\"[/bold]")
        return

    done_ids = {s.id for s in proj.stages if s.status in ("done", "skipped")}

    table = Table(show_header=True, header_style="bold", padding=(0, 2))
    table.add_column("#", justify="right")
    table.add_column("Stage")
    table.add_column("Status")
    table.add_column("Depends")

    for s in sorted(proj.stages, key=lambda x: x.id):
        unmet = [d for d in s.depends_on if d not in done_ids]
        blocked = len(unmet) > 0 and s.status == "pending"

        if blocked:
            status_str = "[red]blocked[/red]"
        elif s.status == "done":
            status_str = "[green]done[/green]"
        elif s.status == "running":
            status_str = "[yellow]running[/yellow]"
        elif s.status == "skipped":
            status_str = "[dim]skipped[/dim]"
        else:
            status_str = "pending"

        deps_str = ", ".join(str(d) for d in s.depends_on) if s.depends_on else "—"
        table.add_row(str(s.id), s.name, status_str, deps_str)

    console.print(table)

    # Print blocking info
    for s in sorted(proj.stages, key=lambda x: x.id):
        unmet = [d for d in s.depends_on if d not in done_ids]
        if unmet and s.status == "pending":
            blocking_names = ", ".join(str(d) for d in unmet)
            console.print(f"  [dim]↳ {s.name} waiting on: {blocking_names}[/dim]")


@app.command()
def sandbox(project: str = typer.Argument(autocompletion=complete_project)):
    """Create an isolated Phoenix sandbox for a project."""
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
        console.print("[bold]Creating bare reference repo (first time, may take a minute)...[/bold]")
        subprocess.run(
            ["git", "clone", "--bare", f"https://github.com/{cfg.github_repo}.git", str(bare)],
            check=True,
        )
    else:
        console.print("[dim]Updating reference repo...[/dim]")
        subprocess.run(["git", "fetch", "--all"], cwd=bare, capture_output=True)

    console.print(f"[bold]Cloning to {sandbox_path}...[/bold]")
    subprocess.run(
        ["git", "clone", "--reference", str(bare), "--shared",
         f"https://github.com/{cfg.github_repo}.git", str(sandbox_path)],
        check=True,
    )

    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=sandbox_path, check=True,
    )

    proj.sandbox = str(sandbox_path)
    proj.branch = branch_name
    update_project_note(proj)

    console.print(f"\n[green]Sandbox ready:[/green] {sandbox_path}")
    console.print(f"[green]Branch:[/green] {branch_name}")


@app.command()
def implement(
    project: str = typer.Argument(autocompletion=complete_project),
    stage_id: int = typer.Argument(None),
    bg: bool = typer.Option(False, "--bg", help="Run autonomously in background (tmux)"),
):
    """Implement a project or stage. Interactive by default, --bg for autonomous."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if not proj.sandbox or not Path(proj.sandbox).exists():
        console.print(f"[red]No sandbox found. Run: wb sandbox {proj.slug}[/red]")
        raise typer.Exit(1)

    sandbox_path = Path(proj.sandbox)

    # --- Background mode: autonomous implement → review pipeline in tmux ---
    if bg:
        _implement_bg(cfg, proj, sandbox_path)
        return

    # --- Interactive mode (default) ---
    if proj.stages:
        _implement_interactive_staged(cfg, proj, sandbox_path, stage_id)
    else:
        _implement_interactive_simple(cfg, proj, sandbox_path)


CODEX_MODEL = "gpt-5.3-codex"


def _codex_review(cfg: Config, proj: Project, sandbox_path: Path,
                  stage_context: str = "") -> None:
    """Run code review with Codex (gpt-5.3-codex) after Claude implementation."""
    console.print()
    console.print("[bold cyan]Starting Codex code review...[/bold cyan]")
    console.print(f"[dim]Model: {CODEX_MODEL}[/dim]")

    proj.status = "reviewing"
    update_project_note(proj)

    # Gather project documentation
    note_content = proj.path.read_text() if proj.path and proj.path.exists() else ""

    # Gather all stage plans if staged project
    plans_context = ""
    if proj.stages and proj.folder:
        for s in proj.stages:
            if s.plan:
                plan_path = proj.folder / s.plan
                if plan_path.exists():
                    plans_context += f"\n### Stage {s.id}: {s.name}\n{plan_path.read_text()}\n"

    review_prompt = textwrap.dedent(f"""\
        You are reviewing an implementation for project: {proj.title}

        ## Project Documentation
        {note_content}

        {f"## Stage Context{chr(10)}{stage_context}" if stage_context else ""}

        {f"## Implementation Plans{plans_context}" if plans_context else ""}

        ## Review Instructions
        1. Run `git diff main` to see all changes
        2. Review for correctness against the project requirements and plans above
        3. Check for edge cases, code quality, and adherence to the plan
        4. Run tests: `{cfg.test_cmd}`
        5. Run lint: `{cfg.lint_cmd}`
        6. Fix any issues you find and commit fixes with clear messages
        7. When done, write `.wb-review.md` summarizing your findings and any fixes made
    """)

    subprocess.run(
        ["codex", "exec",
         "--dangerously-bypass-approvals-and-sandbox",
         "-m", CODEX_MODEL,
         "-C", str(sandbox_path),
         review_prompt],
    )

    console.print("[green]Codex review complete.[/green]")

    proj.status = "awaiting-approval"
    update_project_note(proj)
    notify("wb", f"Implementation + Codex review complete for {proj.slug}")


def _implement_interactive_staged(cfg: Config, proj: Project, sandbox_path: Path,
                                  stage_id: int | None) -> None:
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
    done_ids = {s.id for s in proj.stages if s.status in ("done", "skipped")}
    unmet = [d for d in stage.depends_on if d not in done_ids]
    if unmet:
        blocking = [f"{b.id} ({b.name})" for b in proj.stages if b.id in unmet]
        console.print(f"[red]Stage {stage_id} blocked by: {', '.join(blocking)}[/red]")
        raise typer.Exit(1)

    if stage.status == "done":
        if not typer.confirm(f"Stage {stage_id} is done. Re-run?", default=False):
            raise typer.Exit(0)
        stage.status = "pending"

    if stage.status == "running":
        if not typer.confirm(f"Stage {stage_id} is marked running. Re-run?", default=True):
            raise typer.Exit(0)

    # Update stage status
    stage.status = "running"
    update_project_note(proj)

    # Build system prompt from stage plan
    plan_content = ""
    if stage.plan and proj.folder:
        plan_path = proj.folder / stage.plan
        if plan_path.exists():
            plan_content = plan_path.read_text()

    system_prompt = textwrap.dedent(f"""\
        You are working on stage {stage.id} of project: {proj.title}

        Stage: {stage.name}

        {plan_content if plan_content else "No plan file found. Ask the user what to do."}

        Project sandbox: {sandbox_path}
        Branch: {proj.branch}

        When you've completed all steps, tell the user.
    """)

    console.print(f"[bold]Implementing stage {stage.id}: {stage.name}[/bold]")
    console.print(f"[dim]Sandbox: {sandbox_path}[/dim]\n")

    initial_msg = f"Let's work on stage {stage.id}: {stage.name}"
    subprocess.run(
        ["claude", "--dangerously-skip-permissions",
         "--system-prompt", system_prompt,
         initial_msg],
        cwd=sandbox_path,
    )

    # Codex review with stage context
    stage_ctx = f"Reviewing stage {stage.id}: {stage.name}"
    if plan_content:
        stage_ctx += f"\n\n{plan_content}"
    _codex_review(cfg, proj, sandbox_path, stage_context=stage_ctx)

    # Post-exit prompt
    console.print()
    choice = input(f"Mark '{stage.name}' as done? [Y/n/skip] ").strip().lower()

    if choice in ("", "y", "yes"):
        stage.status = "done"
        update_project_note(proj)
        console.print(f"[green]Stage {stage.id} marked done.[/green]")

        done_ids = {s.id for s in proj.stages if s.status in ("done", "skipped")}
        newly_unblocked = [
            s for s in proj.stages
            if s.status == "pending" and all(d in done_ids for d in s.depends_on)
        ]
        if newly_unblocked:
            names = [f"{s.id} ({s.name})" for s in newly_unblocked]
            console.print(f"[green]Now unblocked: {', '.join(names)}[/green]")
            notify("wb", f"{stage.name} done. Unblocked: {', '.join(s.name for s in newly_unblocked)}")
        else:
            console.print("[dim]No new stages unblocked.[/dim]")

        if all(s.status in ("done", "skipped") for s in proj.stages):
            proj.status = "awaiting-approval"
            update_project_note(proj)
            console.print(f"[green bold]All stages complete! Run: wb approve {proj.slug}[/green bold]")
            notify("wb", f"All stages complete for {proj.slug}")

    elif choice == "skip":
        stage.status = "skipped"
        update_project_note(proj)
        console.print(f"[yellow]Stage {stage.id} skipped.[/yellow]")
    else:
        console.print(f"[dim]Stage {stage.id} still running. Resume with: wb implement {proj.slug} {stage.id}[/dim]")


def _implement_interactive_simple(cfg: Config, proj: Project, sandbox_path: Path) -> None:
    """Interactive implementation for non-staged projects."""
    note_content = proj.path.read_text() if proj.path else ""

    system_prompt = textwrap.dedent(f"""\
        You are working on project: {proj.title}

        {note_content}

        Project sandbox: {sandbox_path}
        Branch: {proj.branch}

        When you've completed all tasks, tell the user.
    """)

    proj.status = "implementing"
    update_project_note(proj)

    console.print(f"[bold]Implementing: {proj.title}[/bold]")
    console.print(f"[dim]Sandbox: {sandbox_path}[/dim]\n")

    subprocess.run(
        ["claude", "--dangerously-skip-permissions",
         "--system-prompt", system_prompt,
         f"Let's implement {proj.title}."],
        cwd=sandbox_path,
    )

    # Codex review
    _codex_review(cfg, proj, sandbox_path)

    console.print()
    choice = input("Ready to approve? [Y/n] ").strip().lower()
    if choice in ("", "y", "yes"):
        console.print(f"[green]Ready for approval. Run: wb approve {proj.slug}[/green]")
    else:
        proj.status = "implementing"
        update_project_note(proj)
        console.print(f"[dim]Project still implementing. Re-run: wb implement {proj.slug}[/dim]")


def _implement_bg(cfg: Config, proj: Project, sandbox_path: Path) -> None:
    """Autonomous implement → review pipeline in tmux background."""
    note_content = proj.path.read_text() if proj.path else ""

    claude_md = textwrap.dedent(f"""\
        # Project: {proj.title}

        You are implementing this project autonomously. Follow the plan below.

        ## Instructions
        1. Read this file carefully
        2. Implement all tasks listed below
        3. Run tests: `{cfg.test_cmd}`
        4. Run lint: `{cfg.lint_cmd}`
        5. Fix any failures
        6. Commit your work with clear commit messages
        7. When fully done, write `.wb-summary.md` with a summary of what you did

        ## Project Note
        {note_content}
    """)
    (sandbox_path / "CLAUDE.md").write_text(claude_md)

    # Build Codex review prompt with full project context
    plans_context = ""
    if proj.stages and proj.folder:
        for s in proj.stages:
            if s.plan:
                plan_path = proj.folder / s.plan
                if plan_path.exists():
                    plans_context += f"\n### Stage {s.id}: {s.name}\n{plan_path.read_text()}\n"

    codex_review_prompt = textwrap.dedent(f"""\
        You are reviewing an implementation for project: {proj.title}

        ## Project Documentation
        {note_content}

        {f"## Implementation Plans{plans_context}" if plans_context else ""}

        ## Review Instructions
        1. Run `git diff main` to see all changes
        2. Review for correctness against the project requirements and plans above
        3. Check for edge cases, code quality, and adherence to the plan
        4. Run tests: `{cfg.test_cmd}`
        5. Run lint: `{cfg.lint_cmd}`
        6. Fix any issues you find and commit fixes with clear messages
        7. Write `.wb-review.md` summarizing your findings and any fixes made
    """)
    codex_review_prompt_escaped = codex_review_prompt.replace("'", "'\\''")

    project_note_path = str(proj.path) if proj.path else ""

    orchestrator = textwrap.dedent(f"""\
        #!/bin/bash
        set -e
        cd "{sandbox_path}"

        echo "=== wb: Starting implementation (Claude) for {proj.slug} ==="

        python3 -c "
import re, yaml
from pathlib import Path
p = Path('{project_note_path}')
text = p.read_text()
text = re.sub(r'status: \\w[\\w-]*', 'status: implementing', text, count=1)
p.write_text(text)
"

        claude --dangerously-skip-permissions -p "Read CLAUDE.md. Implement all tasks. Run tests. Commit your work. Write .wb-summary.md when done."

        echo "=== wb: Implementation done, starting Codex review ({CODEX_MODEL}) ==="

        python3 -c "
import re
from pathlib import Path
p = Path('{project_note_path}')
text = p.read_text()
text = re.sub(r'status: \\w[\\w-]*', 'status: reviewing', text, count=1)
p.write_text(text)
"

        codex exec --dangerously-bypass-approvals-and-sandbox -m {CODEX_MODEL} '{codex_review_prompt_escaped}'

        echo "=== wb: Codex review complete ==="

        python3 -c "
import re
from pathlib import Path
p = Path('{project_note_path}')
text = p.read_text()
text = re.sub(r'status: \\w[\\w-]*', 'status: awaiting-approval', text, count=1)
p.write_text(text)
"

        osascript -e 'display notification "Claude implementation + Codex review complete. Run: wb approve {proj.slug}" with title "wb: {proj.slug}"'
        echo -e "\\a"
        echo "=== wb: {proj.slug} is awaiting approval. Run: wb approve {proj.slug} ==="
    """)

    script_path = sandbox_path / ".wb-orchestrate.sh"
    script_path.write_text(orchestrator)
    script_path.chmod(0o755)

    sess_name = f"wb-{proj.slug}"
    if tmux_session_exists(sess_name):
        console.print(f"[yellow]tmux session '{sess_name}' already exists.[/yellow]")
        console.print(f"Attach with: [bold]tmux attach -t {sess_name}[/bold]")
        raise typer.Exit(1)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", sess_name,
         f"bash {script_path}"],
        check=True,
    )

    proj.status = "implementing"
    update_project_note(proj)

    console.print(f"[green]Pipeline launched in tmux session: {sess_name}[/green]")
    console.print(f"  Attach: [bold]tmux attach -t {sess_name}[/bold]")
    console.print(f"  When done, run: [bold]wb approve {proj.slug}[/bold]")


@app.command()
def approve(project: str = typer.Argument(autocompletion=complete_project)):
    """Create PR and launch CI monitor after review."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if proj.status != "awaiting-approval":
        if proj.status == "pr-open":
            console.print(f"[yellow]PR already open for {proj.slug}[/yellow]")
            if proj.github_prs:
                console.print(f"  {proj.github_prs[-1]}")
            raise typer.Exit(0)
        console.print(f"[red]Project status is '{proj.status}', expected 'awaiting-approval'[/red]")
        raise typer.Exit(1)

    sandbox_path = Path(proj.sandbox)
    if not sandbox_path.exists():
        console.print("[red]Sandbox not found[/red]")
        raise typer.Exit(1)

    summary = ""
    summary_path = sandbox_path / ".wb-summary.md"
    review_path = sandbox_path / ".wb-review.md"
    if summary_path.exists():
        summary += summary_path.read_text()
    if review_path.exists():
        summary += "\n\n---\n\n## Review Notes\n\n" + review_path.read_text()

    if not summary.strip():
        summary = f"Implementation of: {proj.title}"

    console.print(f"[bold]Pushing branch {proj.branch}...[/bold]")
    subprocess.run(
        ["git", "push", "-u", "origin", proj.branch],
        cwd=sandbox_path, check=True,
    )

    console.print("[bold]Creating PR...[/bold]")
    pr_title = proj.title
    if len(pr_title) > 70:
        pr_title = pr_title[:67] + "..."

    issue_refs = ""
    if proj.github_issues:
        issue_refs = "\n\nCloses " + ", ".join(
            f"#{ref.split('#')[-1]}" if "#" in ref else ref
            for ref in proj.github_issues
        )

    pr_body = f"{summary}{issue_refs}"

    result = subprocess.run(
        ["gh", "pr", "create",
         "--repo", cfg.github_repo,
         "--title", pr_title,
         "--body", pr_body],
        cwd=sandbox_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]PR creation failed: {result.stderr}[/red]")
        raise typer.Exit(1)

    pr_url = result.stdout.strip()
    console.print(f"[green]PR created: {pr_url}[/green]")

    proj.status = "pr-open"
    proj.github_prs.append(pr_url)
    update_project_note(proj)

    sess_name = f"wb-{proj.slug}-ci"
    project_note_path = str(proj.path) if proj.path else ""

    ci_script = textwrap.dedent(f"""\
        #!/bin/bash
        cd "{sandbox_path}"

        echo "=== wb: Monitoring CI for {proj.slug} ==="

        while true; do
            sleep 60

            state=$(gh pr view --repo {cfg.github_repo} --json state -q '.state' 2>/dev/null || echo "unknown")

            if [ "$state" = "MERGED" ]; then
                echo "=== wb: PR merged! ==="
                tmux kill-session -t "wb-{proj.slug}-dev" 2>/dev/null || true
                python3 -c "
import re
from pathlib import Path
p = Path('{project_note_path}')
text = p.read_text()
text = re.sub(r'status: \\w[\\w-]*', 'status: archived', text, count=1)
text = re.sub(r'\\ndev_port:.*', '', text)
text = re.sub(r'\\ndev_session:.*', '', text)
p.write_text(text)
"
                osascript -e 'display notification "PR merged!" with title "wb: {proj.slug}"'
                break
            fi

            checks=$(gh pr checks --repo {cfg.github_repo} 2>/dev/null || echo "pending")

            if echo "$checks" | grep -q "fail"; then
                echo "=== wb: CI failure detected, launching fix agent ==="
                claude --dangerously-skip-permissions -p "CI is failing on this PR. Run gh pr checks to see failures. Read the failing logs. Fix the issues. Run tests locally. Commit and push."
            fi

            if echo "$checks" | grep -q "pass"; then
                echo "=== wb: CI passing ==="
                osascript -e 'display notification "CI is passing" with title "wb: {proj.slug}"'
            fi
        done
    """)

    ci_script_path = sandbox_path / ".wb-ci-monitor.sh"
    ci_script_path.write_text(ci_script)
    ci_script_path.chmod(0o755)

    if not tmux_session_exists(sess_name):
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", sess_name,
             f"bash {ci_script_path}"],
            check=True,
        )
        console.print(f"[green]CI monitor launched: {sess_name}[/green]")

    console.print(f"\n[bold]Done![/bold] PR: {pr_url}")


@app.command("open")
def open_cmd(project: str = typer.Argument(autocompletion=complete_project)):
    """Open a project's Obsidian note."""
    cfg = load_config()
    proj = find_project(cfg, project)
    url = proj.obsidian_url(cfg)
    subprocess.run(["open", url])
    console.print(f"[green]Opened {proj.slug} in Obsidian[/green]")


@app.command()
def chat(project: str = typer.Argument(autocompletion=complete_project)):
    """Launch an informal Claude chat with project context."""
    cfg = load_config()
    proj = find_project(cfg, project)

    note_path = proj.path
    sandbox_info = f"Sandbox path: {proj.sandbox}" if proj.sandbox else "No sandbox configured."

    system_prompt = textwrap.dedent(f"""\
        Project: {proj.title}
        Project note: {note_path}
        {sandbox_info}
        Obsidian vault: {cfg.obsidian_vault}

        You have context about this project. The user wants to chat informally — answer questions, brainstorm, help think through problems. If they reference code, you can read files in the sandbox path.
    """)

    initial_msg = f"Read the project note at {note_path}"

    console.print(f"[bold]Chatting about: {proj.title}[/bold]")
    console.print("[dim]Informal Claude session with project context.[/dim]\n")

    os.execvp("claude", [
        "claude", "--dangerously-skip-permissions",
        "--system-prompt", system_prompt,
        initial_msg,
    ])


@app.command()
def cursor(project: str = typer.Argument(autocompletion=complete_project)):
    """Open project sandbox in Cursor with changed files."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if not proj.sandbox or not Path(proj.sandbox).exists():
        console.print(f"[red]No sandbox found. Run: wb sandbox {proj.slug}[/red]")
        raise typer.Exit(1)

    sandbox_path = Path(proj.sandbox)

    # Get changed files relative to main
    result = subprocess.run(
        ["git", "diff", "--name-only", "main"],
        cwd=sandbox_path, capture_output=True, text=True,
    )
    changed_files = []
    if result.returncode == 0 and result.stdout.strip():
        changed_files = result.stdout.strip().splitlines()

    cmd = ["cursor", str(sandbox_path)] + changed_files
    console.print(f"[green]Opening {proj.slug} in Cursor[/green]")
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


@app.command()
def dev(project: str = typer.Argument(autocompletion=complete_project)):
    """Launch local Phoenix dev environment for a project sandbox."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if not proj.sandbox or not Path(proj.sandbox).exists():
        console.print(f"[red]No sandbox found. Run: wb sandbox {proj.slug}[/red]")
        raise typer.Exit(1)

    sandbox_path = Path(proj.sandbox)

    # Source model API keys from main phoenix .env
    env_file = cfg.sandbox_root.parent / "phoenix" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip optional "export " prefix
            if line.startswith("export "):
                line = line[7:]
            key, _, value = line.partition("=")
            if key in MODEL_API_KEYS:
                os.environ[key] = value

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
        cwd=sandbox_path, capture_output=True, text=True,
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
            marker = " ← this project" if sess == f"wb-{proj.slug}-dev" else ""
            console.print(f"  [cyan]{sess}[/cyan]  http://localhost:{port}{marker}")

    # Step 4: Determine launch action
    if not active_servers:
        port = 6006
    else:
        next_port = next_free_port(6007)
        default_choice = "1" if f"wb-{proj.slug}-dev" in active_servers else "3"
        console.print(f"\n[bold]Phoenix launch options:[/bold]")
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
            console.print(f"[dim]DB: ~/.phoenix[/dim]")
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
                console.print("[yellow]Warning: port 6006 not yet free, server may take a moment to bind[/yellow]")
        else:
            port = next_port

    # Step 5: Launch in tmux background
    dev_script = Path(f"/tmp/wb-dev-{proj.slug}.sh")
    lines = ["#!/usr/bin/env bash"]
    for key in MODEL_API_KEYS:
        val = os.environ.get(key, "")
        if val:
            lines.append(f'export {key}="{val}"')
    for var in PHOENIX_CLOUD_VARS:
        lines.append(f"unset {var}")
    lines.append(f'export PHOENIX_WORKING_DIR="{Path.home() / ".phoenix"}"')
    lines.append(f'export PHOENIX_PORT={port}')
    lines.append(f'exec make -C "{sandbox_path}" dev')
    dev_script.write_text("\n".join(lines) + "\n")
    dev_script.chmod(0o755)

    session_name = f"wb-{proj.slug}-dev"
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
    console.print(f"  [dim]DB:[/dim]      ~/.phoenix")


@app.command()
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
    console.print(f"  Next: [bold]wb plan {slug}[/bold]")


@app.command()
def done(
    project: str = typer.Argument(autocompletion=complete_project),
    stage_id: int = typer.Argument(None),
    skip: bool = typer.Option(False, "--skip", help="Mark as skipped instead of done"),
):
    """Mark a stage (or whole project) as done without launching Claude."""
    cfg = load_config()
    proj = find_project(cfg, project)

    if proj.stages:
        if stage_id is None:
            # Default to the first running or next available stage
            stage = next((s for s in proj.stages if s.status == "running"), None)
            if stage is None:
                stage = proj.next_available_stage()
            if stage is None:
                console.print("[yellow]No pending/running stages.[/yellow]")
                raise typer.Exit(0)
        else:
            stage = proj.get_stage(stage_id)
            if stage is None:
                console.print(f"[red]Stage {stage_id} not found[/red]")
                raise typer.Exit(1)

        new_status = "skipped" if skip else "done"
        stage.status = new_status
        update_project_note(proj)
        console.print(f"[green]Stage {stage.id} ({stage.name}) marked {new_status}.[/green]")

        if not skip:
            done_ids = {s.id for s in proj.stages if s.status in ("done", "skipped")}
            newly_unblocked = [
                s for s in proj.stages
                if s.status == "pending" and all(d in done_ids for d in s.depends_on)
            ]
            if newly_unblocked:
                names = [f"{s.id} ({s.name})" for s in newly_unblocked]
                console.print(f"[green]Now unblocked: {', '.join(names)}[/green]")

        if all(s.status in ("done", "skipped") for s in proj.stages):
            proj.status = "awaiting-approval"
            update_project_note(proj)
            console.print(f"[green bold]All stages complete! Run: wb approve {proj.slug}[/green bold]")
    else:
        # No stages — mark the whole project
        new_status = "awaiting-approval" if not skip else "archived"
        proj.status = new_status
        update_project_note(proj)
        console.print(f"[green]{proj.slug} marked {new_status}.[/green]")


@app.command()
def archive(project: str = typer.Argument(autocompletion=complete_project)):
    """Archive a project and clean up its background sessions."""
    cfg = load_config()
    proj = find_project(cfg, project)

    # Kill dev server session if running
    if proj.dev_session and tmux_session_exists(proj.dev_session):
        subprocess.run(["tmux", "kill-session", "-t", proj.dev_session], capture_output=True)
        console.print(f"[dim]Killed dev session: {proj.dev_session}[/dim]")

    # Kill CI monitor if running
    ci_sess = f"wb-{proj.slug}-ci"
    if tmux_session_exists(ci_sess):
        subprocess.run(["tmux", "kill-session", "-t", ci_sess], capture_output=True)
        console.print(f"[dim]Killed CI session: {ci_sess}[/dim]")

    # Kill implement session if running
    impl_sess = f"wb-{proj.slug}"
    if tmux_session_exists(impl_sess):
        subprocess.run(["tmux", "kill-session", "-t", impl_sess], capture_output=True)
        console.print(f"[dim]Killed implement session: {impl_sess}[/dim]")

    proj.status = "archived"
    proj.dev_port = 0
    proj.dev_session = ""
    update_project_note(proj)
    console.print(f"[green]{proj.slug} archived.[/green]")


if __name__ == "__main__":
    app()
