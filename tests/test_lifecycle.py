"""End-to-end tests for the knowledge-layer (Plan #1).

Covers:
- Lifecycle classification rules (deterministic)
- arc migrate-lifecycle (dry-run, apply, idempotent, honors overrides)
- organize.py scan_vault filters out log/frozen notes
- Project.refresh_activity_footer behavior + frozen no-op
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import arc
import organize


FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Copy the fixture vault into a writable tmp dir."""
    dst = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, dst)
    return dst


@pytest.fixture
def cfg(tmp_vault: Path, tmp_path: Path) -> arc.Config:
    return arc.Config(
        obsidian_vault=tmp_vault,
        projects_folder="Projects",
        sandbox_root=tmp_path / "sandbox",
        branch_prefix="test",
        github_user="test-user",
        github_repo="test-org/nonexistent-repo-for-testing-12345",
        test_cmd="pytest",
        lint_cmd="ruff check",
    )


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel,expected_lc,expected_st", [
    ("Projects/feat-a/index.md", "live", "project-meta"),
    ("Projects/old-thing/index.md", "frozen", "project-meta"),
    ("Projects/feat-a/notes.md", "log", "session-log"),
    ("Projects/feat-a/stages/1-foo/plan.md", "live", "project-meta"),
    ("Projects/feat-a/stages/1-foo/notes.md", "log", "session-log"),
    ("Clippings/Some Article.md", "reference", "web-clipping"),
    ("Blogs/My Draft.md", "frozen", "authored"),
    ("Research/Methodology.md", "evergreen", "authored"),
    ("Agents/Random Note.md", "evergreen", "authored"),
    ("Agents/Web Clipping.md", "reference", "web-clipping"),
])
def test_arc_classify_for_migrate(tmp_vault: Path, rel: str, expected_lc: str, expected_st: str) -> None:
    path = tmp_vault / rel
    text = path.read_text()
    fm, _ = arc._split_fm(text)
    lc, st = arc._classify_for_migrate(Path(rel), fm, "Projects")
    assert (lc, st) == (expected_lc, expected_st), f"{rel} → {(lc, st)}"


@pytest.mark.parametrize("rel,expected_lc", [
    ("Projects/feat-a/notes.md", "log"),
    ("Clippings/Some Article.md", "reference"),
    ("Blogs/My Draft.md", "frozen"),
    ("Research/Methodology.md", "evergreen"),
    ("Agents/Web Clipping.md", "reference"),
])
def test_organize_classify_lifecycle(tmp_vault: Path, rel: str, expected_lc: str) -> None:
    path = tmp_vault / rel
    text = path.read_text()
    fm, _ = organize.split_note(text)
    lc, _st = organize.classify_lifecycle(rel, fm, "Projects")
    assert lc == expected_lc


def test_classify_honors_explicit_lifecycle(tmp_vault: Path) -> None:
    """Notes with explicit lifecycle in frontmatter are not reclassified."""
    fm = {"lifecycle": "evergreen", "source_type": "authored", "tags": ["foo"]}
    lc, st = arc._classify_for_migrate(Path("Clippings/Some Article.md"), fm, "Projects")
    assert (lc, st) == ("evergreen", "authored")


def test_classify_source_url_overrides_folder(tmp_vault: Path) -> None:
    """A note in Agents/ with a source URL gets classified as web-clipping."""
    fm = {"source": "https://docs.letta.com/x", "tags": ["clippings"]}
    lc, st = arc._classify_for_migrate(Path("Agents/foo.md"), fm, "Projects")
    assert (lc, st) == ("reference", "web-clipping")


# ---------------------------------------------------------------------------
# arc migrate-lifecycle
# ---------------------------------------------------------------------------


def _read_fm(path: Path) -> dict:
    text = path.read_text()
    fm, _ = arc._split_fm(text)
    return fm or {}


def _all_md(vault: Path) -> list[Path]:
    return sorted(p for p in vault.rglob("*.md"))


def _snapshot_mtimes(vault: Path) -> dict[str, float]:
    return {str(p.relative_to(vault)): p.stat().st_mtime for p in _all_md(vault)}


def _patch_load_config(monkeypatch: pytest.MonkeyPatch, cfg: arc.Config) -> None:
    monkeypatch.setattr(arc, "load_config", lambda: cfg)


def test_migrate_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch, cfg: arc.Config, tmp_vault: Path) -> None:
    _patch_load_config(monkeypatch, cfg)
    before = _snapshot_mtimes(tmp_vault)
    arc.migrate_lifecycle(dry_run=True, verbose=False)
    after = _snapshot_mtimes(tmp_vault)
    assert before == after, "dry-run modified files"


def test_migrate_apply_writes_lifecycle_to_all(monkeypatch: pytest.MonkeyPatch, cfg: arc.Config, tmp_vault: Path) -> None:
    _patch_load_config(monkeypatch, cfg)
    arc.migrate_lifecycle(dry_run=False, verbose=False)
    for p in _all_md(tmp_vault):
        fm = _read_fm(p)
        assert fm.get("lifecycle"), f"missing lifecycle: {p}"
        assert fm.get("source_type"), f"missing source_type: {p}"


