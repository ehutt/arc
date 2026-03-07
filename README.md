# wb

A personal CLI for managing AI-assisted engineering projects on the [Phoenix](https://github.com/Arize-ai/phoenix) repo. Each project lives as an Obsidian note; `wb` drives the full lifecycle from GitHub issue to merged PR using Claude as the implementation agent.

## Goals

- Keep all project state in Obsidian (human-readable, searchable, synced via iCloud)
- Automate the repetitive parts: sandbox setup, branch management, PR creation, CI monitoring
- Provide a consistent interface for launching Claude as a coding agent with the right context
- Support running multiple projects in parallel without interference

## Workflow

```
wb sync          # pull assigned GitHub issues, create project notes
wb plan <slug>   # interactive Claude session to write a plan
wb sandbox <slug>  # clone Phoenix into an isolated sandbox
wb implement <slug>         # interactive Claude coding session
wb implement <slug> --bg    # autonomous implement → review pipeline in tmux
wb approve <slug>  # push branch, open PR, start CI monitor
wb dev <slug>      # launch Phoenix dev server in background tmux
```

Status progresses automatically: `needs-plan` → `ready` → `implementing` → `reviewing` → `awaiting-approval` → `pr-open` → `archived`

## Commands

| Command | Description |
|---|---|
| `wb` | Dashboard: table of all projects with status, stage progress, branch, tmux |
| `wb sync` | Pull assigned GitHub issues; create or attach project notes |
| `wb plan <slug>` | Launch interactive Claude session to plan and write a plan note |
| `wb stage <slug>` | List, add, or plan stages for a project |
| `wb sandbox <slug>` | Create an isolated git clone with a feature branch |
| `wb implement <slug>` | Run Claude interactively on the next available stage |
| `wb implement <slug> --bg` | Autonomous implement → self-review pipeline in tmux |
| `wb approve <slug>` | Push branch, create PR via `gh`, launch CI monitor in tmux |
| `wb dev <slug>` | Launch Phoenix dev server in a background tmux session with port management |
| `wb open <slug>` | Open project note in Obsidian |
| `wb chat <slug>` | Informal Claude chat session with project context |
| `wb cursor <slug>` | Open sandbox in Cursor with changed files pre-loaded |

## Architecture

**Single file:** `wb.py` (~1500 lines) + `config.toml`. No database — all state is YAML frontmatter in Obsidian notes.

### Data model

**`Config`** (from `config.toml`): Obsidian vault path, sandbox root, GitHub repo, branch prefix, test/lint commands.

**`Project`** (parsed from `{vault}/Projects/{slug}/index.md` frontmatter):
- `slug`, `title`, `status`, `type`
- `sandbox` — absolute path to git clone
- `branch` — feature branch name
- `github_issues`, `github_prs` — linked GitHub refs
- `stages` — ordered list of `Stage` objects with dependency graph
- `dev_port`, `dev_session` — live Phoenix server state (set/cleared by `wb dev`)
- `plans`, `related_notes` — Obsidian wikilinks

**`Stage`**: `id`, `name`, `status` (pending/running/done/skipped), `plan` (path to plan file), `depends_on` (list of stage IDs). Status is derived from stages automatically.

### Storage

Project notes are Obsidian Markdown files with YAML frontmatter. `update_project_note()` rewrites the frontmatter in place, preserving the note body. Notes live at `{vault}/Projects/{slug}/index.md`.

### Sandboxes

`wb sandbox` clones Phoenix using a local bare reference repo (`{sandbox_root}/.phoenix-bare`) for fast clones via `--reference --shared`. Each sandbox gets its own feature branch. Sandboxes are fully independent git repos.

### Agent integration

Claude is invoked via `os.execvp` (interactive, replaces the process) or `subprocess.run` (when chaining). Each command builds a system prompt with relevant project context, plan content, and sandbox paths. The `--dangerously-skip-permissions` flag is used throughout since the agent needs full filesystem access in the sandbox.

### tmux sessions

Long-running processes run in named tmux sessions:

| Session | Created by |
|---|---|
| `wb-{slug}` | `wb implement --bg` (implement + review pipeline) |
| `wb-{slug}-ci` | `wb approve` (CI monitor loop) |
| `wb-{slug}-dev` | `wb dev` (Phoenix dev server) |

`wb dev` tracks active servers in project frontmatter (`dev_port`, `dev_session`) and cross-checks against live tmux sessions to offer skip/replace/add-port options when multiple projects are running simultaneously.

### CI monitor

`wb approve` writes a bash script that polls `gh pr view` every 60 seconds. On merge it archives the project note, clears dev server state, and sends a macOS notification. On CI failure it invokes Claude autonomously to fix the issues.

## Configuration

```toml
[core]
obsidian_vault = "~/path/to/vault"
projects_folder = "Projects"
sandbox_root = "~/Projects/work"
branch_prefix = "your-initials"

[github]
user = "your-github-username"
repo = "org/repo"

[agent]
test_cmd = "uv run pytest -x"
lint_cmd = "ruff check && ruff format --check"
```

## Dependencies

- Python 3.11+, [`uv`](https://github.com/astral-sh/uv) (script runner)
- `typer`, `rich`, `pyyaml` (auto-installed by uv)
- `gh` CLI, `tmux`, `git`
- `claude` CLI (Claude Code)
- macOS (uses `osascript` for notifications)
