"""Strategy subprocesses must carry the shared inter-service API key (G1).

Without API_KEY in the subprocess env the HttpSignalEmitter sends no X-API-Key
header and every signal POST is rejected 401 by the scoring engine — on any
cloud. These guard the canonical-name resolution + propagation.
"""

from __future__ import annotations

from types import SimpleNamespace

from indicator_runner.process_manager import IndicatorRunner


def _runner(secrets: dict[str, str]) -> IndicatorRunner:
    return IndicatorRunner(category="indicator", deployment_config={}, secrets=secrets)


def _strategy() -> SimpleNamespace:
    return SimpleNamespace(config={"strategy_id": "s1"}, name="s1", run_mode="paper")


def test_api_key_from_secrets_is_propagated(monkeypatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    env = _runner({"api_key": "shared-secret"})._get_process_env(_strategy(), "signal_worker")
    assert env["API_KEY"] == "shared-secret"


def test_manifest_api_key_env_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "from-manifest")
    env = _runner({"api_key": "from-secrets"})._get_process_env(_strategy(), "signal_worker")
    assert env["API_KEY"] == "from-manifest"


def test_retired_signal_api_key_secret_is_not_used(monkeypatch) -> None:
    """Only the canonical ``api_key`` secret is propagated."""
    monkeypatch.delenv("API_KEY", raising=False)
    env = _runner({"signal_api_key": "retired"})._get_process_env(_strategy(), "signal_worker")
    assert env["API_KEY"] == ""
    assert "SIGNAL_API_KEY" not in env


def test_provider_neutral_signal_endpoint_is_propagated(monkeypatch) -> None:
    monkeypatch.delenv("SIGNAL_API_URL", raising=False)
    runner = IndicatorRunner(
        category="indicator",
        deployment_config={"endpoints": {"signal_api_url": "http://scoring-engine:8001"}},
        secrets={},
    )

    env = runner._get_process_env(_strategy(), "signal_worker")

    assert env["SIGNAL_API_URL"] == "http://scoring-engine:8001"


def test_subprocess_credentials_and_endpoint_use_startup_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("SIGNAL_API_URL", "http://scoring-before-start:8001")
    monkeypatch.setenv("API_KEY", "key-before-start")
    runner = _runner({"api_key": "loaded-secret"})

    monkeypatch.setenv("SIGNAL_API_URL", "http://scoring-after-start:8001")
    monkeypatch.setenv("API_KEY", "key-after-start")
    env = runner._get_process_env(_strategy(), "signal_worker")

    assert env["SIGNAL_API_URL"] == "http://scoring-before-start:8001"
    assert env["API_KEY"] == "key-before-start"
