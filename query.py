# query.py
#
# Reads current-month PAID MAR from the Fivetran Platform Connector's
# destination, and exposes a small connection probe for the UI's debug panel.
#
# Database-agnostic by design: we connect through SQLAlchemy, so the SAME code
# runs against Postgres, Snowflake, BigQuery, Redshift, Databricks, MySQL, and
# any other database SQLAlchemy has a dialect for. You choose the database
# purely by the DATABASE_URL in config.py (and by installing that database's
# driver — see requirements.txt). The query below is deliberately standard SQL
# so it ports cleanly across all of them.

import re
from datetime import date

from sqlalchemy import bindparam, create_engine, text

from config import DATABASE_URL, PLATFORM_SCHEMA

# One engine per process, created lazily so simply importing this module never
# opens a connection. pool_pre_ping quietly recycles a stale connection instead
# of erroring on a socket the database closed underneath us.
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return _engine


# A schema name is a SQL identifier, not a value, so it can't be passed as a
# bound parameter — it has to be written into the query text. To keep that safe
# we allow only plain identifier characters (optionally one dot, e.g. a
# BigQuery project.dataset). That rules out injection while still covering every
# normal schema name.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")


def _table_ref():
    """The incremental_mar table, schema-qualified when PLATFORM_SCHEMA is set.

    Qualifying the table means the query finds incremental_mar regardless of the
    connection's search_path. Leave PLATFORM_SCHEMA blank to use the default
    schema for the connection."""
    if not PLATFORM_SCHEMA:
        return "incremental_mar"
    if not _IDENTIFIER_RE.match(PLATFORM_SCHEMA):
        raise ValueError(
            f"PLATFORM_SCHEMA {PLATFORM_SCHEMA!r} is not a valid schema identifier."
        )
    return f"{PLATFORM_SCHEMA}.incremental_mar"


def _current_month_window():
    """Half-open [first-of-this-month, first-of-next-month). MAR is billed per
    calendar month, so we bound both ends rather than using a rolling window or
    an open-ended >= that would fold in future-dated rows."""
    start = date.today().replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _fetch_mar(free_types, start, end):
    """Run the grouped MAR query and return rows of (schema_name, connection_name,
    total_mar). Shared by the public readers below.

    `free_type IN :free_types` uses an expanding bind param: SQLAlchemy expands
    the list into the right number of placeholders for the target database, so
    the type list is bound safely (never string-formatted into the SQL). Named
    params (:start / :end) likewise render in whatever style the DB expects."""
    types = [t for t in free_types]
    if not types:
        raise ValueError("free_types is empty — specify at least one MAR free_type.")

    sql = text(
        f"""
        SELECT schema_name, connection_name, SUM(incremental_rows) AS total_mar
        FROM {_table_ref()}
        WHERE free_type IN :free_types
          AND measured_date >= :start
          AND measured_date < :end
        GROUP BY schema_name, connection_name
        """
    ).bindparams(bindparam("free_types", expanding=True))

    with _get_engine().connect() as conn:
        result = conn.execute(
            sql, {"free_types": types, "start": start, "end": end}
        )
        return [(s, c, int(m)) for s, c, m in result]


def get_current_mar():
    """Return {connection_name: total_mar} of current-month PAID MAR.

    This is the guardrail's core read (used by main.py): PAID rows only, current
    calendar month only. Connections with zero paid MAR this month won't appear.
    """
    start, end = _current_month_window()
    rows = _fetch_mar(["PAID"], start, end)
    return {connection_name: total for _, connection_name, total in rows}


def get_mar_by_schema(free_types=("PAID",), all_time=False):
    """Return {schema_name: {connection_name: total_mar}} for the UI.

    Defaults match the guardrail (PAID, current month). The demo's debug panel
    passes free_types=("SYSTEM",) and/or all_time=True as an escape hatch so a
    free Fivetran account — which has no PAID rows — can still see live data.
    """
    if all_time:
        start, end = date(1970, 1, 1), date(2999, 1, 1)
    else:
        start, end = _current_month_window()

    grouped = {}
    for schema_name, connection_name, total in _fetch_mar(list(free_types), start, end):
        grouped.setdefault(schema_name or "(no schema)", {})[connection_name] = total
    return grouped


def get_daily_mar(free_types=("PAID",), all_time=False):
    """Return {connection_name: [(measured_date, rows), ...]} (daily, sorted).

    Same source as get_current_mar — incremental_mar — but NOT collapsed to a
    monthly total. This is the per-day history anomaly detection needs to learn
    each connector's baseline and spot spikes. One query for the whole account.
    """
    if all_time:
        start, end = date(1970, 1, 1), date(2999, 1, 1)
    else:
        start, end = _current_month_window()

    sql = text(
        f"""
        SELECT connection_name, measured_date, SUM(incremental_rows) AS rows
        FROM {_table_ref()}
        WHERE free_type IN :free_types
          AND measured_date >= :start
          AND measured_date < :end
        GROUP BY connection_name, measured_date
        """
    ).bindparams(bindparam("free_types", expanding=True))

    out = {}
    with _get_engine().connect() as conn:
        result = conn.execute(
            sql, {"free_types": list(free_types), "start": start, "end": end}
        )
        for connection_name, measured_date, rows in result:
            out.setdefault(connection_name, []).append((measured_date, int(rows)))
    for series in out.values():
        series.sort(key=lambda t: str(t[0]))
    return out


def check_connection():
    """Probe the database without raising. Returns a dict the debug panel renders:

        {"ok": bool,            # did we connect at all?
         "error": str | None,   # the exact failure message when ok is False
         "table": str,          # the schema-qualified table we look for
         "table_found": bool}   # did incremental_mar resolve?

    This is what powers the observability panel's "explain exactly why" — we hand
    back the real exception text, not a generic 'failed'."""
    try:
        table = _table_ref()
    except ValueError as exc:  # bad PLATFORM_SCHEMA identifier
        return {"ok": False, "error": str(exc), "table": "?", "table_found": False}

    try:
        with _get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — surface any driver/connection error verbatim
        return {"ok": False, "error": str(exc), "table": table, "table_found": False}

    # Connected. Now see whether the MAR table actually resolves.
    try:
        with _get_engine().connect() as conn:
            conn.execute(text(f"SELECT 1 FROM {table} WHERE 1 = 0"))
        return {"ok": True, "error": None, "table": table, "table_found": True}
    except Exception as exc:  # noqa: BLE001 — connected but table missing/unreadable
        return {"ok": True, "error": str(exc), "table": table, "table_found": False}
