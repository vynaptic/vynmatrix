"""Unit tests for the configuration ``vmdev init`` generates and ``doctor`` reads."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from dev_cli.core.deployment import checks, configuration, environment

_RESERVED = "p@ss:w/rd?#[]&=+ x"


def test_fifteen_distinct_secrets_are_generated() -> None:
    generated = configuration.generate_secrets()

    assert set(generated) == set(configuration.SECRET_KEYS)
    assert len(configuration.SECRET_KEYS) == 15
    assert len(set(generated.values())) == 15
    assert all(len(value) >= 32 for value in generated.values())
    # The ring is a Fernet key, which is what the secrets backend accepts.
    from cryptography.fernet import Fernet

    Fernet(generated[configuration.SECRET_RING_KEY].encode("ascii"))


def test_a_password_is_encoded_where_it_is_embedded_in_a_connection_string() -> None:
    url = configuration.database_url(
        "vm_backend_login", _RESERVED, "vm_trading", host="postgres", port=5432
    )

    assert _RESERVED not in url
    assert "@postgres:5432/vm_trading" in url
    # Round-tripping is what matters: the driver must see the original secret.
    assert make_url(url).password == _RESERVED


def test_derived_urls_match_what_bootstrap_requires() -> None:
    values = configuration.build_values(
        configuration.OwnerInputs(
            email="owner@example.test", base_ccy="EUR", tz="UTC", paper_equity=Decimal("1000")
        ),
        host="postgres",
        port=5432,
    )

    admin = make_url(values["ADMIN_DATABASE_URL"])
    migration = make_url(values["MIGRATION_DATABASE_URL"])
    assert admin.database == "postgres"
    assert migration.database == values["DB_NAME"]
    # BootstrapSettings requires the maintenance stages to share one identity,
    # and DB_USER/DB_PASSWORD/DB_NAME to agree with the URLs.
    assert (admin.username, admin.password) == (migration.username, migration.password)
    assert (admin.username, admin.password) == (values["DB_USER"], values["DB_PASSWORD"])
    for key, (login, password_key) in configuration.ROLE_URLS.items():
        url = make_url(values[key])
        assert (url.username, url.password) == (login, values[password_key])
        assert url.database == values["DB_NAME"]


def test_rendering_keeps_the_template_comments_and_key_order(tmp_path: Path) -> None:
    template = "# a comment\nFIRST=placeholder\n\n# another\nSECOND=keep\n"

    rendered = environment.render_values(template, {"FIRST": "generated", "THIRD": "new"})

    assert rendered.splitlines()[:5] == [
        "# a comment",
        "FIRST=generated",
        "",
        "# another",
        "SECOND=keep",
    ]
    assert "THIRD=new" in rendered
    assert "# Added by vmdev init --update" in rendered
    (tmp_path / ".env").write_text(rendered, encoding="utf-8")
    assert environment.load_env_file(tmp_path / ".env")["SECOND"] == "keep"


def test_the_key_diff_names_what_appeared_and_what_was_retired() -> None:
    diff = environment.key_diff(["A", "B", "C"], ["A", "C", "LEGACY"])

    assert diff["missing"] == ["B"]
    assert diff["unexpected"] == ["LEGACY"]


def test_a_new_key_takes_the_template_default_and_a_new_secret_is_generated() -> None:
    existing = {"DB_PORT": "5432", "DB_PASSWORD": "installed", "DB_USER": "trader", "DB_NAME": "db"}
    template = {"NEW_SETTING": "42", "BACKEND_ADMIN_API_KEY": ""}

    filled = configuration.fill_missing(
        existing, template, ["NEW_SETTING", "BACKEND_ADMIN_API_KEY"]
    )

    assert filled["NEW_SETTING"] == "42"
    assert len(filled["BACKEND_ADMIN_API_KEY"]) >= 32


def test_a_new_connection_string_reuses_the_installed_password() -> None:
    existing = {
        "DB_PORT": "5432",
        "DB_USER": "trader",
        "DB_NAME": "vm_trading",
        "DB_PASSWORD": "installed",
        "VM_BACKEND_DB_PASSWORD": "already-provisioned",
    }

    filled = configuration.fill_missing(existing, {}, ["BACKEND_DATABASE_URL"])

    assert make_url(filled["BACKEND_DATABASE_URL"]).password == "already-provisioned"


def test_the_template_declares_every_key_the_generator_writes() -> None:
    root = Path(__file__).resolve().parents[3]
    declared = set(environment.declared_keys(root / configuration.ENV_EXAMPLE))
    values = configuration.build_values(
        configuration.OwnerInputs(
            email="owner@example.test", base_ccy="EUR", tz="UTC", paper_equity=Decimal("1")
        ),
        host="postgres",
        port=5432,
    )

    assert set(values) <= declared, sorted(set(values) - declared)
    assert set(configuration.SECRET_KEYS) <= declared


@pytest.mark.parametrize("value", ["0", "-5", "", "many"])
def test_starting_equity_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(ValueError, match="paper equity"):
        configuration.parse_equity(value)


def test_the_owner_file_is_the_shape_bootstrap_accepts() -> None:
    import yaml

    from dev_cli.core.bootstrap import validate_owner_input

    rendered = configuration.render_owner_file(
        configuration.OwnerInputs(
            email="owner@example.test",
            base_ccy="EUR",
            tz="Europe/Amsterdam",
            paper_equity=Decimal("100000"),
            full_name="Owner",
        )
    )

    validate_owner_input(yaml.safe_load(rendered))


def test_a_container_url_is_resolved_to_the_published_listener() -> None:
    env = {
        "DB_PORT": "55432",
        "MIGRATION_DATABASE_URL": "postgresql://trader:secret@postgres:5432/vm_trading",
        "ADMIN_DATABASE_URL": "postgresql://trader:secret@db.example.test:5432/postgres",
    }

    resolved = environment.resolve_host_env(env)

    assert resolved["MIGRATION_DATABASE_URL"].startswith(
        "postgresql://trader:secret@127.0.0.1:55432/"
    )
    # Any other host was chosen deliberately and is left exactly as written.
    assert resolved["ADMIN_DATABASE_URL"] == env["ADMIN_DATABASE_URL"]


def test_doctor_fails_on_a_placeholder_or_a_reused_secret() -> None:
    base = dict.fromkeys(configuration.SECRET_KEYS, "unique")
    supplied = {key: f"{key}-value" for key in configuration.SECRET_KEYS}

    reused = checks.check_values(base)
    unique = checks.check_values(supplied)
    placeholder = checks.check_values({**supplied, "DB_PASSWORD": "CHANGE_ME_BEFORE_USE"})

    assert checks.worst(reused) == checks.FAIL
    assert checks.worst(unique) == checks.OK
    assert checks.worst(placeholder) == checks.FAIL


def test_doctor_refuses_a_configuration_that_allows_live_execution() -> None:
    supplied = {key: f"{key}-value" for key in configuration.SECRET_KEYS}

    unsafe = checks.check_values({**supplied, "EXECUTION_ENGINE_ALLOW_LIVE": "true"})

    assert checks.worst(unsafe) == checks.FAIL
