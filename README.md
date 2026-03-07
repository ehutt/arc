# wb

A personal CLI for managing AI-assisted engineering projects on the [Phoenix](https://github.com/Arize-ai/phoenix) repo. Each project lives as an Obsidian note; `wb` drives the full lifecycle from GitHub issue to merged PR using Claude as the implementation agent.

## Goals

- Keep all project state in Obsidian (human-readable, searchable, synced via iCloud)
- Automate the repetitive parts: sandbox setup, branch management, PR creation, CI monitoring
- Provide a consistent interface for launching Claude as a coding agent with the right context
- Support running multiple projects in parallel without interference

## Workflow

```
wb new <title>   # create a project note from scratch (no GitHub issue needed)
wb sync          # pull assigned GitHub issues, create/attach project notes
wb plan <slug>   # interactive Claude session to write a plan
wb sandbox <slug>  # clone Phoenix into an isolated sandbox
wb implement <slug>         # Claude implementation → Codex code review
wb implement <slug> --bg    # autonomous Claude → Codex review pipeline in tmux
wb done <slug> [stage]      # mark a stage done without launching Claude
wb approve <slug>  # push branch, open PR, start CI monitor
wb dev <slug>      # launch Phoenix dev server in background tmux
wb archive <slug>  # archive a project and kill all background sessions
```

Status progresses automatically: `needs-plan` → `ready` → `implementing` → `reviewing` → `awaiting-approval` → `pr-open` → `archived`

## Commands

| Command | Description |
|---|---|
| `wb` | Dashboard: status, stage progress, branch, tmux sessions, active dev port |
| `wb new <title>` | Create a project note from scratch without a GitHub issue |
| `wb sync` | Pull assigned GitHub issues; create or attach project notes |
| `wb plan <slug>` | Launch interactive Claude session to plan and write a plan note |
| `wb stage <slug>` | List, add, or plan stages for a project |
| `wb sandbox <slug>` | Create an isolated git clone with a feature branch and Python env |
| `wb implement <slug>` | Claude implementation → automatic Codex code review |
| `wb implement <slug> --bg` | Autonomous Claude implementation → Codex review pipeline in tmux |
| `wb done <slug> [stage]` | Mark a stage done (or skipped with `--skip`) without launching Claude |
| `wb approve <slug>` | Push branch, create PR via `gh`, launch CI monitor in tmux |
| `wb dev <slug>` | Launch Phoenix dev server in a background tmux session with port management |
| `wb archive <slug>` | Archive a project and kill all background sessions |
| `wb open <slug>` | Open project note in Obsidian |
| `wb chat <slug>` | Informal Claude chat session with project context |
| `wb cursor <slug>` | Open sandbox in Cursor with changed files pre-loaded |
| `wb organize` | Run the daily vault organizer (tag & link notes) |

## Architecture

**Core:** `wb.py` (~1700 lines) + `organize.py` + `config.toml`. No database — all state is YAML frontmatter in Obsidian notes.

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

`wb sandbox` clones Phoenix using a local bare reference repo (`{sandbox_root}/.phoenix-bare`) for fast clones via `--reference --shared`. Each sandbox gets its own feature branch and runs `uv sync --python 3.10` to set up the Python environment automatically. Sandboxes are fully independent git repos.

### Agent integration

`wb implement` uses a two-agent pipeline: **Claude** handles implementation and **Codex** (OpenAI, `gpt-5.3-codex`) handles code review.

**Implementation (Claude):** Invoked via `subprocess.run` with a system prompt containing project context, plan content, and sandbox paths. Uses `--dangerously-skip-permissions` for full filesystem access. All agent subprocesses run with a cleaned environment (`_clean_env()`) that strips conda/virtualenv variables and paths, preventing package resolution from the host's Python environment.

**Code review (Codex):** Runs automatically after Claude finishes. Codex receives the full project documentation, all stage plans, and the current stage context. It reviews the diff against `main`, runs tests and lint, fixes any issues, and writes `.wb-review.md`. Uses `codex exec --dangerously-bypass-approvals-and-sandbox`. The model is configured via `CODEX_MODEL` (currently `gpt-5.3-codex`).

In interactive mode, you are prompted to approve only after both agents complete. In `--bg` mode, a macOS notification fires when the full pipeline finishes.

Other commands (`plan`, `chat`, `cursor`) still use Claude via `os.execvp` (replaces the process).

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

### Vault organizer

`organize.py` is a separate inline uv script that runs daily (via launchd) to keep the vault tidy:

1. Scans vault for new/changed `.md` files (tracked by body SHA-256 in `.organize-state.json`)
2. Sends each changed note to Claude with vault context (existing tags, active projects)
3. Adds tags to frontmatter (additive only), inserts `[[wikilinks]]`, appends to project `related_notes`
4. All writes are atomic (write `.tmp` then `os.replace()`); iCloud placeholders are skipped

The launchd agent (`com.elizabethhutton.vault-organize`) runs hourly and at login. The script debounces to once per day. Use `wb organize --dry-run` to preview changes, or `wb organize --force` to re-run.

API key is stored in macOS Keychain (`security find-generic-password -a vault-organize -s ANTHROPIC_API_KEY`).

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
test_cmd = "tox run -e unit_tests"
lint_cmd = "make lint-python"

[organize]
skip_folders = ["Templates", ".obsidian", "Assets"]
max_notes_per_run = 20
model = "claude-sonnet-4-20250514"
```

## Dependencies

- Python 3.11+, [`uv`](https://github.com/astral-sh/uv) (script runner)
- `typer`, `rich`, `pyyaml`, `anthropic` (auto-installed by uv)
- `gh` CLI, `tmux`, `git`
- `claude` CLI (Claude Code)
- `codex` CLI ([OpenAI Codex](https://github.com/openai/codex)) — `npm install -g @openai/codex`
- macOS (uses `osascript` for notifications)
