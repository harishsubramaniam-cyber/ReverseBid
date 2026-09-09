"""A tiny forward-only migration: add any columns the model has and the
database does not. Enough for SQLite in the field, without a migration tool.
"""
from __future__ import annotations

import enum as _enum

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
