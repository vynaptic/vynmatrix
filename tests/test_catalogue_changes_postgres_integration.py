"""Opt-in fixed-function catalogue security checks; all fixture writes roll back."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration


@pytest.fixture
def connection() -> Iterator[sa.Connection]:
    url = os.environ.get("CATALOGUE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CATALOGUE_TEST_DATABASE_URL requires isolated PostgreSQL acceptance")
    engine = sa.create_engine(url, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("Catalogue function acceptance requires PostgreSQL")
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(
                sa.text(
                    "SELECT public.vm_catalogue_create_broker('catalogue-change-test', 'Installed', '{}'::jsonb)"
                )
            )
            connection.execute(
                sa.text(
                    "SELECT public.vm_catalogue_create_broker_environment('catalogue-change-test','live','test',CAST(:urls AS jsonb),CAST(:rates AS jsonb))"
                ),
                {
                    "urls": json.dumps({"rest": "https://endpoint.invalid"}),
                    "rates": json.dumps({"requests_per_second": 10}),
                },
            )
            for symbol in ("CATTESTUSD", "OTHERTESTUSD"):
                connection.execute(
                    sa.text(
                        "SELECT public.vm_catalogue_create_instrument(:symbol,'crypto','USD','continuous',true)"
                    ),
                    {"symbol": symbol},
                )
            connection.execute(
                sa.text(
                    "SELECT public.vm_catalogue_create_sector('catalogue-change-test','crypto',NULL)"
                )
            )
            connection.execute(sa.text("SET LOCAL ROLE vm_backend"))
            yield connection
            connection.rollback()
    finally:
        engine.dispose()


def _patch_broker(
    connection: sa.Connection, expected: dict[str, Any], changes: dict[str, Any]
) -> int:
    return int(
        connection.scalar(
            sa.text(
                "SELECT public.vm_catalogue_patch_broker('catalogue-change-test', CAST(:expected AS jsonb), CAST(:changes AS jsonb))"
            ),
            {"expected": json.dumps(expected), "changes": json.dumps(changes)},
        )
    )


def _denied(connection: sa.Connection, sql: str, *, code: str = "42501", **values: Any) -> None:
    with pytest.raises(sa.exc.DBAPIError) as error, connection.begin_nested():
        connection.execute(sa.text(sql), values)
    assert (
        getattr(error.value.orig, "sqlstate", None) or getattr(error.value.orig, "pgcode", None)
    ) == code


def test_fixed_patch_preserves_identity_and_omitted_capabilities(connection: sa.Connection) -> None:
    before = connection.execute(
        sa.text(
            "SELECT broker_id,code,capabilities FROM public.brokers WHERE code='catalogue-change-test'"
        )
    ).one()
    identity = _patch_broker(connection, {"name": "Installed"}, {"name": "Reviewed"})
    assert identity == before.broker_id
    assert _patch_broker(connection, {"name": "Installed"}, {"name": "Reviewed"}) == identity
    after = connection.execute(
        sa.text(
            "SELECT broker_id,code,capabilities FROM public.brokers WHERE code='catalogue-change-test'"
        )
    ).one()
    assert before == after
    _denied(
        connection,
        'SELECT public.vm_catalogue_patch_broker(\'catalogue-change-test\',\'{"name":"Installed"}\'::jsonb,\'{"name":"Overwrite"}\'::jsonb)',
        code="23505",
    )
    _denied(
        connection, "UPDATE public.brokers SET name='bypass' WHERE code='catalogue-change-test'"
    )


@pytest.mark.parametrize(
    "changes",
    [{"code": "renamed"}, {"broker_id": 99}, {"status": "active"}, {"capabilities": None}],
)
def test_fixed_patch_rejects_unsupported_authority_and_types(
    connection: sa.Connection, changes: dict[str, Any]
) -> None:
    expected: dict[str, Any] = {key: {} if key == "capabilities" else None for key in changes}
    _denied(
        connection,
        "SELECT public.vm_catalogue_patch_broker('catalogue-change-test',CAST(:expected AS jsonb),CAST(:changes AS jsonb))",
        code="22023",
        expected=json.dumps(expected),
        changes=json.dumps(changes),
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://name:password@host.invalid",
        "https://host.invalid?token=value",
        "http://host.invalid",
        "https://host.invalid/#fragment",
        "https://host.invalid:0",
        "https://host.invalid:65536",
        "https://host.invalid:invalid",
        "https://host.invalid:-1",
        "https://[::1]:0",
        "https://[::1]:invalid",
    ],
)
def test_fixed_environment_function_rejects_credential_bearing_endpoints(
    connection: sa.Connection, endpoint: str
) -> None:
    _denied(
        connection,
        "SELECT public.vm_catalogue_patch_broker_environment('catalogue-change-test','live','test',CAST(:expected AS jsonb),CAST(:changes AS jsonb))",
        code="22023",
        expected=json.dumps({"base_urls": {"rest": "https://endpoint.invalid"}}),
        changes=json.dumps({"base_urls": {"rest": endpoint}}),
    )


@pytest.mark.parametrize(
    "endpoint",
    ["https://host.invalid:1", "https://host.invalid:65535", "wss://[::1]:9443/ws"],
)
def test_fixed_environment_function_accepts_valid_explicit_ports(
    connection: sa.Connection, endpoint: str
) -> None:
    connection.execute(
        sa.text("""
            SELECT public.vm_catalogue_patch_broker_environment(
                'catalogue-change-test','live','test',
                CAST(:expected AS jsonb),CAST(:changes AS jsonb))
        """),
        {
            "expected": json.dumps({"base_urls": {"rest": "https://endpoint.invalid"}}),
            "changes": json.dumps({"base_urls": {"rest": endpoint}}),
        },
    )
    assert (
        connection.scalar(
            sa.text("""
            SELECT base_urls->>'rest' FROM public.broker_environments e
            JOIN public.brokers b USING(broker_id) WHERE b.code='catalogue-change-test'
        """)
        )
        == endpoint
    )


def test_environment_and_sector_patch_leave_unmentioned_values_unchanged(
    connection: sa.Connection,
) -> None:
    connection.execute(
        sa.text(
            "SELECT public.vm_catalogue_patch_broker_environment('catalogue-change-test','live','test',CAST(:expected AS jsonb),CAST(:changes AS jsonb))"
        ),
        {
            "expected": json.dumps({"rate_limits": {"requests_per_second": 10}}),
            "changes": json.dumps({"rate_limits": {"requests_per_second": 5}}),
        },
    )
    assert (
        connection.scalar(
            sa.text(
                "SELECT base_urls->>'rest' FROM public.broker_environments e JOIN public.brokers b USING(broker_id) WHERE b.code='catalogue-change-test'"
            )
        )
        == "https://endpoint.invalid"
    )
    connection.execute(
        sa.text(
            "SELECT public.vm_catalogue_patch_sector('catalogue-change-test',CAST(:expected AS jsonb),CAST(:changes AS jsonb))"
        ),
        {
            "expected": json.dumps({"description": None}),
            "changes": json.dumps({"description": "Reviewed"}),
        },
    )
    row = connection.execute(
        sa.text(
            "SELECT asset_class,parent_sector_id,description FROM public.sectors WHERE code='catalogue-change-test'"
        )
    ).one()
    assert row == ("crypto", None, "Reviewed")


def test_explicit_alias_and_mapping_registration_are_immutable(connection: sa.Connection) -> None:
    alias_sql = "SELECT public.vm_catalogue_create_alias('CATTESTUSD','CAT-TEST-USD','seed')"
    alias_id = connection.scalar(sa.text(alias_sql))
    assert connection.scalar(sa.text(alias_sql)) == alias_id
    _denied(
        connection,
        "SELECT public.vm_catalogue_create_alias('OTHERTESTUSD','CAT-TEST-USD','seed')",
        code="23505",
    )
    symbol_sql = "SELECT public.vm_catalogue_create_broker_symbol('CATTESTUSD','catalogue-change-test','CAT-USD',:venue,NULL)"
    instrument_id = connection.scalar(sa.text(symbol_sql), {"venue": "exact-product"})
    assert connection.scalar(sa.text(symbol_sql), {"venue": None}) == instrument_id
    assert (
        connection.scalar(
            sa.text(
                "SELECT broker_instrument_id FROM public.instrument_broker_symbols WHERE instr_id=:id"
            ),
            {"id": instrument_id},
        )
        == "exact-product"
    )
    _denied(
        connection,
        "SELECT public.vm_catalogue_create_broker_symbol('CATTESTUSD','catalogue-change-test','different',NULL,NULL)",
        code="23505",
    )
    _denied(
        connection,
        "SELECT public.vm_catalogue_create_broker_symbol('OTHERTESTUSD','catalogue-change-test','OTHER', 'exact-product',NULL)",
        code="23505",
    )


def test_catalogue_functions_share_the_transaction_lock(connection: sa.Connection) -> None:
    _patch_broker(connection, {"name": "Installed"}, {"name": "Reviewed"})
    with connection.engine.connect() as other, other.begin():
        assert other.scalar(sa.text("SELECT pg_try_advisory_xact_lock(18472,1)")) is False


@pytest.mark.parametrize("currency", ["123", "US1", "U$D"])
def test_direct_instrument_registration_requires_alphabetic_currency(
    connection: sa.Connection, currency: str
) -> None:
    _denied(
        connection,
        "SELECT public.vm_catalogue_create_instrument('INVALIDCURRENCY', 'crypto', :currency, 'continuous', true)",
        code="22023",
        currency=currency,
    )


@pytest.mark.parametrize(
    "field",
    [
        "instr_id",
        "canonical",
        "exchange",
        "asset_class",
        "settlement_currency",
        "tick_size",
        "lot_size",
        "is_tradable",
        "market_session_policy",
    ],
)
def test_backend_cannot_update_instrument_identity_or_financial_terms(
    connection: sa.Connection, field: str
) -> None:
    # Even a same-value UPDATE requires the column privilege; this isolates the
    # grant boundary from constraints on particular financial values.
    _denied(
        connection, f"UPDATE public.instruments SET {field}={field} WHERE canonical='CATTESTUSD'"
    )


def test_backend_can_assign_only_an_existing_calendar_to_a_scheduled_instrument(
    connection: sa.Connection,
) -> None:
    instrument_id = connection.scalar(
        sa.text(
            "SELECT public.vm_catalogue_create_instrument('CALTESTUSD','equity','USD','scheduled',true)"
        )
    )
    calendar_id = connection.scalar(
        sa.text("""
            INSERT INTO public.market_calendars(code,source_kind,provider,source_reference)
            VALUES ('CATALOGUE-CALENDAR-TEST','exchange','fixture','https://exchange.invalid/calendar')
            RETURNING calendar_id
        """)
    )
    connection.execute(
        sa.text(
            "UPDATE public.instruments SET market_calendar_id=:calendar WHERE instr_id=:instrument"
        ),
        {"calendar": calendar_id, "instrument": instrument_id},
    )
    assert (
        connection.scalar(
            sa.text("SELECT market_calendar_id FROM public.instruments WHERE instr_id=:instrument"),
            {"instrument": instrument_id},
        )
        == calendar_id
    )


@pytest.mark.parametrize(
    "role", ["vm_execution", "vm_scoring", "vm_feedback", "vm_indicator", "vm_market_data"]
)
def test_other_services_cannot_patch_or_register_instrument_links(
    connection: sa.Connection, role: str
) -> None:
    connection.execute(sa.text(f"SET LOCAL ROLE {role}"))
    _denied(
        connection,
        'SELECT public.vm_catalogue_patch_sector(\'catalogue-change-test\',\'{"name":"catalogue-change-test"}\'::jsonb,\'{"name":"New"}\'::jsonb)',
    )
    _denied(connection, "SELECT public.vm_catalogue_create_alias('CATTESTUSD','NEWALIAS','seed')")


def test_catalogue_function_search_path_and_public_execute_remain_locked(
    connection: sa.Connection,
) -> None:
    names = [
        "vm_catalogue_patch_broker",
        "vm_catalogue_patch_broker_environment",
        "vm_catalogue_patch_sector",
        "vm_catalogue_create_alias",
        "vm_catalogue_create_broker_symbol",
    ]
    rows = connection.execute(
        sa.text("SELECT prosecdef,proconfig FROM pg_proc WHERE proname=ANY(:names)"),
        {"names": names},
    ).all()
    assert len(rows) == 5
    assert all(row == (True, ["search_path=pg_catalog, pg_temp"]) for row in rows)
    assert (
        connection.scalar(
            sa.text(
                "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) a WHERE p.proname=ANY(:names) AND a.grantee=0 AND a.privilege_type='EXECUTE'"
            ),
            {"names": names},
        )
        == 0
    )
    _denied(connection, "SELECT public.vm_catalogue_check_patch('{}','{}','{}',ARRAY['name'])")
    assert (
        connection.scalar(
            sa.text(
                "SELECT to_regprocedure('public.vm_catalogue_patch_instrument(text,jsonb,jsonb)')"
            )
        )
        is None
    )
