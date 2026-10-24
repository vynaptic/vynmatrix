"""Load the packaged evidence a paper strategy release relies on.

The readiness label is evidence only because it is reviewed, so it is read from a
``config.json`` that Git tracks and that is unchanged from ``HEAD``. The strategy
directory's Git tree at ``HEAD`` is returned so the caller can compare it with
the tree the running image was built from.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dev_cli.core.catalogue import load_strategy_releases
from lib_application.services.catalogue import StrategyRelease
from lib_common.runner_utils import build_strategy_core_parameters

_STRATEGY_ROOT = "strategies/indicator"


@dataclass(frozen=True)
class PackagedRelease:
    """One exact packaged release and the committed source facts behind it."""

    release: StrategyRelease
    readiness: str
    decision: str
    source_commit: str
    config_sha256: str
    strategy_tree: str


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def _git_output(root: Path, *args: str) -> str:
    result = _git(root, *args)
    if result.returncode:
        msg = f"git {args[0]} failed; a release needs a Git checkout of the source"
        raise ValueError(msg)
    return result.stdout.strip()


def strategy_tree_at(root: Path, commit: str, strategy_name: str) -> str | None:
    """Return the Git tree ID of one strategy directory at ``commit``, if it exists."""
    result = _git(
        root, "rev-parse", "--verify", "--quiet", f"{commit}:{_STRATEGY_ROOT}/{strategy_name}"
    )
    tree = result.stdout.strip()
    return tree if result.returncode == 0 and tree else None


def load_packaged_release(root: Path, *, strategy_id: str, version: str) -> PackagedRelease:
    """Validate the exact packaged release and the committed label it carries."""
    releases = load_strategy_releases(root, strategy_id=strategy_id)
    if len(releases) != 1 or releases[0].semver != version:
        msg = "Release requires the exact unambiguous packaged strategy version"
        raise ValueError(msg)
    release = releases[0]
    relative = f"{_STRATEGY_ROOT}/{release.strategy_name}/config.json"
    if _git(root, "ls-files", "--error-unmatch", "--", relative).returncode:
        msg = f"{relative} is not tracked by Git; commit it before releasing"
        raise ValueError(msg)
    if _git(root, "diff", "--quiet", "HEAD", "--", relative).returncode:
        msg = f"{relative} differs from HEAD; commit the reviewed change before releasing"
        raise ValueError(msg)
    # Resolve the commit once and read the label from it, so a concurrent
    # checkout change cannot pair one commit's label with another's tree.
    commit = _git_output(root, "rev-parse", "--verify", "HEAD^{commit}")
    shown = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{relative}"],
        capture_output=True,
        check=False,
    )
    if shown.returncode:
        msg = f"{relative} is not in commit {commit[:12]}"
        raise ValueError(msg)
    payload = shown.stdout
    config = json.loads(payload)
    if (
        config.get("strategy_id") != release.strategy_id
        or config.get("strategy_version") != release.semver
        or build_strategy_core_parameters(config) != release.default_params
    ):
        msg = "Strategy source changed during validation; retry from stable packaged source"
        raise ValueError(msg)
    metadata = config.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    tree = strategy_tree_at(root, commit, release.strategy_name)
    if tree is None:
        msg = f"{release.strategy_name} is not in commit {commit[:12]}"
        raise ValueError(msg)
    return PackagedRelease(
        release=release,
        readiness=str(metadata.get("readiness") or ""),
        decision=str(metadata.get("decision") or ""),
        source_commit=commit,
        config_sha256=hashlib.sha256(payload).hexdigest(),
        strategy_tree=tree,
    )


__all__ = ["PackagedRelease", "load_packaged_release", "strategy_tree_at"]
