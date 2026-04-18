#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40",
#     "pyyaml>=6",
#     "rich>=13",
# ]
# ///
"""Daily Obsidian vault organizer — tags notes and links them to projects."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path

import anthropic
import yaml
from rich.console import Console
from rich.logging import RichHandler

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.toml"
STATE_PATH = SCRIPT_DIR / ".organize-state.json"
LOG_DIR = SCRIPT_DIR / "logs"

console = Console()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    vault = Path(raw["core"]["obsidian_vault"]).expanduser()
    org = raw.get("organize", {})
    return {
        "vault": vault,
        "projects_folder": raw["core"]["projects_folder"],
        "skip_folders": org.get("skip_folders", ["Templates", ".obsidian", "Assets"]),
        "max_notes_per_run": org.get("max_notes_per_run", 20),
        "model": org.get("model", "claude-sonnet-4-20250514"),
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_run": None, "hashes": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(dry_run: bool) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    today = date.today().isoformat()
    log_file = LOG_DIR / f"organize-{today}.log"

    logger = logging.getLogger("organize")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

    ch = RichHandler(console=console, show_path=False, markup=True)
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    if dry_run:
        logger.info("[bold yellow]DRY RUN[/bold yellow] — no files will be modified")
    return logger


# ---------------------------------------------------------------------------
# API key from Keychain
# ---------------------------------------------------------------------------


def get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "vault-organize",
             "-s", "ANTHROPIC_API_KEY", "-w"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        console.print("[red]API key not found. Set ANTHROPIC_API_KEY env var or run:[/red]")
        console.print('  security add-generic-password -a "vault-organize" -s "ANTHROPIC_API_KEY" -w "sk-ant-..."')
        sys.exit(1)


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

FM_RE = re.compile(r"^---\n(.*?\n)---\n", re.DOTALL)


def split_note(text: str) -> tuple[dict | None, str]:
    """Split a note into (frontmatter_dict, body). Returns (None, text) if no frontmatter."""
    m = FM_RE.match(text)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1))
        if not isinstance(fm, dict):
            return None, text
        body = text[m.end():]
        return fm, body
    except yaml.YAMLError:
        return None, text


def reassemble_note(fm: dict, body: str) -> str:
    """Reassemble frontmatter + body into a note string."""
    fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{fm_str}---\n{body}"


def body_hash(body: str) -> str:
    """SHA-256 of body only (excludes frontmatter so our tag edits don't re-trigger)."""
    return hashlib.sha256(body.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Lifecycle classification (deterministic rules, mirrors arc migrate-lifecycle)
# ---------------------------------------------------------------------------


def classify_lifecycle(rel_path: str, fm: dict | None, projects_folder: str) -> tuple[str, str]:
    """Return (lifecycle, source_type) for a note. Honors explicit fm overrides."""
    if fm and fm.get("lifecycle") and fm.get("source_type"):
        return str(fm["lifecycle"]), str(fm["source_type"])

    parts = Path(rel_path).parts
    name = Path(rel_path).name
    top = parts[0] if parts else ""

    # Project tree
    if top == projects_folder:
        if name == "index.md":
            return "live", "project-meta"
        if name == "notes.md":
            return "log", "session-log"
        if name == "plan.md":
            return "live", "project-meta"
        return "evergreen", "authored"

    # Source URL → web clipping (regardless of folder)
    if fm:
        src = fm.get("source")
        if isinstance(src, str) and src.startswith(("http://", "https://")):
            return "reference", "web-clipping"

    # Folder defaults
    if top == "Clippings":
        return "reference", "web-clipping"
    if top == "Blogs":
        return "frozen", "authored"
    if top == "Research":
        return "evergreen", "authored"

    return "evergreen", "authored"


# ---------------------------------------------------------------------------
# Vault scanning
# ---------------------------------------------------------------------------


def scan_vault(cfg: dict, state: dict, logger: logging.Logger) -> list[dict]:
    """Find new/changed .md files. Returns list of {path, rel, fm, body, hash, lifecycle}."""
    vault = cfg["vault"]
    skip = set(cfg["skip_folders"])
    old_hashes = state.get("hashes", {})
    changed = []

    for md in vault.rglob("*.md"):
        # Skip folders
        rel = md.relative_to(vault)
        if any(part in skip for part in rel.parts):
            continue
        # Skip iCloud placeholders
        if md.name.startswith(".") and md.name.endswith(".icloud"):
            continue
        # Skip project index.md files (we append to them, not tag them)
        if rel.parts[0] == cfg["projects_folder"] and md.name == "index.md":
            continue

        text = md.read_text(errors="replace")
        fm, body = split_note(text)

        # Lifecycle filter: never touch logs or frozen notes
        lifecycle, source_type = classify_lifecycle(str(rel), fm, cfg["projects_folder"])
        if lifecycle in ("log", "frozen"):
            continue

        h = body_hash(body)
        rel_str = str(rel)

        if old_hashes.get(rel_str) != h:
            changed.append({
                "path": md, "rel": rel_str, "fm": fm, "body": body, "hash": h,
                "lifecycle": lifecycle, "source_type": source_type,
            })

    return changed


# ---------------------------------------------------------------------------
# Vault context for LLM
# ---------------------------------------------------------------------------


def build_vault_context(cfg: dict) -> dict:
    """Build lightweight context: tag vocabulary, project list, note index."""
    vault = cfg["vault"]
    skip = set(cfg["skip_folders"])
    projects_dir = vault / cfg["projects_folder"]

    # Collect all tags
    all_tags: set[str] = set()
    note_index: list[dict] = []

    for md in vault.rglob("*.md"):
        rel = md.relative_to(vault)
        if any(part in skip for part in rel.parts):
            continue
        if md.name.startswith("."):
            continue
        text = md.read_text(errors="replace")
        fm, _ = split_note(text)
        tags = fm.get("tags", []) if fm else []
        if isinstance(tags, list):
            all_tags.update(tags)
        title = fm.get("title", md.stem) if fm else md.stem
        note_index.append({"title": title, "path": str(rel), "tags": tags or []})

    # Collect active projects
    projects = []
    if projects_dir.exists():
        for idx in projects_dir.rglob("index.md"):
            text = idx.read_text(errors="replace")
            fm, _ = split_note(text)
            if fm and fm.get("status") != "archived":
                projects.append({
                    "slug": fm.get("slug", idx.parent.name),
                    "title": fm.get("title", ""),
                    "tags": fm.get("tags", []),
                    "related_notes": fm.get("related_notes", []),
                })

    return {
        "tag_vocabulary": sorted(all_tags),
        "projects": projects,
        "note_count": len(note_index),
    }


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a vault organizer for an Obsidian vault belonging to an AI engineer focused on LLM evaluation.

Given a note and the vault context (existing tags, active projects), return a JSON object with:
- "tags": list of tags to ADD (use existing vocabulary when possible; create new ones sparingly)
- "project_slugs": list of project slugs this note is relevant to (empty if none)
- "wikilinks": list of {"text": "exact phrase in note", "target": "Note Title"} for suggested [[wikilinks]] (max 3, only for strong connections to other notes)

Rules:
- Only suggest tags that genuinely apply. Prefer existing tags over new ones.
- A note is relevant to a project if it directly relates to the project's topic/goals.
- For wikilinks, the "text" must be an exact substring of the note body.
- Return ONLY valid JSON, no markdown fences."""


def classify_note(
    client: anthropic.Anthropic,
    model: str,
    note: dict,
    vault_ctx: dict,
    logger: logging.Logger,
) -> dict | None:
    existing_tags = note["fm"].get("tags", []) if note["fm"] else []

    user_msg = json.dumps({
        "note_path": note["rel"],
        "note_title": note["fm"].get("title", Path(note["rel"]).stem) if note["fm"] else Path(note["rel"]).stem,
        "existing_tags": existing_tags,
        "note_body": note["body"][:4000],
        "vault_tag_vocabulary": vault_ctx["tag_vocabulary"],
        "active_projects": vault_ctx["projects"],
    }, indent=2)

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        result = json.loads(text)
        logger.debug("LLM response for %s: %s", note["rel"], result)
        return result
    except Exception as e:
        logger.error("LLM call failed for %s: %s", note["rel"], e)
        return None


# ---------------------------------------------------------------------------
# Apply changes
# ---------------------------------------------------------------------------


def apply_tags(note_path: Path, fm: dict | None, body: str, new_tags: list[str], logger: logging.Logger) -> str | None:
    """Add tags to frontmatter. Returns new file content or None if no change."""
    if not new_tags:
        return None

    if fm is None:
        fm = {"tags": []}

    existing = set(fm.get("tags", []) or [])
    to_add = [t for t in new_tags if t not in existing]
    if not to_add:
        return None

    fm.setdefault("tags", [])
    fm["tags"].extend(to_add)
    logger.info("  +tags %s -> %s", note_path.name, to_add)
    return reassemble_note(fm, body)


def apply_wikilinks(body: str, links: list[dict], logger: logging.Logger) -> str:
    """Insert [[wikilinks]] at first occurrence of exact text match."""
    for link in links:
        text = link.get("text", "")
        target = link.get("target", "")
        if not text or not target:
            continue
        wikilink = f"[[{target}|{text}]]"
        # Only replace first occurrence, and only if not already a wikilink
        if text in body and wikilink not in body and f"[[{target}]]" not in body:
            body = body.replace(text, wikilink, 1)
            logger.info("  +link [[%s]]", target)
    return body


def append_related_note(
    project_index: Path, note_rel: str, logger: logging.Logger, dry_run: bool
) -> None:
    """Append a note to a project's related_notes frontmatter list."""
    text = project_index.read_text(errors="replace")
    fm, body = split_note(text)
    if fm is None:
        return

    related = fm.get("related_notes", []) or []
    if note_rel in related:
        return

    related.append(note_rel)
    fm["related_notes"] = related
    logger.info("  +related %s -> %s", note_rel, project_index.parent.name)

    if not dry_run:
        new_content = reassemble_note(fm, body)
        atomic_write(project_index, new_content)


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run(dry_run: bool = False) -> None:
    logger = setup_logging(dry_run)
    cfg = load_config()
    state = load_state()

    # Check if already ran today
    today = date.today().isoformat()
    if state.get("last_run") == today and "--force" not in sys.argv:
        logger.debug("Already ran today, exiting.")
        return

    logger.info("Scanning vault: %s", cfg["vault"])

    # Scan for changed notes
    changed = scan_vault(cfg, state, logger)
    if not changed:
        logger.info("No changed notes found.")
        state["last_run"] = today
        if not dry_run:
            save_state(state)
        return

    # Cap the number of notes per run
    max_notes = cfg["max_notes_per_run"]
    if len(changed) > max_notes:
        logger.info("Found %d changed notes, processing first %d", len(changed), max_notes)
        changed = changed[:max_notes]
    else:
        logger.info("Found %d changed note(s)", len(changed))

    # Build vault context
    vault_ctx = build_vault_context(cfg)
    logger.info("Vault context: %d tags, %d active projects", len(vault_ctx["tag_vocabulary"]), len(vault_ctx["projects"]))

    # Get API key and create client
    api_key = get_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    projects_dir = cfg["vault"] / cfg["projects_folder"]

    for note in changed:
        logger.info("Processing: %s (%s)", note["rel"], note["lifecycle"])
        result = classify_note(client, cfg["model"], note, vault_ctx, logger)
        if result is None:
            continue

        fm = note["fm"]
        body = note["body"]

        # Bake lifecycle + source_type into frontmatter if missing
        baked = False
        fm_to_write = dict(fm) if fm else {}
        if not fm_to_write.get("lifecycle"):
            fm_to_write["lifecycle"] = note["lifecycle"]
            baked = True
        if not fm_to_write.get("source_type"):
            fm_to_write["source_type"] = note["source_type"]
            baked = True
        new_content = reassemble_note(fm_to_write, body) if baked else None
        if baked:
            logger.info("  +lifecycle %s/%s", note["lifecycle"], note["source_type"])

        # Apply tags (operates on whatever we've built up so far)
        if new_content:
            fm_now, body_now = split_note(new_content)
        else:
            fm_now, body_now = fm, body
        tagged = apply_tags(note["path"], fm_now, body_now, result.get("tags", []), logger)
        if tagged:
            new_content = tagged

        # Apply wikilinks to body — only for non-reference notes (clippings stay pristine)
        wikilinks = result.get("wikilinks", [])
        if wikilinks and note["lifecycle"] != "reference":
            if new_content:
                fm_updated, body_updated = split_note(new_content)
                body_updated = apply_wikilinks(body_updated, wikilinks, logger)
                new_content = reassemble_note(fm_updated, body_updated)
            else:
                body_updated = apply_wikilinks(body, wikilinks, logger)
                if body_updated != body:
                    new_content = reassemble_note(fm or {}, body_updated)
        elif wikilinks and note["lifecycle"] == "reference":
            logger.debug("  skipping %d wikilink(s) — reference note", len(wikilinks))

        # Write note
        if new_content and not dry_run:
            atomic_write(note["path"], new_content)

        # Link to projects
        for slug in result.get("project_slugs", []):
            idx = projects_dir / slug / "index.md"
            if idx.exists():
                append_related_note(idx, note["rel"], logger, dry_run)

        # Update hash in state
        state.setdefault("hashes", {})[note["rel"]] = note["hash"]

    state["last_run"] = today
    if not dry_run:
        save_state(state)

    logger.info("Done.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    run(dry_run=dry)
