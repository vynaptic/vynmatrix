"""Tests for ``vmdev audit``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_cli.commands import audit as audit_module


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Build a minimal fake repo and ``git init`` so audit checks have files."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    (tmp_path / "libs" / "python" / "lib_strategy" / "lib_strategy" / "signals").mkdir(
        parents=True, exist_ok=True
    )
    canonical = (
        tmp_path / "libs" / "python" / "lib_strategy" / "lib_strategy" / "signals" / "signal.py"
    )
    canonical.write_text("class Signal: ...\nclass SignalAction: ...\n", encoding="utf-8")
    (tmp_path / "libs" / "python" / "lib_application" / "lib_application" / "db").mkdir(
        parents=True, exist_ok=True
    )
    (
        tmp_path / "libs" / "python" / "lib_application" / "lib_application" / "db" / "session.py"
    ).write_text(
        "from sqlalchemy.orm import sessionmaker\nSessionLocal = sessionmaker()\n",
        encoding="utf-8",
    )
    return tmp_path


def _git_add_all(root: Path) -> None:
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _findings_by_rule(findings: list[audit_module.Finding]) -> dict:
    out: dict = {}
    for f in findings:
        out.setdefault(f.rule, []).append(f)
    return out


def test_clean_repo_passes(fake_repo: Path) -> None:
    _git_add_all(fake_repo)
    report = audit_module.run_audit(staged=False, root=fake_repo)
    assert not report.errors, f"expected no errors, got {report.errors}"


def test_default_audit_includes_non_ignored_untracked_files(fake_repo: Path) -> None:
    _git_add_all(fake_repo)
    rogue = fake_repo / "apps" / "rogue" / "signal.py"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("class SignalAction:\n    LONG = 'LONG'\n", encoding="utf-8")

    report = audit_module.run_audit(staged=False, root=fake_repo)

    assert rogue in audit_module._all_worktree_files(fake_repo)
    assert any(
        finding.rule == "duplicate-signal-type" and finding.path == "apps/rogue/signal.py"
        for finding in report.errors
    )


def test_staged_audit_remains_index_only(fake_repo: Path) -> None:
    _git_add_all(fake_repo)
    rogue = fake_repo / "apps" / "rogue" / "signal.py"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("class SignalAction:\n    LONG = 'LONG'\n", encoding="utf-8")

    report = audit_module.run_audit(staged=True, root=fake_repo)

    assert rogue not in audit_module._staged_files(fake_repo)
    assert not any(finding.path == "apps/rogue/signal.py" for finding in report.findings)


def test_duplicate_signal_action_flagged(fake_repo: Path) -> None:
    """Defining ``SignalAction`` outside the canonical file is an error."""
    rogue = fake_repo / "apps" / "rogue" / "signal.py"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("class SignalAction:\n    LONG = 'LONG'\n", encoding="utf-8")
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    by_rule = _findings_by_rule(report.errors)
    assert "duplicate-signal-type" in by_rule
    paths = [f.path for f in by_rule["duplicate-signal-type"]]
    assert any("apps/rogue/signal.py" in p for p in paths)


def test_session_drift_creates_warning(fake_repo: Path) -> None:
    """Direct ``create_engine`` outside the canonical helper is a warning."""
    drift = fake_repo / "apps" / "myapp" / "main.py"
    drift.parent.mkdir(parents=True, exist_ok=True)
    drift.write_text(
        "from sqlalchemy import create_engine\nengine = create_engine('sqlite://')\n",
        encoding="utf-8",
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    by_rule = _findings_by_rule(report.warnings)
    assert "session-drift-create-engine" in by_rule


def test_logger_canonical_drift_creates_warning(fake_repo: Path) -> None:
    """Direct ``logging.getLogger`` outside the canonical helper is a warning."""
    drift = fake_repo / "apps" / "myapp" / "module.py"
    drift.parent.mkdir(parents=True, exist_ok=True)
    drift.write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n",
        encoding="utf-8",
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    by_rule = _findings_by_rule(report.warnings)
    assert "logger-canonical-drift" in by_rule, (
        "Direct ``logging.getLogger`` in apps/ should fire the canonical drift "
        f"warning. Got warnings: {[f.rule for f in report.warnings]}"
    )


def test_logger_canonical_drift_allowlists_scripts_and_tools_reconciliation(
    fake_repo: Path,
) -> None:
    """Operational CLIs under ``scripts/`` / ``tools/reconciliation/`` are
    legitimately exempt because they don't run inside an app process."""
    script = fake_repo / "scripts" / "one_shot_tool.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n",
        encoding="utf-8",
    )
    recon = fake_repo / "tools" / "reconciliation" / "harness.py"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n",
        encoding="utf-8",
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    drift_paths = {f.path for f in report.warnings if f.rule == "logger-canonical-drift"}
    assert "scripts/one_shot_tool.py" not in drift_paths
    assert "tools/reconciliation/harness.py" not in drift_paths


