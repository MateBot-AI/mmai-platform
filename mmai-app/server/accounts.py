"""Staff accounts: individual sign-ins for the reservations team (one account per person, so the audit log shows who did what).

Seeding is idempotent: `ensure_staff_accounts` adds an account only when its username is missing and NEVER resets an
existing password. A new account's first password comes from the environment variable MMAI_INITIAL_PASSWORD_<USERNAME>
(upper case) when present. Without it the account row is still created (its name, role and audit identity exist) but no first
password is issued: the row holds the hash of a long random value that is thrown away at once and never logged, returned or
stored in clear, so nobody can sign in to that account until the manager sets its password (Settings -> Staff accounts ->
Reset password, the existing manager-only route). The way into a brand-new database is therefore MMAI_INITIAL_PASSWORD_BILL:
the manager signs in with it and sets the others. Passwords are stored as PBKDF2-SHA256 hashes with a per-user salt; no
password is ever stored, logged or committed in clear text.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys

from .db import now_iso, row, rows

MIN_PASSWORD_LENGTH = 10

# username, display name, role.  'owner' = manager (Bill); 'staff' = reservations team.
STAFF_ACCOUNTS = [
    ("bill", "Bill Pana", "owner"),
    ("anna", "Anna Chan", "staff"),
    ("elaisa", "Elaisa Doctolero", "staff"),
    ("mylene", "Mylene Diamas", "staff"),
    ("keithley", "Keithley Benito", "staff"),
    ("jordan", "Jordan Groth", "staff"),
]
# The shared sign-in from the first previews. Kept as a record (its audit history still resolves) but sign-in is disabled.
SHARED_ACCOUNT = "alm-staff"
SHARED_ACCOUNT_DISPLAY = "ALM Reservations"

_WORDS = ["Amber", "Birch", "Cedar", "Delta", "Ember", "Fjord", "Grove", "Harbour", "Island", "Jasper", "Kelp", "Lagoon", "Maple",
          "North", "Ocean", "Pebble", "Quartz", "River", "Summit", "Tidal", "Umber", "Valley", "Willow", "Yarra", "Zephyr",
          "Anchor", "Beacon", "Comet", "Dune", "Falcon", "Garden", "Heron", "Ivory", "Juniper", "Koala", "Lantern", "Meadow",
          "Nectar", "Orchid", "Pelican", "Raven", "Saffron", "Thistle", "Violet", "Wattle", "Lyre", "Marina", "Harvest", "Granite"]


def generate_password() -> str:
    """Readable temporary password: Word-Word-4digits (two different words)."""
    a = secrets.choice(_WORDS)
    b = secrets.choice([w for w in _WORDS if w != a])
    return f"{a}-{b}-{secrets.randbelow(9000) + 1000}"


def hash_password(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()


def verify_password(user: dict, pw: str) -> bool:
    return secrets.compare_digest(user["pw_hash"], hash_password(pw or "", user["pw_salt"]))


def validate_new_password(pw: str) -> str | None:
    """Return a plain-English problem with the proposed password, or None if it is acceptable."""
    if not pw or len(pw) < MIN_PASSWORD_LENGTH:
        return f"The new password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if pw.strip() != pw:
        return "The new password cannot start or end with a space."
    return None


def set_password(c, user_id: int, pw: str):
    salt = secrets.token_hex(8)
    c.execute("UPDATE staff_users SET pw_salt=?, pw_hash=?, pw_changed_at=? WHERE id=?", (salt, hash_password(pw, salt), now_iso(), user_id))
    c.commit()


def get_user(c, username: str) -> dict | None:
    return row(c.execute("SELECT * FROM staff_users WHERE username=?", ((username or "").strip().lower(),)))


def active_display_names(c) -> list[str]:
    return [r["display_name"] for r in rows(c.execute("SELECT display_name FROM staff_users WHERE COALESCE(disabled,0)=0 ORDER BY id"))]


def list_users(c) -> list[dict]:
    return rows(c.execute("SELECT id, username, display_name, role, COALESCE(disabled,0) AS disabled, created_at, pw_changed_at FROM staff_users ORDER BY id"))


def ensure_staff_accounts(c, log=None) -> dict:
    """Idempotent seeding. Adds missing accounts (never touches existing passwords) and disables the shared account once.
    Returns {"created": [usernames], "disabled": [usernames]} for logging and tests.

    No log line ever carries a password. An account created without MMAI_INITIAL_PASSWORD_<USERNAME> gets no first password at
    all: its row holds the hash of a long random value that exists only inside this loop, so the account cannot be used until
    the manager sets a password through the existing reset route; the log line names the variable and says so."""
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    created, disabled = [], []
    for username, display_name, role in STAFF_ACCOUNTS:
        if get_user(c, username):
            continue
        env_key = f"MMAI_INITIAL_PASSWORD_{username.upper()}"
        pw = os.environ.get(env_key)
        from_env = bool(pw)
        if not pw:
            pw = secrets.token_urlsafe(32)   # nobody is meant to know it: hashed below, then dropped; never logged or returned
        salt = secrets.token_hex(8)
        c.execute("INSERT INTO staff_users(username, display_name, pw_salt, pw_hash, role, disabled, created_at) VALUES (?,?,?,?,?,0,?)",
                  (username, display_name, salt, hash_password(pw, salt), role, now_iso()))
        c.commit()
        created.append(username)
        if from_env:
            log(f"[mmai accounts] created staff account '{username}' ({display_name}) with the password from {env_key}")
        else:
            who = "another manager" if role == "owner" else "the manager"
            note = (f"[mmai accounts] created staff account '{username}' ({display_name}) with NO {env_key} set: no first password "
                    f"was issued, so nobody can sign in to it until {who} sets one (Settings -> Staff accounts -> Reset password)")
            if role == "owner":
                note += f". {env_key} is read only when this account is first created; setting it now does not change this account"
            log(note)
    # The shared account: keep the row (audit history), but close sign-in. Done once, recorded in settings so it is not re-applied
    # if a manager ever re-enables it deliberately.
    if row(c.execute("SELECT value FROM settings WHERE key='accounts.shared_disabled_at'")) is None:
        shared = get_user(c, SHARED_ACCOUNT)
        if shared and not shared.get("disabled"):
            c.execute("UPDATE staff_users SET disabled=1 WHERE id=?", (shared["id"],))
            c.execute("DELETE FROM sessions WHERE user_id=?", (shared["id"],))
            disabled.append(SHARED_ACCOUNT)
        c.execute("INSERT INTO settings(key, value) VALUES ('accounts.shared_disabled_at', ?)", (now_iso(),))
        c.commit()
    return {"created": created, "disabled": disabled}
