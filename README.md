# arc

A CLI for managing AI-assisted engineering projects. Each project lives as an Obsidian note with YAML frontmatter; `arc` drives the full lifecycle from GitHub issue to merged PR using Claude as the implementation agent and Codex for code review.

## What it does

- Pulls your assigned GitHub issues and creates Obsidian project notes
- Creates isolated git sandboxes (clones) per project with feature branches
- Launches Claude to implement changes, then Codex to review them
- Monitors CI, auto-fixes failures, and manages PRs
- Runs multiple projects in parallel via tmux sessions
- Keeps all state in human-readable Obsidian Markdown — no database

## Quick start

### Prerequisites

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (script runner — `arc` uses uv inline scripts, no virtualenv needed)
- [`claude`](https://github.com/anthropics/claude-code) CLI (Claude Code)
- [`codex`](https://github.com/openai/codex) CLI — `npm install -g @openai/codex`
- [`gh`](https://cli.github.com/) CLI (authenticated)
- `tmux`, `git`
- macOS (uses `osascript` for notifications; Linux users can swap for `notify-send`)

### Setup

```bash
git clone https://github.com/ehutt/arc.git
cd arc
cp config.example.toml config.toml
# Edit config.toml with your paths and repo info (see Configuration below)
```

Make `arc.py` your CLI entry point:

```bash
# Option A: alias
echo 'alias arc="uv run --script ~/path/to/arc/arc.py"' >> ~/.zshrc

# Option B: symlink
ln -s ~/path/to/arc/arc.py ~/.local/bin/arc
```

### First run

```bash
arc sync          # Pull your assigned GitHub issues into Obsidian
arc               # Dashboard — see all projects and their status
arc plan <slug>   # Interactive Claude session to write a plan
arc sandbox <slug>  # Create an isolated git clone with feature branch
arc implement <slug>  # Claude implements → Codex reviews
arc approve <slug>    # Push, open PR, monitor CI
```

## Commands

| Command | Description |
|---|---|
| `arc` | Dashboard: status, stage progress, branches, tmux sessions |
| `arc new <title>` | Create a project note from scratch (no GitHub issue needed) |
| `arc sync` | Pull assigned GitHub issues; create/attach project notes |
| `arc plan <slug>` | Interactive Claude session to write a plan |
| `arc stage <slug>` | List, add, or plan stages for a project |
| `arc sandbox <slug>` | Create an isolated git clone with a feature branch |
| `arc implement <slug>` | Claude implementation → automatic Codex code review |
| `arc implement <slug> --bg` | Same, but runs autonomously in a tmux session |
| `arc done <slug> [stage]` | Mark a stage done (or `--skip`) |
| `arc approve <slug>` | Push branch, create PR via `gh`, launch CI monitor |
| `arc dev <slug>` | Launch your app's dev server in background tmux |
| `arc archive <slug>` | Archive a project and kill all background sessions |
| `arc open <slug>` | Open project note in Obsidian |
| `arc chat <slug>` | Informal Claude chat with project context |
| `arc cursor <slug>` | Open sandbox in Cursor with changed files |
| `arc organize` | Run the vault organizer (tag & link notes) |

Status progresses automatically: `needs-plan` → `ready` → `implementing` → `reviewing` → `awaiting-approval` → `pr-open` → `archived`

## Configuration

Copy `config.example.toml` to `config.toml` and edit it. The file is gitignored — your personal config stays local.

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

This section is written for a coding agent helping a new user set up arc for their own repo and workflow.

### Pointing arc at a different repo

1. **`config.toml`**: Set `github.repo` to the target repo (e.g. `"my-org/my-app"`) and `github.user` to the user's GitHub username.
2. **`config.toml`**: Set `sandbox_root` to wherever clones should live (e.g. `"~/Projects/my-app-clones"`).
3. **`arc.py` line ~147**: The `bare_repo` property returns `self.sandbox_root / ".phoenix-bare"`. Rename `.phoenix-bare` to something appropriate (e.g. `".my-app-bare"`). This is a local bare git reference used for fast `git clone --reference`.
4. **`arc.py` `_load_env_keys()`** (line ~2083): This function loads API keys from a `.env` file at `cfg.sandbox_root.parent / "phoenix" / ".env"`. Change `"phoenix"` to the directory name of your main repo checkout, or remove this function if you manage env vars differently.

### Customizing the dev server command

The `arc dev` command (line ~2098) launches a dev server in tmux. By default it runs `make dev` in the sandbox. To customize:

1. **Change the launch command**: In `arc.py`, find the `dev_script` construction (line ~2213). The last line is `exec make -C "{sandbox_path}" dev`. Replace this with your app's dev server command (e.g. `exec npm run dev`, `exec cargo run`, etc.).
2. **Change the default port**: The default port is `6006` (line ~2166). Change to whatever your dev server uses.
3. **Phoenix-specific env vars**: The `PHOENIX_CLOUD_VARS` list (line ~2074) and `PHOENIX_WORKING_DIR` (line ~2119) are specific to the Phoenix app. Remove or replace these with env vars relevant to your app.

### Customizing environment variable injection

arc injects env vars into agent subprocesses in two ways:

1. **`MODEL_API_KEYS` list** (line ~2065): API keys carried into tmux sessions and agent subprocesses. Currently: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `KAGGLE_USERNAME`, `KAGGLE_KEY`. Add or remove keys as needed for your setup.

2. **`_load_env_keys()` function** (line ~2081): Reads a `.env` file and loads matching keys into `os.environ`. The path is currently `cfg.sandbox_root.parent / "phoenix" / ".env"`. Point this at your repo's `.env` file, or remove it if you set env vars another way.

3. **`_clean_env()` function** (line ~56): Strips conda/virtualenv paths from `PATH` before launching agents. This prevents Claude and Codex from seeing the wrong Python environment. If you don't use conda, this is harmless. If you use a different environment manager, you may need to adjust the path filters.

4. **`ARC_PROJECT_SLUG` env var** (line ~53): Set before launching Claude so that Claude Code hooks can identify which project is active. If you use Claude Code hooks, you can read this variable to customize behavior per-project.

### Customizing the code review agent

The review step uses OpenAI Codex by default:

1. **`CODEX_MODEL`** (line ~1182): The model used for code review. Currently `"gpt-5.3-codex"`.
2. **Review prompt**: The `_codex_review()` function (line ~1185) constructs the review prompt. Modify the review instructions to match your team's standards.
3. **To use a different reviewer** (e.g. Claude for both steps): Replace the `codex exec` call in `_codex_review()` and in the background orchestrator script (`_implement_bg()`).

### Customizing the vault organizer

`organize.py` runs independently as a uv inline script. It uses Claude to tag notes and link them to projects.

1. **Scheduling**: Set up a launchd plist (macOS) or cron job (Linux) to run `uv run --script organize.py` periodically.
2. **API key**: The organizer looks for `ANTHROPIC_API_KEY` in the environment, then falls back to macOS Keychain (`security find-generic-password -a vault-organize -s ANTHROPIC_API_KEY`). Set whichever is convenient.
3. **Model**: Configured via `organize.model` in `config.toml`.
4. **System prompt**: The `SYSTEM_PROMPT` in `organize.py` (line ~248) describes the vault owner as "an AI engineer focused on LLM evaluation." Change this to match the user's domain for better tagging.

## Architecture

**`arc.py`** (~2400 lines) — main CLI built with Typer. All project state lives in Obsidian YAML frontmatter — no database.

**`organize.py`** — standalone vault organizer. Scans for new/changed notes, classifies them with Claude, adds tags and wikilinks.

**`config.toml`** — user configuration (gitignored, see `config.example.toml`).

### Data model

- **`Config`** — parsed from `config.toml`: vault path, sandbox root, GitHub repo, branch prefix, test/lint commands
- **`Project`** — parsed from `{vault}/Projects/{slug}/index.md` frontmatter: slug, title, status, branch, sandbox path, stages, linked issues/PRs
- **`Stage`** — subtask with dependency graph: id, name, status (pending/running/done/skipped), plan file, depends_on list

### tmux sessions

| Session | Created by |
|---|---|
| `arc-{slug}` | `arc implement --bg` (autonomous implement + review) |
| `arc-{slug}-ci` | `arc approve` (CI monitor loop) |
| `arc-{slug}-dev` | `arc dev` (dev server) |

### Agent pipeline

`arc implement` → Claude writes code → Codex reviews → you approve → `arc approve` → PR + CI monitor → auto-fix CI failures → merge

In `--bg` mode, the entire pipeline runs in a tmux session with macOS notifications on completion.

## License

MIT
