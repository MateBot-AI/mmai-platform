"""E7: first-start staff seeding never writes a password to the log.

What must hold (server/accounts.py ensure_staff_accounts):
  * an account missing from the database is still created when MMAI_INITIAL_PASSWORD_<USERNAME> is not set, but the log line
    carries no password of any kind - neither through a caller's log callback nor through the default stderr logger;
  * that account is made usable by the manager through the existing reset route, whose one-time reply is the only place a new
    password appears;
  * the environment branch is unchanged: it logs the variable's NAME, never its value, and the value is the working password;
  * accounts that already exist are not touched by a later start, whatever the environment then says.

The tests record every random value the seeding could have used as a first password (spies on the two generators that return
their real values) and compare whole log lines, so "no password in the log" is checked against known values, not by eye.
"""
import importlib
import re

import pytest

from tests.conftest import TEST_PW
from server import accounts
from server.db import rows

SEEDED = [("bill", "Bill Pana", "owner"), ("anna", "Anna Chan", "staff"), ("elaisa", "Elaisa Doctolero", "staff"),
          ("mylene", "Mylene Diamas", "staff"), ("keithley", "Keithley Benito", "staff"), ("jordan", "Jordan Groth", "staff")]
READABLE_PASSWORD = re.compile(r"[A-Z][a-z]+-[A-Z][a-z]+-\d{4}")   # the Word-Word-4digits shape of generate_password()


def env_key(username):
    return f"MMAI_INITIAL_PASSWORD_{username.upper()}"


def line_with_env(username, display):
    return f"[mmai accounts] created staff account '{username}' ({display}) with the password from {env_key(username)}"


def line_without_env(username, display, role):
    who = "another manager" if role == "owner" else "the manager"
    text = (f"[mmai accounts] created staff account '{username}' ({display}) with NO {env_key(username)} set: no first password "
            f"was issued, so nobody can sign in to it until {who} sets one (Settings -> Staff accounts -> Reset password)")
    if role == "owner":
        text += f". {env_key(username)} is read only when this account is first created; setting it now does not change this account"
    return text


def account_lines(text):
    return [line for line in text.splitlines() if line.startswith("[mmai accounts]")]


def staff_rows(c):
    return rows(c.execute("SELECT * FROM staff_users ORDER BY id"))


def assert_carries_no_password(text, values):
    for value in values:
        assert value not in text, "a generated or configured password value reached the log"
    assert not READABLE_PASSWORD.search(text), "a readable Word-Word-4digits password reached the log"
    assert "temporary password" not in text


@pytest.fixture
def made(monkeypatch):
    """Every value either generator hands out while the test runs, in order. Both spies return the real value."""
    values = []
    real_urlsafe, real_readable = accounts.secrets.token_urlsafe, accounts.generate_password

    def urlsafe(nbytes=None):
        value = real_urlsafe(nbytes)
        values.append(value)
        return value

    def readable():
        value = real_readable()
        values.append(value)
        return value

    monkeypatch.setattr(accounts.secrets, "token_urlsafe", urlsafe)
    monkeypatch.setattr(accounts, "generate_password", readable)
    return values


@pytest.fixture
def no_initial_passwords(monkeypatch):
    for username, _display, _role in SEEDED:
        monkeypatch.delenv(env_key(username), raising=False)


def _fresh_app(tmp_path, monkeypatch, name="e7.db"):
    """The suite's own way of starting the application on an empty database (tests/test_data_and_api.py)."""
    monkeypatch.setenv("MMAI_DB", str(tmp_path / name))
    from fastapi.testclient import TestClient
    import server.db as dbmod
    dbmod._conn = None
    import server.app as appmod
    importlib.reload(appmod)
    return appmod, lambda: TestClient(appmod.app, base_url="https://mmai.test")


