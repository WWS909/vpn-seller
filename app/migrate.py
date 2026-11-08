"""
Schema upgrades without a migration tool.

New versions of the project may add columns. create_all() creates new
tables but never touches existing ones, so an upgraded install would
crash on the first query of a new column. This adds missing columns to
existing tables (SQLite and PostgreSQL). Columns are only ever added —
nothing is renamed or dropped, so it is safe to run on every start.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from .db import Base

log = logging.getLogger("vpnshop.migrate")


def _default_sql(column, dialect) -> str:
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return ""
    value = default.arg
    if isinstance(value, bool):
        return " DEFAULT " + (("TRUE" if value else "FALSE") if dialect == "postgresql"
                              else ("1" if value else "0"))
    if isinstance(value, (int, float)):
        return f" DEFAULT {value}"
    if isinstance(value, str):
        return " DEFAULT '" + value.replace("'", "''") + "'"
    return ""


def run(engine) -> list[str]:
    from . import models  # noqa: F401 — registers tables
    added = []
    insp = inspect(engine)
    dialect = engine.dialect.name
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            cols = {c["name"]: c for c in insp.get_columns(table.name)}
            have = set(cols)
            for column in table.columns:
                if column.name in have:
                    # A string column made longer in a newer version (PostgreSQL
                    # enforces the length; SQLite does not care).
                    want = getattr(column.type, "length", None)
                    got = getattr(cols[column.name]["type"], "length", None)
                    if dialect == "postgresql" and want and got and want > got:
                        conn.execute(text(f'ALTER TABLE "{table.name}" ALTER COLUMN "{column.name}" '
                                          f"TYPE VARCHAR({int(want)})"))
                        added.append(f"{table.name}.{column.name} (widened)")
                    continue
                ctype = column.type.compile(dialect=engine.dialect)
                sql = (f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ctype}'
                       f"{_default_sql(column, dialect)}")
                conn.execute(text(sql))
                added.append(f"{table.name}.{column.name}")
    for name in added:
        log.info("schema: added column %s", name)
    return added