def test_migrate_idempotent(monkeypatch: pytest.MonkeyPatch, cfg: arc.Config, tmp_vault: Path) -> None:
    _patch_load_config(monkeypatch, cfg)
    arc.migrate_lifecycle(dry_run=False, verbose=False)
    snap1 = _snapshot_mtimes(tmp_vault)
    arc.migrate_lifecycle(dry_run=False, verbose=False)
    snap2 = _snapshot_mtimes(tmp_vault)
    assert snap1 == snap2, "second migration run modified files (not idempotent)"


def test_migrate_honors_existing_lifecycle(monkeypatch: pytest.MonkeyPatch, cfg: arc.Config, tmp_vault: Path) -> None:
    """Pre-setting lifecycle: evergreen on a clipping survives migration."""
    target = tmp_vault / "Clippings" / "Some Article.md"
    text = target.read_text()
    fm, body = arc._split_fm(text)
    fm["lifecycle"] = "evergreen"
    fm["source_type"] = "web-clipping"
    target.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n{body}")

    _patch_load_config(monkeypatch, cfg)
    arc.migrate_lifecycle(dry_run=False, verbose=False)
    final_fm = _read_fm(target)
    assert final_fm["lifecycle"] == "evergreen", "explicit lifecycle was overwritten"


def test_migrate_skips_icloud_placeholders(monkeypatch: pytest.MonkeyPatch, cfg: arc.Config, tmp_vault: Path) -> None:
    """iCloud placeholder files (.foo.md.icloud) must be ignored."""
    placeholder = tmp_vault / ".pending.md.icloud"
    placeholder.write_text("")
    _patch_load_config(monkeypatch, cfg)
    arc.migrate_lifecycle(dry_run=False, verbose=False)
    assert placeholder.read_text() == "", "placeholder was modified"


# ---------------------------------------------------------------------------
# organize.py scan_vault — lifecycle filter
# ---------------------------------------------------------------------------


def test_organize_scan_skips_log_and_frozen(tmp_vault: Path, tmp_path: Path) -> None:
    cfg_dict = {
        "vault": tmp_vault,
        "projects_folder": "Projects",
        "skip_folders": ["Templates", ".obsidian", "Assets"],
        "max_notes_per_run": 100,
        "model": "claude-sonnet-4-20250514",
    }
    state = {"last_run": None, "hashes": {}}
    import logging
    logger = logging.getLogger("test")
    changed = organize.scan_vault(cfg_dict, state, logger)
    rels = {n["rel"] for n in changed}

    # Should NOT include logs (notes.md) or frozen (Blogs, archived index)
    assert "Projects/feat-a/notes.md" not in rels
    assert "Projects/feat-a/stages/1-foo/notes.md" not in rels
    assert "Blogs/My Draft.md" not in rels
    # SHOULD include evergreen + reference
    assert "Research/Methodology.md" in rels
    assert "Agents/Random Note.md" in rels
    assert "Clippings/Some Article.md" in rels


# ---------------------------------------------------------------------------
# Project.refresh_activity_footer
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make subprocess.run for `gh pr list` return empty fast."""
    real = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, list) and len(args) >= 2 and args[0] == "gh" and args[1] == "pr":
            class _R:
                returncode = 0
                stdout = "[]"
                stderr = ""
            return _R()
        return real(args, *a, **kw)

    monkeypatch.setattr(arc.subprocess, "run", fake_run)


def test_refresh_activity_footer_writes_fields(cfg: arc.Config, tmp_vault: Path, fast_gh: None) -> None:
    proj = arc.find_project(cfg, "feat-a")
    proj.refresh_activity_footer(cfg, "chat")

    fm = _read_fm(proj.path)
    assert fm.get("last_activity"), "last_activity not written"
    assert fm.get("last_command") == "chat"
    assert fm.get("active_stage") == "1 foo"
    assert fm.get("lifecycle") == "live"
    assert fm.get("source_type") == "project-meta"


def test_refresh_activity_footer_skips_frozen(cfg: arc.Config, tmp_vault: Path, fast_gh: None) -> None:
    proj = arc.find_project(cfg, "old-thing")
    # Migrate first so old-thing has lifecycle=frozen
    proj.lifecycle = "frozen"
    before = proj.path.read_text()
    proj.refresh_activity_footer(cfg, "chat")
    after = proj.path.read_text()
    assert before == after, "frozen project was modified"


def test_refresh_legacy_project_sets_defaults(cfg: arc.Config, tmp_vault: Path, fast_gh: None) -> None:
    """A project without lifecycle in frontmatter gets lifecycle=live + source_type=project-meta."""
    proj = arc.find_project(cfg, "feat-a")
    assert proj.lifecycle == "", "fixture should not have lifecycle yet"
    proj.refresh_activity_footer(cfg, "plan")
    fm = _read_fm(proj.path)
    assert fm["lifecycle"] == "live"
    assert fm["source_type"] == "project-meta"


# ---------------------------------------------------------------------------
# Obsidian URL builder
# ---------------------------------------------------------------------------


def test_vault_obsidian_url(cfg: arc.Config) -> None:
    url = arc._vault_obsidian_url(cfg, Path("Clippings/Some Article.md"))
    assert url.startswith("obsidian://open?vault=")
    assert "Clippings" in url
    assert "Some" in url and "Article" in url
    assert ".md" not in url, "URL should strip .md"
