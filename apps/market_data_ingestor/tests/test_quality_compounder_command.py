"""Tests for the default-off Quality Compounder one-shot command."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib_infrastructure.market_data import eodhd_client as eodhd_mod
from market_data_ingestor import main as main_mod
from market_data_ingestor import quality_compounder_calendar as calendar_mod
from market_data_ingestor import quality_compounder_producer as producer_mod
from market_data_ingestor import quality_compounder_quarterly as quarterly_mod
from market_data_ingestor import sec_edgar as sec_mod


def test_command_is_a_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUALITY_COMPOUNDER_PANELS_ENABLED", "false")
    monkeypatch.setattr(
        main_mod,
        "create_engine_for_env",
        lambda **_kwargs: pytest.fail("disabled command opened the database"),
    )

    main_mod.run_quality_compounder_once()


def test_enabled_command_requires_explicit_runtime_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUALITY_COMPOUNDER_PANELS_ENABLED", "true")
    for name in (
        "EODHD_API_TOKEN",
        "EDGAR_USER_AGENT",
        "QUALITY_COMPOUNDER_ENTITLEMENT_OWNER_USER_ID",
        "QUALITY_COMPOUNDER_ROUND_TRIP_COMMISSION_BPS",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="EODHD_API_TOKEN is required"):
        main_mod.run_quality_compounder_once()


def test_enabled_command_builds_one_frozen_job_and_closes_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUALITY_COMPOUNDER_PANELS_ENABLED", "true")
    monkeypatch.setenv("EODHD_API_TOKEN", "secret-token")
    monkeypatch.setenv("EDGAR_USER_AGENT", "vynmatrix ops@example.com")
    monkeypatch.setenv("QUALITY_COMPOUNDER_ENTITLEMENT_OWNER_USER_ID", "owner-1")
    monkeypatch.setenv("QUALITY_COMPOUNDER_ROUND_TRIP_COMMISSION_BPS", "1.25")
    events: list[str] = []
    captured: dict[str, object] = {}

    class _EODHD:
        def __init__(self, token: str) -> None:
            assert token == "secret-token"

        def close(self) -> None:
            events.append("eodhd_closed")

    class _Sec:
        def __init__(self, config: object) -> None:
            captured["sec_config"] = config

        def close(self) -> None:
            events.append("sec_closed")

    class _Producer:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class _Job:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["enabled"] is True
            assert isinstance(kwargs["producer"], _Producer)

        def run_once(self) -> object:
            return SimpleNamespace(
                status=SimpleNamespace(value="not_quarter_end"),
                decision_session=date(2026, 6, 29),
                input_sha256=None,
            )

    engine = object()
    session_factory = object()
    monkeypatch.setattr(eodhd_mod, "EODHDClient", _EODHD)
    monkeypatch.setattr(sec_mod, "SecEdgarClient", _Sec)
    monkeypatch.setattr(producer_mod, "QualityCompounderPanelProducer", _Producer)
    monkeypatch.setattr(quarterly_mod, "QualityCompounderQuarterlyJob", _Job)
    monkeypatch.setattr(main_mod, "build_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(main_mod, "create_engine_for_env", lambda **_kwargs: engine)
    monkeypatch.setattr(
        main_mod,
        "get_session_factory",
        lambda **_kwargs: session_factory,
    )
    monkeypatch.setattr(
        main_mod,
        "dispose_engine",
        lambda value: events.append("engine_disposed") if value is engine else None,
    )

    main_mod.run_quality_compounder_once()

    market_policy = captured["market_policy"]
    cost_policy = captured["cost_policy"]
    assert market_policy.round_trip_commission_bps == 1.25
    assert market_policy.cost_context_sha256 == cost_policy.configuration_sha256
    assert captured["entitlement_owner_user_id"] == "owner-1"
    assert events == ["sec_closed", "eodhd_closed", "engine_disposed"]


def test_calendar_import_persists_one_pinned_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "xnys.json"
    path.write_bytes(b"pinned")
    monkeypatch.setenv("QUALITY_COMPOUNDER_OFFICIAL_SESSION_ARTIFACT", str(path))
    monkeypatch.setenv("QUALITY_COMPOUNDER_OFFICIAL_SESSION_SHA256", "a" * 64)
    artifact = SimpleNamespace(
        content_sha256="a" * 64,
        coverage_from=date(2025, 1, 1),
        coverage_to=date(2026, 12, 31),
    )
    events: list[str] = []

    class _Transaction:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def begin(self) -> _Transaction:
            return _Transaction()

    monkeypatch.setattr(
        calendar_mod,
        "load_quality_compounder_calendar_artifact",
        lambda content, *, expected_sha256: (
            artifact
            if content == b"pinned" and expected_sha256 == "a" * 64
            else pytest.fail("calendar import lost pinned bytes or digest")
        ),
    )
    monkeypatch.setattr(
        calendar_mod,
        "persist_quality_compounder_calendar",
        lambda _session, value: (
            SimpleNamespace(calendar_id=7)
            if value is artifact
            else pytest.fail("calendar import persisted another artifact")
        ),
    )
    engine = object()
    monkeypatch.setattr(main_mod, "build_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(main_mod, "create_engine_for_env", lambda **_kwargs: engine)
    monkeypatch.setattr(main_mod, "get_session_factory", lambda **_kwargs: _Session)
    monkeypatch.setattr(
        main_mod,
        "dispose_engine",
        lambda value: events.append("engine_disposed") if value is engine else None,
    )

    main_mod.run_quality_compounder_calendar_import()

    assert events == ["engine_disposed"]
