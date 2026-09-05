"""Command boundary for the reviewed equity catalogue import."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_data_ingestor import equity_catalogue_import as catalogue_mod
from market_data_ingestor import main as main_mod


@pytest.mark.parametrize(
    ("configured_dry_run", "expected_dry_run"),
    [(None, True), ("false", False)],
)
def test_equity_catalogue_command_is_pinned_and_defaults_to_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_dry_run: str | None,
    expected_dry_run: bool,
) -> None:
    path = tmp_path / "equities.json"
    path.write_bytes(b"reviewed")
    monkeypatch.setenv("EQUITY_CATALOGUE_ARTIFACT", str(path))
    monkeypatch.setenv("EQUITY_CATALOGUE_SHA256", "a" * 64)
    if configured_dry_run is None:
        monkeypatch.delenv("EQUITY_CATALOGUE_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("EQUITY_CATALOGUE_DRY_RUN", configured_dry_run)
    artifact = SimpleNamespace(
        content_sha256="a" * 64,
        instruments=(object(),),
        reviewed_at=datetime(2026, 8, 16, tzinfo=UTC),
        reviewer="portfolio-operations",
        source_reference="reviewed contract export",
    )
    observed: list[object] = []

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
        catalogue_mod,
        "load_reviewed_equity_catalogue_artifact",
        lambda content, *, expected_sha256: (
            artifact
            if content == b"reviewed" and expected_sha256 == "a" * 64
            else pytest.fail("command lost the pinned catalogue bytes or digest")
        ),
    )

    def _apply(session: object, value: object, *, dry_run: bool) -> object:
        observed.extend((session, value, dry_run))
        return SimpleNamespace(
            content_sha256="a" * 64,
            dry_run=dry_run,
            instruments_created=("AAPL",),
            instruments_completed=(),
            broker_mappings_created=("AAPL",),
            exact_replays=(),
        )

    monkeypatch.setattr(catalogue_mod, "apply_reviewed_equity_catalogue", _apply)
    engine = object()
    monkeypatch.setattr(main_mod, "build_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(main_mod, "create_engine_for_env", lambda **_kwargs: engine)
    monkeypatch.setattr(main_mod, "get_session_factory", lambda **_kwargs: _Session)
    monkeypatch.setattr(
        main_mod,
        "dispose_engine",
        lambda value: observed.append("disposed") if value is engine else None,
    )
    monkeypatch.setattr(main_mod, "setup_logging", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["market-data-ingestor", "equity-catalogue-import"])

    main_mod.main()

    assert isinstance(observed[0], _Session)
    assert observed[1:] == [artifact, expected_dry_run, "disposed"]