def test_indicator_signal_only_violation_is_error(fake_repo: Path) -> None:
    """Direct ``self.Buy(...)`` in an indicator strategy is an error."""
    bad = fake_repo / "strategies" / "indicator" / "Bad" / "main.py"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "class S:\n    def OnData(self):\n        self.Buy('BTCUSD', 1)\n",
        encoding="utf-8",
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    by_rule = _findings_by_rule(report.errors)
    assert "indicator-signal-only" in by_rule


def test_hand_rolled_env_parsing_is_error(fake_repo: Path) -> None:
    drift = fake_repo / "apps" / "myapp" / "config.py"
    drift.parent.mkdir(parents=True, exist_ok=True)
    drift.write_text(
        "import os\n"
        "port = int(os.getenv('PORT', '8000'))\n"
        "enabled = os.environ.get('ENABLED', 'false').strip().lower() in {'1', 'true'}\n",
        encoding="utf-8",
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    findings = [finding for finding in report.errors if finding.rule == "env-parse-drift"]
    assert len(findings) == 2
    assert {finding.extra["line"] for finding in findings} == {2, 3}


def test_non_boolean_env_enum_comparison_is_allowed(fake_repo: Path) -> None:
    config = fake_repo / "apps" / "myapp" / "config.py"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "import os\nmode = os.getenv('MODE', 'paper').strip().lower() == 'live'\n",
        encoding="utf-8",
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    assert not [finding for finding in report.errors if finding.rule == "env-parse-drift"]


def test_indirect_and_mapping_env_parsing_is_error(fake_repo: Path) -> None:
    drift = fake_repo / "libs" / "python" / "lib_x" / "config.py"
    drift.parent.mkdir(parents=True, exist_ok=True)
    drift.write_text(
        "import os\n"
        "def from_mapping(env=None):\n"
        "    values = env if env is not None else os.environ\n"
        "    return int(values.get('PORT', '8000'))\n"
        "def from_local():\n"
        "    value = os.environ.get('ENABLED', 'false')\n"
        "    return value.strip().lower() in {'1', 'true'}\n",
        encoding="utf-8",
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    findings = [finding for finding in report.errors if finding.rule == "env-parse-drift"]
    assert len(findings) == 2
    assert {finding.extra["line"] for finding in findings} == {4, 7}


def test_forbidden_tracked_artefact(fake_repo: Path) -> None:
    """Tracked ``build/`` files are an error."""
    artefact = fake_repo / "libs" / "python" / "lib_x" / "build" / "lib_x.whl"
    artefact.parent.mkdir(parents=True, exist_ok=True)
    artefact.write_text("", encoding="utf-8")
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    by_rule = _findings_by_rule(report.errors)
    assert "forbidden-tracked" in by_rule


def test_loc_cap_flags_giant_file(fake_repo: Path) -> None:
    big = fake_repo / "libs" / "python" / "lib_x" / "module.py"
    big.parent.mkdir(parents=True, exist_ok=True)
    body = "x = 0\n" * (audit_module.PRODUCTION_FILE_MAX_LOC + 50)
    big.write_text(body, encoding="utf-8")
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    by_rule = _findings_by_rule(report.errors)
    assert "file-loc-cap" in by_rule


def test_migration_exemption_downgrades_loc_error_to_warning(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files in ``MIGRATION_EXEMPTIONS`` produce warnings, not errors.

    The mechanism is exercised by injecting a synthetic in-flight refactor;
    the production exemption table is intentionally empty post-Phase-3.D.
    """
    big = (
        fake_repo
        / "libs"
        / "python"
        / "lib_application"
        / "lib_application"
        / "db"
        / "models"
        / "__init__.py"
    )
    big.parent.mkdir(parents=True, exist_ok=True)
    body = "x = 0\n" * (audit_module.MODELS_FILE_MAX_LOC + 50)
    big.write_text(body, encoding="utf-8")
    _git_add_all(fake_repo)

    monkeypatch.setitem(
        audit_module.MIGRATION_EXEMPTIONS,
        "libs/python/lib_application/lib_application/db/models/__init__.py",
        "Synthetic in-flight refactor (test only).",
    )

    report = audit_module.run_audit(staged=False, root=fake_repo)
    error_rules = {f.rule for f in report.errors}
    warning_rules = {f.rule for f in report.warnings}
    assert "file-loc-cap" not in error_rules
    assert "file-loc-cap" in warning_rules


def _write_build_yaml(root: Path, python_version: str = "3.11.13") -> None:
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "build.yaml").write_text(
        f'global:\n  python_version: "{python_version}"\n',
        encoding="utf-8",
    )


def test_docker_python_base_drift_flagged(fake_repo: Path) -> None:
    """A Dockerfile python base that disagrees with build.yaml is an error."""
    _write_build_yaml(fake_repo, "3.11.13")
    docker = fake_repo / "docker"
    docker.mkdir(parents=True, exist_ok=True)
    (docker / "platform_runtime.Dockerfile").write_text(
        "FROM python:3.14-slim\nRUN true\n", encoding="utf-8"
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    by_rule = _findings_by_rule(report.errors)
    assert "docker-python-base-drift" in by_rule
    assert by_rule["docker-python-base-drift"][0].extra == {
        "found": "3.14",
        "required": "3.11",
    }


def test_docker_python_base_match_arg_and_nonpython_pass(fake_repo: Path) -> None:
    """Direct match, ARG-resolved match, and non-python bases produce no drift."""
    _write_build_yaml(fake_repo, "3.11.13")
    docker = fake_repo / "docker"
    (docker / "base").mkdir(parents=True, exist_ok=True)
    (docker / "platform_runtime.Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (docker / "base" / "Dockerfile").write_text(
        "ARG PYTHON_VERSION=3.11\nFROM python:${PYTHON_VERSION}-slim\n", encoding="utf-8"
    )
    (docker / "lean.Dockerfile").write_text(
        "ARG LEAN_TAG=latest\nFROM quantconnect/lean:${LEAN_TAG}\n", encoding="utf-8"
    )
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    drift = [f for f in report.findings if f.rule == "docker-python-base-drift"]
    assert not drift, f"unexpected drift findings: {[f.path for f in drift]}"


def _write_runtime_dependency_contract(root: Path) -> None:
    constraints = root / audit_module.RUNTIME_CONSTRAINTS_PATH
    constraints.parent.mkdir(parents=True, exist_ok=True)
    constraints.write_text(
        "fastapi==0.110.0\nalembic==1.18.3\npsutil==7.2.2\n",
        encoding="utf-8",
    )
    profiles = {
        audit_module.RUNTIME_REQUIREMENTS_PATHS[0]: "fastapi==0.110.0\n",
        audit_module.RUNTIME_REQUIREMENTS_PATHS[1]: "alembic==1.18.3\npsutil==7.2.2\n",
    }
    for relative, content in profiles.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_runtime_dependency_contract_accepts_exact_nonduplicated_pins(
    fake_repo: Path,
) -> None:
    _write_runtime_dependency_contract(fake_repo)
    report = audit_module.AuditReport()

    audit_module.check_runtime_dependency_contract(fake_repo, report)

    assert not report.findings


def test_runtime_dependency_contract_rejects_service_base_duplication(
    fake_repo: Path,
) -> None:
    _write_runtime_dependency_contract(fake_repo)
    execution = fake_repo / audit_module.RUNTIME_REQUIREMENTS_PATHS[1]
    execution.write_text("fastapi==0.110.0\n", encoding="utf-8")
    report = audit_module.AuditReport()

    audit_module.check_runtime_dependency_contract(fake_repo, report)

    assert any("duplicates svc-base" in finding.message for finding in report.errors)


def test_runtime_dependency_contract_rejects_duplicate_pin_in_one_profile(
    fake_repo: Path,
) -> None:
    _write_runtime_dependency_contract(fake_repo)
    execution = fake_repo / audit_module.RUNTIME_REQUIREMENTS_PATHS[1]
    execution.write_text("alembic==1.18.3\nalembic==1.18.3\n", encoding="utf-8")
    report = audit_module.AuditReport()

    audit_module.check_runtime_dependency_contract(fake_repo, report)

    assert any("duplicate requirement: alembic" in finding.message for finding in report.errors)


def test_runtime_dependency_contract_rejects_heavy_optional_runtime_package(
    fake_repo: Path,
) -> None:
    _write_runtime_dependency_contract(fake_repo)
    constraints = fake_repo / audit_module.RUNTIME_CONSTRAINTS_PATH
    constraints.write_text(
        constraints.read_text(encoding="utf-8") + "pyarrow==24.0.0\n",
        encoding="utf-8",
    )
    market = fake_repo / audit_module.RUNTIME_REQUIREMENTS_PATHS[1]
    market.write_text("pyarrow==24.0.0\n", encoding="utf-8")
    report = audit_module.AuditReport()

    audit_module.check_runtime_dependency_contract(fake_repo, report)

    assert any("cannot enter" in finding.message for finding in report.errors)


def test_runtime_dependency_contract_rejects_heavy_mandatory_wheel_dependency(
    fake_repo: Path,
) -> None:
    _write_runtime_dependency_contract(fake_repo)
    setup = fake_repo / "libs" / "python" / "lib_data" / "setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text(
        "from setuptools import setup\n"
        'runtime = ["pandas>=2", "pyarrow>=20"]\n'
        'provider = ["hypothetical-provider-sdk>=2"]\n'
        "setup(install_requires=runtime, extras_require={'provider': provider})\n",
        encoding="utf-8",
    )
    report = audit_module.AuditReport()

    audit_module.check_runtime_dependency_contract(fake_repo, report)

    assert any(
        finding.path.endswith("lib_data/setup.py")
        and "mandatory wheel dependencies" in finding.message
        and "pyarrow" in finding.message
        and "hypothetical-provider-sdk" not in finding.message
        for finding in report.errors
    )


def test_runtime_dependency_contract_rejects_dynamic_install_requires(
    fake_repo: Path,
) -> None:
    _write_runtime_dependency_contract(fake_repo)
    setup = fake_repo / "apps" / "service" / "setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text(
        "from setuptools import setup\n"
        "def requirements():\n"
        "    return ['fastapi']\n"
        "setup(install_requires=requirements())\n",
        encoding="utf-8",
    )
    report = audit_module.AuditReport()

    audit_module.check_runtime_dependency_contract(fake_repo, report)

    assert any(
        finding.path.endswith("service/setup.py")
        and "must be a static string list" in finding.message
        for finding in report.errors
    )


def test_baseline_constants_are_documented() -> None:
    """Catch silent baseline bumps. If you raise these, update the comment too."""
    assert audit_module.PRODUCTION_FILE_MAX_LOC == 1_800
    assert audit_module.TEST_FILE_MAX_LOC == 2_500
    assert audit_module.TOOLS_FILE_MAX_LOC == 1_800
    assert audit_module.MODELS_FILE_MAX_LOC == 600
    assert audit_module.BARE_EXCEPT_BASELINE == 43


def test_broad_except_scanner_counts_alias_and_tuple_handlers(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ratchet must not miss ``as exc`` or tuple spelling variants."""
    module = fake_repo / "apps" / "service" / "worker.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "try:\n"
        "    run()\n"
        "except Exception as exc:\n"
        "    handle(exc)\n"
        "try:\n"
        "    run()\n"
        "except (ValueError, Exception):\n"
        "    handle()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_module, "BARE_EXCEPT_BASELINE", 1)

    report = audit_module.AuditReport()
    audit_module.check_bare_except_baseline(fake_repo, report)

    finding = next(item for item in report.errors if item.rule == "bare-except-baseline")
    assert finding.extra == {"count": 2, "baseline": 1}


def _copy_broker_capability_contract(root: Path) -> None:
    source_root = audit_module._repo_root()
    for relative in (
        audit_module.BROKER_CAPABILITIES_PATH,
        audit_module.BROKER_SEED_PATH,
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            (source_root / relative).read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def test_broker_capability_catalogue_matches_runtime_matrix() -> None:
    report = audit_module.AuditReport()

    audit_module.check_broker_capability_catalogue(audit_module._repo_root(), report)

    assert not report.findings


def test_broker_capability_catalogue_drift_is_error(fake_repo: Path) -> None:
    _copy_broker_capability_contract(fake_repo)
    seed_path = fake_repo / audit_module.BROKER_SEED_PATH
    seed_path.write_text(
        seed_path.read_text(encoding="utf-8").replace(
            '"features": ["spot"]',
            '"features": ["spot", "margin"]',
            1,
        ),
        encoding="utf-8",
    )
    report = audit_module.AuditReport()

    audit_module.check_broker_capability_catalogue(fake_repo, report)

    finding = next(
        item for item in report.errors if item.rule == "broker-capability-catalogue-drift"
    )
    assert finding.extra == {"broker": "coinbase"}


def test_broad_except_scanner_fails_closed_on_invalid_syntax(fake_repo: Path) -> None:
    module = fake_repo / "apps" / "service" / "worker.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("try:\n    run()\nexcept Exception as:\n    pass\n", encoding="utf-8")

    report = audit_module.AuditReport()
    audit_module.check_bare_except_baseline(fake_repo, report)

    assert any(item.rule == "broad-except-scan-syntax" for item in report.errors)


def test_canonical_signal_path_is_real() -> None:
    """The canonical signal-types path the audit defends must actually exist."""
    repo_root = audit_module._repo_root()
    canonical = repo_root / audit_module.CANONICAL_SIGNAL_TYPE_PATH
    assert canonical.is_file(), (
        "Canonical Signal/SignalAction file moved — update "
        "``CANONICAL_SIGNAL_TYPE_PATH`` in audit.py."
    )


def test_canonical_session_path_is_real() -> None:
    repo_root = audit_module._repo_root()
    canonical = repo_root / audit_module.CANONICAL_SESSION_PATH
    assert canonical.is_file(), (
        "Canonical session helper moved — update ``CANONICAL_SESSION_PATH`` in audit.py."
    )


def test_real_repo_has_no_audit_errors() -> None:
    """Smoke: the live repo's audit baseline must not regress past zero errors."""
    report = audit_module.run_audit(staged=False)
    error_summaries = [f"{f.path}: {f.rule}" for f in report.errors]
    assert not report.errors, (
        "vmdev audit reported errors against the real repo — these block the "
        "pre-commit hook. Investigate or add a documented migration exemption "
        "before committing.\n" + "\n".join(error_summaries)
    )


def test_json_payload_shape() -> None:
    """``--json`` output is parseable and has the documented top-level keys."""
    report = audit_module.run_audit(staged=False)
    payload = {
        "files_scanned": report.files_scanned,
        "errors": [f.__dict__ for f in report.errors],
        "warnings": [f.__dict__ for f in report.warnings],
    }
    encoded = json.dumps(payload, default=str)
    decoded = json.loads(encoded)
    assert "files_scanned" in decoded
    assert "errors" in decoded
    assert "warnings" in decoded


def _write_first_party_component(root: Path, *, declare_setup: bool, declare_build: bool) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    deps = "\n    - lib_bar" if declare_build else " []"
    (root / "config" / "build.yaml").write_text(
        "global:\n"
        '  python_version: "3.11.13"\n'
        "libs:\n"
        "  components:\n"
        "  - name: lib_foo\n"
        "    path: libs/python/lib_foo\n"
        f"    dependencies:{deps}\n",
        encoding="utf-8",
    )
    pkg = root / "libs" / "python" / "lib_foo" / "lib_foo"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "mod.py").write_text("from lib_bar import thing\n", encoding="utf-8")
    requires = '"lib-bar>=0.1.0",' if declare_setup else ""
    (root / "libs" / "python" / "lib_foo" / "setup.py").write_text(
        f"from setuptools import setup\nsetup(name='lib-foo', install_requires=[{requires}])\n",
        encoding="utf-8",
    )


def test_first_party_dependency_contract_flags_undeclared_import(fake_repo: Path) -> None:
    """A lib_* import missing from setup.py AND build.yaml raises two errors."""
    _write_first_party_component(fake_repo, declare_setup=False, declare_build=False)
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    findings = [f for f in report.findings if f.rule == "first-party-dependency-contract"]
    assert {f.path for f in findings} == {"libs/python/lib_foo/setup.py", "config/build.yaml"}
    assert all("lib_bar" in f.message for f in findings)


def test_first_party_dependency_contract_passes_when_declared(fake_repo: Path) -> None:
    """Declared first-party edges (either dash or underscore spelling) pass."""
    _write_first_party_component(fake_repo, declare_setup=True, declare_build=True)
    _git_add_all(fake_repo)

    report = audit_module.run_audit(staged=False, root=fake_repo)
    findings = [f for f in report.findings if f.rule == "first-party-dependency-contract"]
    assert not findings, [f.message for f in findings]
