"""Login-role passwords: no silent, guessable defaults outside throwaway databases.

These tests need no database.
"""

from __future__ import annotations

import pytest

from medchron import cli
from medchron.db.roles import LoginRole, MissingRolePassword, login_roles_from_env

PASSWORD_VARS = ("MEDCHRON_DB_API_PASSWORD", "MEDCHRON_DB_WORKER_PASSWORD")


@pytest.fixture
def no_passwords(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (*PASSWORD_VARS, "MEDCHRON_DB_API_USER", "MEDCHRON_DB_WORKER_USER"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.usefixtures("no_passwords")
def test_unset_password_is_refused_by_default() -> None:
    with pytest.raises(MissingRolePassword, match="MEDCHRON_DB_API_PASSWORD is not set"):
        login_roles_from_env()


@pytest.mark.usefixtures("no_passwords")
def test_each_unset_password_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDCHRON_DB_API_PASSWORD", "s3cret-api-0123456789")
    with pytest.raises(MissingRolePassword, match="MEDCHRON_DB_WORKER_PASSWORD"):
        login_roles_from_env()


@pytest.mark.usefixtures("no_passwords")
def test_explicit_passwords_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDCHRON_DB_API_PASSWORD", "api-pw-0123456789")
    monkeypatch.setenv("MEDCHRON_DB_WORKER_PASSWORD", "worker-pw-0123456789")
    assert login_roles_from_env() == [
        LoginRole("medchron_api", "api-pw-0123456789", system=False),
        LoginRole("medchron_worker", "worker-pw-0123456789", system=True),
    ]


@pytest.mark.usefixtures("no_passwords")
def test_defaults_only_when_explicitly_allowed() -> None:
    api, worker = login_roles_from_env(allow_defaults=True)
    assert (api.password, worker.password) == ("medchron_api", "medchron_worker")


@pytest.mark.usefixtures("no_passwords")
def test_empty_password_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDCHRON_DB_API_PASSWORD", "")
    with pytest.raises(MissingRolePassword):
        login_roles_from_env()


@pytest.mark.usefixtures("no_passwords")
def test_cli_refuses_before_touching_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("db-users connected to the database before checking passwords")

    monkeypatch.setattr("medchron.db.store.make_engine", must_not_connect)
    with pytest.raises(SystemExit, match="db-users: MEDCHRON_DB_API_PASSWORD is not set"):
        cli.main(["db-users"])
