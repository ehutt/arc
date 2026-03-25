# arc

A CLI for tracking the arc of AI-assisted projects end-to-end. Each project lives as an Obsidian note with YAML frontmatter; `arc` drives the full lifecycle from GitHub issue to merged PR using Claude for implementation and Codex (or Claude) for code review.

## Why

When you're working on multiple projects in parallel, context gets scattered — across GitHub issues, Obsidian notes, web clippings, and your codebase. You lose track of what's running where, which branch is which, and the current stage of ongoing projects.

arc is opinionated. It bakes in the development patterns and preferences I've landed on after months of working with agents. I built it to solve some very specific problems in my workflow, some of which may or may not be relevant to you. That said, the problems it solves are general. If you're feeling the friction of context switching, parallel development, tracking project status, and documenting agent sessions across long-running AI-assisted projects, then arc might be useful to you as-is. Or at least as a starting point for building your own version. The [customization guide](#customization-guide) is written specifically to help your coding agent of preference adapt arc to your repo, your tools, and your workflow.


The specific problems arc solves:

- **Context management**: Pulls together GitHub issues, Obsidian notes, and repo state into a single project view. Plans, clippings, and related notes are automatically linked and fed to agents as progressively disclosed context.
- **Parallel development**: Each project gets its own isolated git clone (not a worktree — a full clone), its own feature branch, and optionally its own dev server, all sharing a common local database (my personal preference, ymmv). 
- **At-a-glance status**: The dashboard shows all active projects — development and non-development — with their current stage, branch, PR status, and running tmux sessions.
- **Automate the predictable parts**: Code review, CI monitoring, branch management, and PR creation are tedious but mechanical. arc handles them so you can focus on more interesting things without dropping the ball. 

## Workflow

```
1. arc sync / arc new     →  Pull GitHub issues or create a project from scratch
2. arc plan               →  Collect context from Obsidian + GitHub, break into stages
3. arc sandbox / arc dev  →  Create isolated clone, optionally launch dev server
4. arc implement          →  Claude implements (interactive or background)
5. arc review             →  AI code review (Codex or Claude) + human review
6. arc approve            →  Push, open PR, continuous CI monitor with auto-fix
7. arc done / arc archive →  Mark complete, clean up sessions
```

At any point during a project's lifecycle, you can drop in for human-in-the-loop work:

- **`arc chat <slug>`** — start a Claude conversation with full project context loaded. Good for brainstorming, asking questions about the codebase, or thinking through a tricky decision without launching a full implementation session.
- **`arc note <slug>`** — jump straight to the project's Obsidian note. Review plans, check session history, or update the objective.
- **`arc editor <slug>`** — open the project sandbox in VS Code (or Cursor with `--cursor`) with the changed files pre-loaded, so you can review a diff or make a quick manual edit.

These aren't part of the linear workflow — they're escape hatches for when you want to check in, steer, or just read what's happened.

## Highlights

- **Not everything needs an agent** — arc makes it easy to jump between Obsidian, VS Code/Cursor, and the terminal. `arc note`, `arc editor`, and the dashboard are quick-nav commands, not AI wrappers.
- **Separate implementation and review** — use Claude to implement, then Codex (or Claude with a different model) to review. Or skip AI review and review the diff yourself. The steps are decoupled.
- **Obsidian as the knowledge layer** — project context, plans, clippings, and related notes all live in your vault. The vault organizer automatically tags and cross-links new notes to active projects.
- **Project-aware Claude sessions** — the `ARC_PROJECT_SLUG` env var is set before every Claude launch, so your Claude Code status line and hooks can show which project you're working on.
- **Full clones, not worktrees** — each sandbox is a complete git clone via `--reference`, so you get full isolation with minimal disk cost. Dev servers can run in parallel on different ports sharing a common local database.
- **CI monitor with auto-fix** — after opening a PR, arc polls CI status and can automatically invoke Claude to fix failures and push.

## Quick start

### Prerequisites

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (script runner — `arc` uses uv inline scripts, no virtualenv needed)
- [`claude`](https://github.com/anthropics/claude-code) CLI (Claude Code)
- [`codex`](https://github.com/openai/codex) CLI — `npm install -g @openai/codex` (for code review; optional if using `--tool claude`)
- [`gh`](https://cli.github.com/) CLI (authenticated)
- `tmux`, `git`
- [Obsidian](https://obsidian.md/) — all project state lives in your vault as Markdown notes
- [VS Code](https://code.visualstudio.com/) or [Cursor](https://cursor.sh/) — `arc editor` opens sandboxes in your editor (use `--cursor` for Cursor)
- macOS (uses `osascript` for notifications; Linux users can swap for `notify-send`)

### Setup

```bash
git clone https://github.com/ehutt/arc.git
cd arc
arc init   # interactive config setup — or copy config.example.toml to config.toml
```

Make `arc.py` your CLI entry point:

```bash
# Option A: alias
echo 'alias arc="uv run --script ~/path/to/arc/arc.py"' >> ~/.zshrc

# Option B: symlink
ln -s ~/path/to/arc/arc.py ~/.local/bin/arc
```

Project slug autocompletion is built in via Typer — tab-complete works for all commands that take a `<slug>` argument.


## Commands

| Command | Description |
|---|---|
| `arc` | Dashboard: status, stage progress, branches, tmux sessions |
| **Project Management** | |
| `arc sync` | Pull GitHub issues and sync PR status from the configured repo |
| `arc note <slug>` | Open a project's Obsidian note |
| `arc new <title>` | Create a new project note without a GitHub issue |
| `arc done <slug> [stage]` | Mark a stage (or whole project) as done — auto-promotes next stages to `ready`. Use `--skip` to skip instead. |
| `arc archive <slug>` | Shelve a project without completing it — kills sessions but preserves the sandbox for later. Use when pausing or abandoning work. |
| **Planning** | |
| `arc plan <slug>` | Interactive Claude session to plan a project |
| `arc stage <slug>` | List stages; `--add "name"` to add, `--depends-on 1,2` for deps, `--plan <id>` to plan a stage |
| **Development** | |
| `arc sandbox <slug>` | Create an isolated git clone with a feature branch |
| `arc implement <slug>` | Claude implementation; add `--bg` for autonomous mode in tmux |
| `arc editor <slug>` | Open sandbox in VS Code (or Cursor with `--cursor`) with changed files |
| `arc dev <slug>` | Launch dev server in a background tmux session |
| **Review & Ship** | |
| `arc review <slug>` | AI code review (`--tool codex` or `--tool claude`, `--model` to override) |
| `arc approve <slug>` | Push branch, create PR, and launch CI monitor |
| **Utilities** | |
| `arc init` | Interactive setup — create `config.toml` from prompts |
| `arc chat <slug>` | Informal Claude chat with project context |
| `arc organize` | Run the vault organizer (tag & link notes) |

### Status lifecycle

**Project statuses** (derived from stages): `needs-plan` → `planned` → `active` → `done` → `archived`

**Stage statuses**: `pending` → `ready` → `implemented` → `reviewed` → `pr-open` → `done`

Project status is derived from stages automatically, with one exception: a project is only marked `done` via an explicit `arc done <slug>` call (not by completing all stages). All stages being done means the current plan is finished — the project stays `active` until you close it. When the last stage completes, arc prints a hint to run `arc done` when you're ready.

Stages auto-promote from `pending` to `ready` when their dependencies are met. Arc owns all status transitions — agents don't update frontmatter.

### Project folder structure

```
Projects/my-feature/
  index.md              # frontmatter + objective + tasks (lean)
  notes.md              # project-level session log
  stages/
    1-api-endpoints/
      plan.md           # stage plan
      notes.md          # stage session log
    2-ui-components/
      plan.md
      notes.md
```

Session notes, plans, and CI events all go to the appropriate `notes.md` — never into `index.md`. Each stage gets its own folder with isolated context.

Existing projects are auto-migrated on load: `## Notes` sections are extracted from `index.md` into `notes.md`, old statuses are mapped to the new system, and stage folders are created.

## Configuration

Run `arc init` for interactive setup, or copy `config.example.toml` to `config.toml` and edit it. The file is gitignored — your personal config stays local.

```toml
[core]
obsidian_vault = "~/path/to/your/obsidian-vault"
projects_folder = "Projects"
sandbox_root = "~/Projects/my-repo-clones"
branch_prefix = "your-username"

[github]
user = "your-github-username"
repo = "org/repo-name"

[agent]
test_cmd = "pytest tests/"
lint_cmd = "make lint"

[organize]
skip_folders = ["Templates", ".obsidian", "Assets"]
max_notes_per_run = 20
model = "claude-sonnet-4-20250514"
```

### Config reference

| Key | Purpose | Example |
|---|---|---|
| `core.obsidian_vault` | Path to your Obsidian vault (~ expanded) | `"~/Documents/Notes"` |
| `core.projects_folder` | Folder inside the vault for project notes | `"Projects"` |
| `core.sandbox_root` | Where isolated git clones are created | `"~/Projects/my-repo-clones"` |
| `core.branch_prefix` | Prefix for feature branches (`prefix/slug`) | `"jdoe"` |
| `github.user` | Your GitHub username (for filtering assigned issues) | `"jdoe"` |
| `github.repo` | Target repo in `org/name` format | `"my-org/my-app"` |
| `agent.test_cmd` | Test command run in sandboxes by agents | `"pytest tests/ -x"` |
| `agent.lint_cmd` | Lint command run in sandboxes by agents | `"ruff check ."` |
| `organize.skip_folders` | Vault folders the organizer ignores | `["Templates", "Assets"]` |
| `organize.max_notes_per_run` | Caps API calls per organizer run | `20` |
| `organize.model` | Claude model for the vault organizer | `"claude-sonnet-4-20250514"` |

## Customization guide

### Pointing arc at a different repo

1. **`config.toml`**: Set `github.repo` to the target repo (e.g. `"my-org/my-app"`) and `github.user` to the user's GitHub username.
2. **`config.toml`**: Set `sandbox_root` to wherever clones should live (e.g. `"~/Projects/my-app-clones"`).
3. **`arc.py`** `Config.bare_repo` property: Returns `self.sandbox_root / ".phoenix-bare"`. Rename `.phoenix-bare` to something appropriate (e.g. `".my-app-bare"`). This is a local bare git reference used for fast `git clone --reference`.
4. **`arc.py`** `_load_env_keys()` function: Loads API keys from a `.env` file at `cfg.sandbox_root.parent / "phoenix" / ".env"`. Change `"phoenix"` to the directory name of your main repo checkout, or remove this function if you manage env vars differently.

### Customizing the dev server command

The `arc dev` command launches a dev server in tmux. By default it runs `make dev` in the sandbox. To customize:

1. **Change the launch command**: In `arc.py`, find the `dev_script` construction (search for `exec make`). Replace the last line with your app's dev server command (e.g. `exec npm run dev`, `exec cargo run`, etc.).
2. **Change the default port**: The default port is `6006`. Change to whatever your dev server uses.
3. **Phoenix-specific env vars**: The `PHOENIX_CLOUD_VARS` list and `PHOENIX_WORKING_DIR` are specific to the Phoenix app. Remove or replace these with env vars relevant to your app.

### Customizing environment variable injection

arc injects env vars into agent subprocesses in two ways:

1. **`MODEL_API_KEYS` list**: API keys carried into tmux sessions and agent subprocesses. Currently: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `KAGGLE_USERNAME`, `KAGGLE_KEY`. Add or remove keys as needed for your setup.

2. **`_load_env_keys()` function**: Reads a `.env` file and loads matching keys into `os.environ`. The path is currently `cfg.sandbox_root.parent / "phoenix" / ".env"`. Point this at your repo's `.env` file, or remove it if you set env vars another way.

3. **`_clean_env()` function**: Strips conda/virtualenv paths from `PATH` before launching agents. This prevents Claude and Codex from seeing the wrong Python environment. If you don't use conda, this is harmless. If you use a different environment manager, you may need to adjust the path filters.

### Customizing the code review

`arc review` defaults to Codex but supports both tools:

```bash
arc review <slug>                        # codex (default)
arc review <slug> --tool claude          # claude instead
arc review <slug> --model gpt-5.3-codex # codex with specific model
arc review <slug> --tool claude -m claude-sonnet-4-20250514
```

To change the default, edit `DEFAULT_REVIEW_TOOL` and `DEFAULT_REVIEW_MODEL` near the top of `_run_review()` in `arc.py`.

### System prompts injected by arc

arc injects system prompts into Claude and Codex at several points. You can customize these to match your team's conventions, coding standards, or review criteria. Each prompt includes project context (title, paths to notes/plans) and task-specific instructions. Agents are told to write session notes to the appropriate `notes.md` and are explicitly told *not* to update status in frontmatter.

| Command | Where in `arc.py` | What the prompt does |
|---|---|---|
| `arc plan` | `plan()` function | Instructs Claude to write a plan; points to project note and vault |
| `arc implement` (interactive) | `_implement_interactive_simple()` and `_implement_interactive_staged()` | Gives Claude the project context, sandbox path, and git commit instructions. For staged projects, auto-includes `plan.md` and `notes.md` from all dependent stages under a "Prior Stages" section. |
| `arc implement --bg` | `_implement_bg()` — writes a `CLAUDE.md` to the sandbox | Autonomous implementation instructions with paths to project note and notes file |
| `arc implement --bg` | `_implement_bg()` — inline `-p` flag | One-line prompt passed to `claude` CLI in the orchestrator script |
| `arc review` | `_run_review()` | Code quality review prompt: diff against main, check correctness, run tests/lint, fix issues |
| `arc chat` | `chat()` function | Lightweight context prompt with paths to project note and notes file |
| `arc approve` (CI fix) | CI monitor script in `approve()` | Instructs Claude to read CI failures, fix them, and push |

### Claude Code integration

arc sets the `ARC_PROJECT_SLUG` environment variable before launching Claude sessions. This lets you customize Claude Code's behavior per-project using hooks and the status line.

**Status line**: If you want your Claude Code status bar to show the active project, create `~/.claude/statusline.sh`:

```bash
#!/bin/bash
input=$(cat)
model=$(echo "$input" | jq -r '.model.display_name')
cwd=$(echo "$input" | jq -r '.cwd')
dir="${cwd##*/}"
pct=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)

# Use arc project slug if available, otherwise directory name
project="${ARC_PROJECT_SLUG:-$dir}"

echo "[$model] $project | ${pct}% context"
```

Then in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh"
  }
}
```

**Tab title hook**: To set your terminal tab/window title to the project name, create a hook script (e.g. `~/.claude/hooks/set-tab-title.sh`):

```bash
#!/bin/bash
label="${ARC_PROJECT_SLUG:-$(basename "${CLAUDE_PROJECT_DIR:-unknown}")}"
printf '\033]1;CC: %s\033\\' "$label"
printf '\033]2;Claude Code — %s\033\\' "$label"
```

Then in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/set-tab-title.sh"
          }
        ]
      }
    ]
  }
}
```

These are optional — arc works without them. They just make it easier to track which project you're in when running multiple Claude sessions.

### Customizing the vault organizer

`organize.py` runs independently as a uv inline script. It uses Claude to tag notes and link them to projects.

1. **Scheduling**: Set up a launchd plist (macOS) or cron job (Linux) to run `uv run --script organize.py` periodically.
2. **API key**: The organizer looks for `ANTHROPIC_API_KEY` in the environment, then falls back to macOS Keychain (`security find-generic-password -a vault-organize -s ANTHROPIC_API_KEY`). Set whichever is convenient.
3. **Model**: Configured via `organize.model` in `config.toml`.
4. **System prompt**: The `SYSTEM_PROMPT` in `organize.py` describes the vault owner as "an AI engineer focused on LLM evaluation." Change this to match the user's domain for better tagging.

## Architecture

**`arc.py`** (~2400 lines) — main CLI built with Typer. All project state lives in Obsidian YAML frontmatter — no database.

**`organize.py`** — standalone vault organizer. Scans for new/changed notes, classifies them with Claude, adds tags and wikilinks.

**`config.toml`** — user configuration (gitignored, see `config.example.toml`).

### Data model

- **`Config`** — parsed from `config.toml`: vault path, sandbox root, GitHub repo, branch prefix, test/lint commands
- **`Project`** — parsed from `{vault}/Projects/{slug}/index.md` frontmatter: slug, title, status, branch, sandbox path, stages, linked issues/PRs. Project status is derived from stage statuses (except `done`, which requires explicit `arc done`).
- **`Stage`** — subtask with dependency graph: id, name, status, depends_on, github_issues, github_prs. Each stage gets a folder under `stages/` with `plan.md` and `notes.md`.

### tmux sessions

| Session | Created by |
|---|---|
| `arc-{slug}` | `arc implement --bg` (autonomous implementation) |
| `arc-{slug}-ci` | `arc approve` (CI monitor loop) |
| `arc-{slug}-dev` | `arc dev` (dev server) |

### Agent pipeline

```
arc implement  →  Claude writes code  →  you review the diff
arc review     →  AI code review (codex or claude)
arc approve    →  PR + CI monitor  →  auto-fix CI failures  →  merge
```

In `--bg` mode, implementation runs in a tmux session with macOS notifications on completion. Review is a separate step you run when ready.

## License

MIT
