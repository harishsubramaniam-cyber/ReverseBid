"""A tiny forward-only migration: add any columns the model has and the
database does not. Enough for SQLite in the field, without a migration tool.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from .db import Base, engine

#: Types SQLite accepts in a bare ALTER TABLE ... ADD COLUMN.
_SQL_TYPE = {"TEXT": "TEXT", "VARCHAR": "TEXT", "INTEGER": "INTEGER",
             "FLOAT": "FLOAT", "BOOLEAN": "BOOLEAN", "DATETIME": "DATETIME"}


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
                    if isinstance(arg, (str, int, float, bool)) and not callable(arg):
                        literal = f"'{arg}'" if isinstance(arg, str) else str(int(arg) if isinstance(arg, bool) else arg)
                        default = f" DEFAULT {literal}"
                conn.execute(text(
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} {sql_type}{default}"))
                added.append(f"{table.name}.{column.name}")
    return added
