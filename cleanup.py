#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "typer>=0.15",
#     "rich>=13",
#     "pyyaml>=6",
# ]
# ///
"""Weekly project cleanup — moves done/archived projects to vault archive folder."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tomllib
from datetime import date
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.toml"
LOG_DIR = SCRIPT_DIR / "logs"

console = Console()


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    vault = Path(raw["core"]["obsidian_vault"]).expanduser()
    cleanup = raw.get("cleanup", {})
    return {
        "vault": vault,
        "projects_folder": raw["core"]["projects_folder"],
        "sandbox_root": Path(raw["core"]["sandbox_root"]).expanduser(),
        "statuses": cleanup.get("statuses", ["done", "archived"]),
        "archived_folder": cleanup.get("archived_folder", "Archived"),
    }


def setup_logging(dry_run: bool) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    today = date.today().isoformat()
    log_file = LOG_DIR / f"cleanup-{today}.log"

    logger = logging.getLogger("cleanup")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

    ch = RichHandler(console=console, show_path=False, markup=True)
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    if dry_run:
        logger.info("[bold yellow]DRY RUN[/bold yellow] — no files will be moved")
    return logger


def parse_frontmatter(index: Path) -> dict:
    text = index.read_text()
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        fm = yaml.safe_load(parts[1])
        return fm if isinstance(fm, dict) else {}
    except yaml.YAMLError:
        return {}


def _git_output(sandbox: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=sandbox, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _remove_disposable_sandbox(
    *,
    status: str,
    slug: str,
    sandbox: Path,
    sandbox_root: Path,
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """Remove a clean, commit-free done sandbox; preserve every other state."""
    if status != "done" or not sandbox.exists():
        return False

    root = sandbox_root.resolve()
    resolved = sandbox.resolve()
    safe = (
        not sandbox.is_symlink()
        and resolved.parent == root
        and resolved.name == slug
        and (resolved / ".git").exists()
    )
    if not safe:
        logger.warning("Keeping sandbox outside expected location: %s", sandbox)
        return False

    porcelain = _git_output(resolved, "status", "--porcelain")
    if porcelain is None or porcelain:
        reason = "git status failed" if porcelain is None else "uncommitted changes"
        logger.info("Keeping sandbox %s (%s)", resolved, reason)
        return False

    base = next(
        (
            ref
            for ref in ("origin/main", "main")
            if _git_output(resolved, "rev-parse", "--verify", "--quiet", ref) is not None
        ),
        None,
    )
    if base is None:
        logger.info("Keeping sandbox %s (main branch unavailable)", resolved)
        return False

    ahead = _git_output(resolved, "rev-list", "--count", f"{base}..HEAD")
    if ahead is None or ahead != "0":
        reason = "commit check failed" if ahead is None else f"{ahead} commit(s) ahead of {base}"
        logger.info("Keeping sandbox %s (%s)", resolved, reason)
        return False

    if dry_run:
        logger.info("Would remove clean, commit-free sandbox: %s", resolved)
        return True

    shutil.rmtree(resolved)
    logger.info("Removed clean, commit-free sandbox: %s", resolved)
    return True


app = typer.Typer(add_completion=False)


@app.command()
def main(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview without moving files"),
) -> None:
    """Move done/archived project folders to the vault archive directory."""
    cfg = load_config()
    logger = setup_logging(dry_run)

    projects_dir = cfg["vault"] / cfg["projects_folder"]
    archive_dir = projects_dir / cfg["archived_folder"]
    statuses = cfg["statuses"]

    if not projects_dir.exists():
        logger.error("Projects directory not found: %s", projects_dir)
        raise typer.Exit(1)

    candidates: list[tuple[Path, dict]] = []
    for d in sorted(projects_dir.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name == cfg["archived_folder"]:
            continue
        index = d / "index.md"
        if not index.exists():
            continue
        fm = parse_frontmatter(index)
        status = fm.get("status")
        if status in statuses:
            candidates.append((d, fm))

    if not candidates:
        logger.info("No projects to archive (statuses checked: %s)", ", ".join(statuses))
        return

    table = Table(title="Projects to archive", show_header=True, header_style="bold")
    table.add_column("Slug")
    table.add_column("Status")
    table.add_column("Destination")
    for d, fm in candidates:
        status = str(fm.get("status", ""))
        table.add_row(d.name, status, str(archive_dir / d.name))
    console.print(table)

    for d, fm in candidates:
        status = str(fm.get("status", ""))
        # Archived projects intentionally retain their sandboxes for later work.
        if status != "done":
            continue
        sandbox_value = fm.get("sandbox")
        sandbox = (
            Path(str(sandbox_value)).expanduser()
            if sandbox_value
            else cfg["sandbox_root"] / str(fm.get("slug", d.name))
        )
        _remove_disposable_sandbox(
            status=status,
            slug=str(fm.get("slug", d.name)),
            sandbox=sandbox,
            sandbox_root=cfg["sandbox_root"],
            dry_run=dry_run,
            logger=logger,
        )

    if dry_run:
        logger.info("Dry run complete — %d project(s) would be archived", len(candidates))
        return

    if not archive_dir.exists():
        archive_dir.mkdir(parents=True)
        logger.info("Created archive directory: %s", archive_dir)

    moved, skipped = 0, 0
    for d, fm in candidates:
        status = str(fm.get("status", ""))
        dest = archive_dir / d.name
        if dest.exists():
            logger.warning("Skipping %s — destination already exists: %s", d.name, dest)
            skipped += 1
            continue
        shutil.move(str(d), str(dest))
        logger.info("Archived [bold]%s[/bold] (%s) → %s", d.name, status, dest)
        moved += 1

    logger.info("Done — %d archived, %d skipped", moved, skipped)


if __name__ == "__main__":
    app()
