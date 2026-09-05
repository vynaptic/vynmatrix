"""Tests for git mr helper utilities and push guard."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from dev_cli.core.git_mr import (
    SubmitOptions,
    build_feature_branch_name,
    build_pr_create_command,
    extract_pr_url,
    install_git_mr_alias_and_hooks,
    slugify_subject,
)


def test_slugify_subject_normalizes_commit_subject() -> None:
    assert slugify_subject("Feat: Add MR Submit!!!") == "feat-add-mr-submit"
    assert slugify_subject("   ") == "changes"


def test_build_feature_branch_name_uses_timestamp_and_slug() -> None:
    branch_name = build_feature_branch_name(
        "feat: add submit flow",
        now=datetime(2026, 3, 3, 11, 22, 33, tzinfo=UTC),
    )
    assert branch_name == "feature/20260303-112233-feat-add-submit-flow"


def test_build_pr_create_command_includes_optional_flags() -> None:
    options = SubmitOptions(draft=True, title="Title", body="Body")
    cmd = build_pr_create_command("feature/20260303-112233-test", options)

    assert cmd == [
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        "feature/20260303-112233-test",
        "--fill",
        "--draft",
        "--title",
        "Title",
        "--body",
        "Body",
    ]


def test_extract_pr_url_reads_pr_link_from_output() -> None:
    output = "Created pull request:\nhttps://github.com/acme/repo/pull/42\n"
    assert extract_pr_url(output) == "https://github.com/acme/repo/pull/42"
    assert extract_pr_url("no url") is None


def test_prepush_hook_blocks_direct_push_to_main() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    hook_path = repo_root / ".githooks" / "pre-push"
    result = subprocess.run(
        ["bash", str(hook_path)],
        input="refs/heads/main abc refs/heads/main def\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "git mr submit" in result.stdout


def test_prepush_hook_rejects_environment_override() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    hook_path = repo_root / ".githooks" / "pre-push"
    env = os.environ.copy()
    env["VM_ALLOW_MAIN_PUSH"] = "1"

    result = subprocess.run(
        ["bash", str(hook_path)],
        input="refs/heads/main abc refs/heads/main def\n",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 1
    assert "Direct push to origin/main is blocked" in result.stdout


def test_git_install_preserves_global_configuration(tmp_path, monkeypatch) -> None:
    global_config = tmp_path / "global.gitconfig"
    original = "[alias]\n\tmr = existing-command\n"
    global_config.write_text(original)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    result = install_git_mr_alias_and_hooks(repo)

    assert global_config.read_text() == original
    assert result.alias_value == "!vmdev git mr"
    local_alias = subprocess.run(
        ["git", "config", "--local", "--get", "alias.mr"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert local_alias.stdout.strip() == result.alias_value


def test_installed_hooks_enforce_quality_gate_on_real_commit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "hook-cache"))
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Maintainer"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "maintainer@example.invalid"], cwd=repo, check=True
    )
    source = Path(__file__).resolve().parents[3]
    shutil.copytree(source / ".githooks", repo / ".githooks")
    install_git_mr_alias_and_hooks(repo)
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n- repo: local\n  hooks:\n  - id: quality-gate\n"
        "    name: quality-gate\n    entry: python validate.py\n"
        "    language: system\n    always_run: true\n    pass_filenames: false\n"
    )
    (repo / "validate.py").write_text(
        'from pathlib import Path\nraise SystemExit(int(Path("blocked").exists()))\n'
    )
    (repo / "blocked").touch()
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    rejected = subprocess.run(
        ["git", "commit", "-m", "test: rejected"],
        check=False,
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert "quality-gate" in rejected.stdout + rejected.stderr

    (repo / "blocked").unlink()
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    accepted = subprocess.run(
        ["git", "commit", "-m", "test: accepted"],
        check=False,
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