def test_accounts_without_a_configured_password_are_created_and_the_log_carries_no_password(db, made, no_initial_passwords):
    logs = []
    result = accounts.ensure_staff_accounts(db, log=logs.append)
    assert result == {"created": [u for u, _d, _r in SEEDED], "disabled": []}
    assert logs == [line_without_env(u, d, r) for u, d, r in SEEDED]   # whole lines: there is no room for a value
    assert made, "the test must know the values the seeding generated, or the absence check below proves nothing"
    assert_carries_no_password("\n".join(logs), made)
    created = {r["username"]: r for r in staff_rows(db)}
    assert list(created) == [u for u, _d, _r in SEEDED]
    for username, display, role in SEEDED:
        r = created[username]
        assert (r["display_name"], r["role"], r["disabled"] or 0) == (display, role, 0)
        assert re.fullmatch(r"[0-9a-f]{64}", r["pw_hash"]) and r["pw_salt"] and r["pw_changed_at"] is None
        for value in made:   # no value is stored in clear either
            assert value not in (r["pw_hash"], r["pw_salt"])
    assert len({r["pw_hash"] for r in created.values()}) == len(SEEDED)


def test_the_default_logger_writes_no_password_to_stderr_or_stdout(db, made, no_initial_passwords, capfd):
    capfd.readouterr()
    result = accounts.ensure_staff_accounts(db)   # no log callback: the production logger (stderr)
    out, err = capfd.readouterr()
    assert result["created"] == [u for u, _d, _r in SEEDED]
    assert account_lines(err) == [line_without_env(u, d, r) for u, d, r in SEEDED] and account_lines(out) == []
    assert made
    assert_carries_no_password(out + "\n" + err, made)


def test_the_environment_branch_logs_the_variable_name_only_and_its_value_is_the_password(db, made, monkeypatch):
    chosen = {u: f"Chosen-{u.title()}-First-{n:04d}" for n, (u, _d, _r) in enumerate(SEEDED, 1)}
    for username, value in chosen.items():
        monkeypatch.setenv(env_key(username), value)
    logs = []
    result = accounts.ensure_staff_accounts(db, log=logs.append)
    assert result == {"created": [u for u, _d, _r in SEEDED], "disabled": []}
    assert logs == [line_with_env(u, d) for u, d, _r in SEEDED]   # exactly the lines the function wrote before this change
    assert_carries_no_password("\n".join(logs), list(chosen.values()) + made)
    for username, value in chosen.items():
        user = accounts.get_user(db, username)
        assert accounts.verify_password(user, value) and value not in (user["pw_hash"], user["pw_salt"])


def test_a_mix_of_configured_and_unconfigured_accounts_logs_each_kind_of_line(db, made, no_initial_passwords, monkeypatch, capfd):
    monkeypatch.setenv(env_key("bill"), "Chosen-Manager-First-0001")
    capfd.readouterr()
    accounts.ensure_staff_accounts(db)
    out, err = capfd.readouterr()
    assert account_lines(err) == [line_with_env("bill", "Bill Pana")] + [line_without_env(u, d, r) for u, d, r in SEEDED[1:]]
    assert_carries_no_password(out + "\n" + err, ["Chosen-Manager-First-0001"] + made)
    assert accounts.verify_password(accounts.get_user(db, "bill"), "Chosen-Manager-First-0001")


