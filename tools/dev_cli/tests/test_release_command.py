"""Release-tag provenance guard tests."""

from __future__ import annotations

import importlib

import pytest
from click.testing import CliRunner

release_module = importlib.import_module("dev_cli.commands.release")


def test_normalize_rejects_semver_leading_zero() -> None:
    result = CliRunner().invoke(release_module.release, ["normalize", "v01.2.3"])

    assert result.exit_code == 1
    assert "Cannot normalize" in result.output


def test_tag_rejects_commit_that_is_not_main_tip(monkeypatch) -> None:
    calls: list[list[str]] = []
    ref_sha = "0123456789abcdef0123456789abcdef01234567"
    main_sha = "fedcba9876543210fedcba9876543210fedcba98"

    def _run_git(args: list[str]) -> str:
        calls.append(args)
        if args[0] == "rev-parse":
            return main_sha if args[-1] == "main^{commit}" else ref_sha
        message = f"unexpected git call: {args}"
        raise AssertionError(message)

    monkeypatch.setattr(release_module, "_run_git", _run_git)

    result = CliRunner().invoke(release_module.release, ["tag", "1.2.3"])

    assert result.exit_code == 1
    assert "not local main tip" in result.output
    assert all(call[0] != "tag" for call in calls)


def test_tag_uses_resolved_main_commit(monkeypatch) -> None:
    commit_sha = "0123456789abcdef0123456789abcdef01234567"
    calls: list[list[str]] = []

    def _run_git(args: list[str]) -> str:
        calls.append(args)
        if args[0] == "rev-parse":
            return commit_sha
        if args[0] == "tag":
            return ""
        message = f"unexpected git call: {args}"
        raise AssertionError(message)

    monkeypatch.setattr(release_module, "_run_git", _run_git)
    monkeypatch.setattr(
        release_module,
        "_require_changelog_release",
        lambda _version: None,
    )

    result = CliRunner().invoke(release_module.release, ["tag", "V1_2p3"])

    assert result.exit_code == 0, result.output
    assert ["rev-parse", "--verify", "main^{commit}"] in calls
    assert ["tag", "-a", "v1.2.3", commit_sha, "-m", "v1.2.3"] in calls


def test_changelog_release_requires_exact_dated_section(
    monkeypatch,
    tmp_path,
) -> None:
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-07-25\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_module, "get_project_root", lambda: tmp_path)

    assert release_module._require_changelog_release("v1.2.3") == (tmp_path / "CHANGELOG.md")


def test_changelog_release_rejects_missing_version(monkeypatch, tmp_path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_module, "get_project_root", lambda: tmp_path)

    with pytest.raises(
        release_module.click.ClickException,
        match="exactly one dated release section",
    ):
        release_module._require_changelog_release("v1.2.3")
