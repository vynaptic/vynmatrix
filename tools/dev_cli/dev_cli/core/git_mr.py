"""GitHub PR submission helpers for the vmdev CLI."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import which


class GitMRFlowError(RuntimeError):
    """Raised when a git mr workflow step fails."""


@dataclass(frozen=True)
class InstallResult:
    """Result for git alias/hook installation."""

    alias_value: str
    hooks_path_value: str


@dataclass(frozen=True)
class SubmitOptions:
    """Options for MR submit workflow."""

    draft: bool = False
    title: str | None = None
    body: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    """Result for MR submit workflow."""

    branch_name: str
    pr_url: str


def _run_command(
    cmd: list[str],
    cwd: Path,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return a text-mode process result."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=capture_output,
        check=check,
    )


def _run_git(
    args: list[str],
    repo_root: Path,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run git in the provided repository root."""
    return _run_command(["git", *args], cwd=repo_root, check=check, capture_output=capture_output)


def _run_gh(
    args: list[str],
    repo_root: Path,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run gh in the provided repository root."""
    return _run_command(["gh", *args], cwd=repo_root, check=check, capture_output=capture_output)


def _require_git_repository(repo_root: Path) -> None:
    """Ensure the path is a git repository."""
    try:
        result = _run_git(["rev-parse", "--is-inside-work-tree"], repo_root)
    except subprocess.CalledProcessError as exc:
        message = "Current directory is not inside a git repository."
        raise GitMRFlowError(message) from exc

    if result.stdout.strip().lower() != "true":
        message = "Current directory is not inside a git repository."
        raise GitMRFlowError(message)


def _get_current_branch(repo_root: Path) -> str:
    """Return active branch name."""
    result = _run_git(["branch", "--show-current"], repo_root)
    return result.stdout.strip()


def _get_remote_url(repo_root: Path, remote: str = "origin") -> str:
    """Return URL for the requested remote."""
    try:
        result = _run_git(["remote", "get-url", remote], repo_root)
    except subprocess.CalledProcessError as exc:
        message = f"Git remote '{remote}' was not found."
        raise GitMRFlowError(message) from exc
    return result.stdout.strip()


def _is_github_remote(remote_url: str) -> bool:
    """Check if remote URL points to GitHub."""
    return "github.com" in remote_url.lower()


def _require_clean_worktree(repo_root: Path) -> None:
    """Ensure no staged/unstaged/untracked changes exist."""
    result = _run_git(["status", "--porcelain"], repo_root)
    if result.stdout.strip():
        message = (
            "Working tree is not clean. Commit, stash, or discard local changes before "
            "running 'git mr submit'."
        )
        raise GitMRFlowError(message)


def _require_gh_installed() -> None:
    """Ensure GitHub CLI is available."""
    if which("gh") is None:
        message = "GitHub CLI (gh) is not installed. Install gh to use 'git mr submit'."
        raise GitMRFlowError(message)


def _require_gh_auth(repo_root: Path) -> None:
    """Ensure gh has valid auth for github.com."""
    result = _run_gh(["auth", "status", "-h", "github.com"], repo_root, check=False)
    if result.returncode != 0:
        message = "GitHub CLI authentication is invalid. Run:\n  gh auth login -h github.com"
        raise GitMRFlowError(message)


def _fetch_origin_main(repo_root: Path) -> None:
    """Fetch latest origin/main before ahead/behind checks."""
    try:
        _run_git(["fetch", "origin", "main"], repo_root, capture_output=False)
    except subprocess.CalledProcessError as exc:
        message = "Failed to fetch origin/main. Check remote connectivity and permissions."
        raise GitMRFlowError(message) from exc


def _require_origin_main_exists(repo_root: Path) -> None:
    """Ensure origin/main ref exists after fetch."""
    try:
        _run_git(["rev-parse", "--verify", "origin/main"], repo_root)
    except subprocess.CalledProcessError as exc:
        message = "Remote ref origin/main does not exist."
        raise GitMRFlowError(message) from exc


def _count_commits(repo_root: Path, revspec: str) -> int:
    """Count commits for a rev-list spec."""
    result = _run_git(["rev-list", "--count", revspec], repo_root)
    return int(result.stdout.strip())


def _get_first_ahead_subject(repo_root: Path) -> str:
    """Get subject line from oldest ahead commit."""
    result = _run_git(["log", "--reverse", "--format=%s", "origin/main..main"], repo_root)
    for line in result.stdout.splitlines():
        subject = line.strip()
        if subject:
            return subject
    return "changes"


def slugify_subject(subject: str) -> str:
    """Convert commit subject to branch-safe slug."""
    lowered = subject.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return "changes"
    return slug[:48].strip("-") or "changes"


def build_feature_branch_name(subject: str, now: datetime | None = None) -> str:
    """Build feature branch name from timestamp + commit subject."""
    ts = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    slug = slugify_subject(subject)
    return f"feature/{ts}-{slug}"


def _local_branch_exists(repo_root: Path, branch_name: str) -> bool:
    """Check if local branch already exists."""
    result = _run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        repo_root,
        check=False,
    )
    return result.returncode == 0


def _ensure_unique_branch_name(repo_root: Path, branch_name: str) -> str:
    """Ensure branch name doesn't collide with existing local branch."""
    if not _local_branch_exists(repo_root, branch_name):
        return branch_name

    suffix = 2
    while True:
        candidate = f"{branch_name}-{suffix}"
        if not _local_branch_exists(repo_root, candidate):
            return candidate
        suffix += 1


def build_pr_create_command(branch_name: str, options: SubmitOptions) -> list[str]:
    """Build gh pr create command arguments."""
    cmd = ["pr", "create", "--base", "main", "--head", branch_name, "--fill"]
    if options.draft:
        cmd.append("--draft")
    if options.title:
        cmd.extend(["--title", options.title])
    if options.body:
        cmd.extend(["--body", options.body])
    return cmd


def extract_pr_url(output: str) -> str | None:
    """Extract first GitHub PR URL from command output."""
    match = re.search(r"https://github\.com/\S+/pull/\d+", output)
    if match:
        return match.group(0)
    return None


def install_git_mr_alias_and_hooks(repo_root: Path) -> InstallResult:
    """Install the Git alias and hooks path only in this repository."""
    _require_git_repository(repo_root)

    _run_git(["config", "--local", "alias.mr", "!vmdev git mr"], repo_root)
    _run_git(["config", "--local", "core.hooksPath", ".githooks"], repo_root)

    alias_value = _run_git(["config", "--local", "--get", "alias.mr"], repo_root).stdout.strip()
    hooks_path_value = _run_git(
        ["config", "--local", "--get", "core.hooksPath"], repo_root
    ).stdout.strip()

    return InstallResult(alias_value=alias_value, hooks_path_value=hooks_path_value)


def submit_from_main(repo_root: Path, options: SubmitOptions) -> SubmitResult:
    """Create a PR from local main commits and reset local main."""
    _require_git_repository(repo_root)
    _require_clean_worktree(repo_root)

    current_branch = _get_current_branch(repo_root)
    if current_branch != "main":
        message = f"Current branch is '{current_branch}'. Switch to 'main' before running submit."
        raise GitMRFlowError(message)

    remote_url = _get_remote_url(repo_root, "origin")
    if not _is_github_remote(remote_url):
        message = f"Origin remote is not GitHub ({remote_url}). This workflow supports GitHub only."
        raise GitMRFlowError(message)

    _require_gh_installed()
    _require_gh_auth(repo_root)
    _fetch_origin_main(repo_root)
    _require_origin_main_exists(repo_root)

    ahead = _count_commits(repo_root, "origin/main..main")
    behind = _count_commits(repo_root, "main..origin/main")

    if ahead == 0:
        message = "No local commits to submit. main is not ahead of origin/main."
        raise GitMRFlowError(message)
    if behind > 0:
        message = "Local main is behind origin/main. Rebase or pull before submitting."
        raise GitMRFlowError(message)

    first_subject = _get_first_ahead_subject(repo_root)
    proposed_branch = build_feature_branch_name(first_subject)
    branch_name = _ensure_unique_branch_name(repo_root, proposed_branch)

    try:
        _run_git(["switch", "-c", branch_name], repo_root, capture_output=False)
        _run_git(["push", "-u", "origin", branch_name], repo_root, capture_output=False)
    except subprocess.CalledProcessError as exc:
        message = "Failed while creating/pushing submission branch."
        raise GitMRFlowError(message) from exc

    gh_cmd = build_pr_create_command(branch_name, options)
    pr_result = _run_gh(gh_cmd, repo_root, check=False)
    if pr_result.returncode != 0:
        merged_output = "\n".join([pr_result.stdout or "", pr_result.stderr or ""]).strip()
        message = (
            "Failed to create GitHub PR. "
            "Your branch has been pushed and local main was not reset.\n"
            f"Branch: {branch_name}\n"
            f"gh output:\n{merged_output}"
        )
        raise GitMRFlowError(message)

    merged_output = "\n".join([pr_result.stdout or "", pr_result.stderr or ""]).strip()
    pr_url = extract_pr_url(merged_output)
    if not pr_url:
        message = (
            "PR may have been created, but no PR URL was found in gh output. "
            "Local main was not reset."
        )
        raise GitMRFlowError(message)

    try:
        _run_git(["switch", "main"], repo_root, capture_output=False)
        _run_git(["reset", "--hard", "origin/main"], repo_root, capture_output=False)
    except subprocess.CalledProcessError as exc:
        message = (
            "PR created successfully, but failed to reset local main.\n"
            f"PR: {pr_url}\n"
            "Run manually:\n"
            "  git switch main\n"
            "  git reset --hard origin/main"
        )
        raise GitMRFlowError(message) from exc

    return SubmitResult(branch_name=branch_name, pr_url=pr_url)
