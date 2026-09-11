"""A tiny forward-only migration: add any columns the model has and the
database does not. Enough for SQLite in the field, without a migration tool.
"""
from __future__ import annotations

import enum as _enum

from datetime import datetime

from sqlalchemy import inspect, text

from .db import Base, engine

#: Types SQLite accepts in a bare ALTER TABLE ... ADD COLUMN.
_SQL_TYPE = {"TEXT": "TEXT", "VARCHAR": "TEXT", "INTEGER": "INTEGER",
             "FLOAT": "FLOAT", "BOOLEAN": "BOOLEAN", "DATETIME": "DATETIME"}


def _literal(arg) -> str | None:
    """The default value as SQL, or None when we should not write one.

    Enum columns are the trap here: SQLAlchemy stores the member *name*, so a
    default of ``Role.BUYER`` has to be written as ``'BUYER'``. Writing
    ``str(Role.BUYER)`` instead put the text "Role.BUYER" in the column, and
    every row it touched then failed to load with a LookupError - a restart to
    pick up a new column took the whole app down.
    """
    if isinstance(arg, _enum.Enum):
        return "'" + str(arg.name).replace("'", "''") + "'"
    if isinstance(arg, bool):
        return str(int(arg))
    if isinstance(arg, (int, float)):
        return str(arg)
    if isinstance(arg, str):
        return "'" + arg.replace("'", "''") + "'"
    return None


def adopt_into_one_organisation() -> str:
    """Give an installation that predates organisations one of its own.

    Before this, a deployment WAS a single buying company: the first account
    owned everything and sign-up closed behind it. Those installations have
    rows with no organisation against them, which would be invisible once
    every screen filters by one. So the first time this runs on such a
    database, it makes an organisation and moves everything into it. Nothing
    is lost and nobody has to do anything.
    """
    with engine.begin() as conn:
        tables = set(inspect(engine).get_table_names())
        if "organisations" not in tables or "users" not in tables:
            return ""
        orphans = conn.execute(text(
            "SELECT COUNT(*) FROM users WHERE org_id IS NULL")).scalar() or 0
        if not orphans:
            return ""
        existing = conn.execute(text("SELECT id FROM organisations ORDER BY id")).first()
        if existing:
            org_id = existing[0]
        else:
            # Name it after whoever set the installation up, so the screen
            # says something recognisable rather than "Organisation 1".
            owner = conn.execute(text(
                "SELECT name FROM users WHERE role IN ('BUYER','ADMIN') "
                "ORDER BY id LIMIT 1")).first()
            label = (owner[0] if owner else "").strip()
            name = f"{label}'s organisation" if label else "Our organisation"
            conn.execute(text("INSERT INTO organisations (name, created_at) "
                              "VALUES (:n, :t)"), {"n": name[:200], "t": datetime.utcnow()})
            org_id = conn.execute(text(
                "SELECT id FROM organisations ORDER BY id DESC LIMIT 1")).scalar()
        for table in ("users", "vendors", "units", "items", "auctions"):
            if table in tables:
                conn.execute(text(f"UPDATE {table} SET org_id = :o WHERE org_id IS NULL"),
                             {"o": org_id})
        # The old schema made an address unique across the whole database.
        # Two organisations must be able to hold the same supplier, so that
        # index is replaced with one that is unique per organisation.
        for index in inspect(engine).get_indexes("users"):
            if index.get("unique") and index.get("column_names") == ["email"]:
                conn.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))
                conn.execute(text('CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)'))
                conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_user_org_email '
                                  'ON users (org_id, email)'))
        return f"{orphans} account(s) and everything they own"


def run() -> list[str]:
    """Returns a list of the columns it added, for logging."""
    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.tables.values():
            if table.name not in existing_tables:
                continue                      # create_all will make it
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                type_name = column.type.__class__.__name__.upper()
                sql_type = _SQL_TYPE.get(type_name, "TEXT")
                default = ""
                if column.default is not None and getattr(column.default, "arg", None) is not None:
                    arg = column.default.arg
                    if not callable(arg):
                        literal = _literal(arg)
                        if literal is not None:
                            default = f" DEFAULT {literal}"
                conn.execute(text(
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} {sql_type}{default}"))
                added.append(f"{table.name}.{column.name}")
    return added