def test_existing_accounts_are_not_touched_by_a_later_start(db, made, no_initial_passwords, monkeypatch):
    # an account that was already there before any seeding, with its own password
    salt = "feedfacecafebeef"
    db.execute("INSERT INTO staff_users(username, display_name, pw_salt, pw_hash, role, disabled, created_at) VALUES (?,?,?,?,?,0,?)",
               ("anna", "Anna Chan", salt, accounts.hash_password("Anna-Already-Here-1", salt), "staff", "2026-10-27T00:00:00+00:00"))
    db.commit()
    monkeypatch.setenv(env_key("bill"), "Chosen-Manager-First-0001")
    monkeypatch.setenv(env_key("anna"), "Ignored-Because-Anna-Exists-1")
    logs = []
    first = accounts.ensure_staff_accounts(db, log=logs.append)
    assert first["created"] == ["bill", "elaisa", "mylene", "keithley", "jordan"]
    assert all("'anna'" not in line for line in logs)
    anna = accounts.get_user(db, "anna")
    assert (anna["pw_salt"], anna["created_at"], anna["pw_changed_at"]) == (salt, "2026-10-27T00:00:00+00:00", None)
    assert accounts.verify_password(anna, "Anna-Already-Here-1") and not accounts.verify_password(anna, "Ignored-Because-Anna-Exists-1")
    # someone changes a password; then a later start with a different environment
    accounts.set_password(db, accounts.get_user(db, "jordan")["id"], "Jordan-Own-Password-1")
    before = staff_rows(db)
    monkeypatch.setenv(env_key("bill"), "A-Different-Manager-Value-2")
    monkeypatch.setenv(env_key("jordan"), "A-Different-Jordan-Value-2")
    monkeypatch.setenv(env_key("elaisa"), "Set-Too-Late-For-Elaisa-2")
    later_logs = []
    assert accounts.ensure_staff_accounts(db, log=later_logs.append) == {"created": [], "disabled": []}
    assert later_logs == [] and staff_rows(db) == before
    assert accounts.verify_password(accounts.get_user(db, "bill"), "Chosen-Manager-First-0001")
    assert not accounts.verify_password(accounts.get_user(db, "bill"), "A-Different-Manager-Value-2")
    assert accounts.verify_password(accounts.get_user(db, "jordan"), "Jordan-Own-Password-1")
    assert not accounts.verify_password(accounts.get_user(db, "elaisa"), "Set-Too-Late-For-Elaisa-2")
    assert_carries_no_password("\n".join(logs + later_logs), made + ["Chosen-Manager-First-0001", "Ignored-Because-Anna-Exists-1"])


def test_application_start_logs_no_password_and_the_manager_reset_makes_the_account_usable(tmp_path, made, no_initial_passwords,
                                                                                         monkeypatch, capfd):
    monkeypatch.setenv(env_key("bill"), TEST_PW["bill"])   # the documented first start: only the manager's first password is set
    capfd.readouterr()
    appmod, client = _fresh_app(tmp_path, monkeypatch)
    out, err = capfd.readouterr()
    seeded_values = list(made)
    assert account_lines(err) == [line_with_env("bill", "Bill Pana")] + [line_without_env(u, d, r) for u, d, r in SEEDED[1:]]
    assert_carries_no_password(out + "\n" + err, seeded_values + [TEST_PW["bill"]])
    assert {u["username"] for u in accounts.list_users(appmod.db)} == {u for u, _d, _r in SEEDED}
    # the unconfigured account exists but nobody has a password for it
    for guess in ("", "elaisa", "Elaisa-Doctolero-0000"):
        assert client().post("/api/auth/login", json={"username": "elaisa", "password": guess}).status_code == 401
    # the manager signs in with the configured first password and sets elaisa's through the existing route
    r = client().post("/api/auth/login", json={"username": "bill", "password": TEST_PW["bill"]})
    assert r.status_code == 200 and r.json()["role"] == "owner"
    bill = {"Authorization": "Bearer " + r.json()["token"]}
    r = client().post("/api/auth/users/elaisa/reset-password", json={}, headers=bill)
    assert r.status_code == 200 and r.json()["username"] == "elaisa"
    temporary = r.json()["temporary_password"]
    assert READABLE_PASSWORD.fullmatch(temporary) and temporary in made   # the one-time reply is where the new password appears
    r = client().post("/api/auth/login", json={"username": "elaisa", "password": temporary})
    assert r.status_code == 200 and r.json()["display_name"] == "Elaisa Doctolero" and r.json()["role"] == "staff"
    # the manager may also choose the first password
    r = client().post("/api/auth/users/mylene/reset-password", json={"new_password": "Mylene-First-Password-1"}, headers=bill)
    assert r.status_code == 200
    assert client().post("/api/auth/login", json={"username": "mylene", "password": "Mylene-First-Password-1"}).status_code == 200
    # nothing that happened after start wrote a password to the log or the audit trail
    out, err = capfd.readouterr()
    assert account_lines(err) == []
    assert_carries_no_password(out + "\n" + err, made + [TEST_PW["bill"], "Mylene-First-Password-1"])
    audit = "\n".join(str(sorted(r.items())) for r in rows(appmod.db.execute("SELECT * FROM audit_log ORDER BY id")))
    assert "reset_password" in audit
    assert_carries_no_password(audit, made + [TEST_PW["bill"], "Mylene-First-Password-1"])
