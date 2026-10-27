"""Freeze trusted coordinator/engine sources and attest their host environment."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Any

_ROOTS = (
    "tools/dev_cli",
    "libs/python/lib_common",
    "libs/python/lib_data",
    "libs/python/lib_indicators",
    "libs/python/lib_strategy",
    "libs/python/lib_application",
    "libs/python/lib_infrastructure",
)
_RUNTIME_SUFFIXES = {".py", ".so", ".pyd", ".dll", ".dylib"}


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment_fingerprint() -> dict[str, Any]:
    """Hash actual installed dependency code, not merely its version/RECORD claim.

    Editable repository libraries are copied into the bundle. All other installed
    Python/extension code remains in the host tooling environment and is hashed
    here, including transitive dependencies and editable-import bootstraps.
    """
    distributions = {}
    installed = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib")}
    for distribution in importlib.metadata.distributions(path=sorted(installed)):
        name = distribution.metadata["Name"] or ""
        files = {}
        for entry in distribution.files or ():
            path = Path(str(distribution.locate_file(entry)))
            if (path.suffix in _RUNTIME_SUFFIXES or path.suffix == ".pth") and path.is_file():
                files[str(entry)] = _hash(path)
        distributions[name.lower()] = {
            "version": distribution.version,
            "files_hash": hashlib.sha256(_json(files)).hexdigest(),
        }
    return {
        "executable": str(Path(sys.executable).absolute()),
        "interpreter_hash": _hash(Path(sys.executable).resolve()),
        "version": sys.version,
        "implementation": sys.implementation.name,
        "dependencies": distributions,
    }


def freeze_bundle(root: Path, parent: Path) -> tuple[Path, str]:
    """Copy only trusted package files; never include environment, datasets or code uploads."""
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix="preparing-", dir=parent))
    files = {}
    for relative in _ROOTS:
        source_root = root / relative
        if not source_root.exists():
            continue
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or source.suffix not in {".py", ".json", ".yaml", ".yml"}:
                continue
            if {"tests", "__pycache__", "build", "dist"} & set(
                source.relative_to(source_root).parts
            ):
                continue
            if source.is_symlink():
                msg = "Trusted bundle source must not contain symlinks"
                raise ValueError(msg)
            name = source.relative_to(root).as_posix()
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            files[name] = _hash(target)
            target.chmod(0o400)
    if not files:
        msg = "No trusted research package sources found"
        raise ValueError(msg)
    manifest = {
        "schema": 1,
        "files": files,
        "environment": environment_fingerprint(),
        "import_roots": [name for name in _ROOTS if (staging / name).is_dir()],
    }
    raw = _json(manifest)
    digest = hashlib.sha256(raw).hexdigest()
    (staging / "manifest.json").write_bytes(raw)
    (staging / "manifest.json").chmod(0o400)
    target = parent / digest
    if target.exists():
        verify_bundle(target, digest)
        # Only this invocation's exact private staging directory is disposable.
        shutil.rmtree(staging)
    else:
        staging.rename(target)
    return target, digest


def verify_bundle(path: Path, expected_hash: str) -> dict[str, Any]:
    raw = (path / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        msg = "Frozen research bundle manifest identity mismatch"
        raise ValueError(msg)
    manifest = json.loads(raw)
    if (
        set(manifest) != {"schema", "files", "environment", "import_roots"}
        or manifest["schema"] != 1
    ):
        msg = "Frozen research bundle schema mismatch"
        raise ValueError(msg)
    for name, digest in manifest["files"].items():
        relative = Path(name)
        target = path / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or target.is_symlink()
            or _hash(target) != digest
        ):
            msg = "Frozen research bundle code identity mismatch"
            raise ValueError(msg)
    actual = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    if actual != set(manifest["files"]) | {"manifest.json"}:
        msg = "Frozen research bundle has unexpected files"
        raise ValueError(msg)
    if environment_fingerprint() != manifest["environment"]:
        msg = "Trusted interpreter/dependency identity changed"
        raise ValueError(msg)
    return dict(manifest)


# Verify every code byte with stdlib before importing any frozen package code.
# -I prevents checkout/PYTHONPATH injection; the only added roots are verified.
_BOOTSTRAP = """
import hashlib, json, pathlib, runpy, sys
sys.dont_write_bytecode = True
bundle = pathlib.Path(sys.argv[1]).resolve()
expected = sys.argv[2]
raw = (bundle / 'manifest.json').read_bytes()
if hashlib.sha256(raw).hexdigest() != expected:
    raise ValueError('Frozen research manifest identity mismatch')
manifest = json.loads(raw)
for name, digest in manifest['files'].items():
    relative = pathlib.Path(name)
    source = bundle / relative
    invalid = relative.is_absolute() or '..' in relative.parts or source.is_symlink()
    if invalid or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError('Frozen research source identity mismatch')
namespace = runpy.run_path(str(bundle / 'tools/dev_cli/dev_cli/core/research/bundle.py'))
namespace['verify_bundle'](bundle, expected)
for relative in reversed(manifest['import_roots']):
    sys.path.insert(0, str(bundle / relative))
sys.argv = ['research-worker', *sys.argv[3:], '--bundle', str(bundle), '--bundle-hash', expected]
runpy.run_module('dev_cli.core.research.worker', run_name='__main__')
"""


def launch_bundle(path: Path, digest: str, root: Path, *, once: bool = False) -> int:
    verify_bundle(path, digest)
    command = [sys.executable, "-I", "-B", "-c", _BOOTSTRAP, str(path), digest, "--root", str(root)]
    if once:
        command.append("--once")
    # The supervisor is a foreground, bounded process owned by this command.
    result = subprocess.run(
        command,
        check=False,
        env={
            **os.environ,
            "EXECUTION_MODE": "paper",
            "EXECUTION_ENGINE_ALLOW_LIVE": "false",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        },
    )
    return result.returncode
