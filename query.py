# query.py
#
# Reads current-month MAR from the Fivetran Platform Connector's destination.
#
# Database-agnostic by design: we connect through SQLAlchemy, so the SAME code
# runs against Postgres, Snowflake, BigQuery, Redshift, Databricks, MySQL, and
# any other database SQLAlchemy has a dialect for. You choose the database
# purely by the DATABASE_URL in config.py (and by installing that database's
# driver — see requirements.txt). The query below is deliberately standard SQL
# so it ports cleanly across all of them.

import re
from datetime import date

from sqlalchemy import create_engine, text

from config import DATABASE_URL, PLATFORM_SCHEMA

# One engine per process, created lazily so simply importing this module (e.g.
# the Streamlit demo running on sample data) never opens a connection.
# pool_pre_ping quietly recycles a stale connection instead of erroring on a
# socket the database closed underneath us.
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


def get_current_mar():
    """Return {connection_name: total_paid_mar} for the current calendar month.

    Only PAID rows are counted (see the free_type note below), and only rows
    whose measured_date falls in the current month. Connections with zero paid
    MAR this month simply won't appear in the result.
    """
    # First day of the current month, and first day of the NEXT month. MAR is
    # billed per calendar month, so we scope to "this month" by bounding both
    # ends — measured_date >= this month AND < next month — rather than a
    # rolling 30-day window or an open-ended >= that would also fold in any
    # future-dated rows. incremental_mar stamps each row with a daily
    # measured_date (there is no measured_month column — that only appears as a
    # date_trunc() expression in Fivetran's sample queries), so a half-open
    # [month_start, next_month_start) range captures exactly this month's days.
    month_start = date.today().replace(day=1)
    if month_start.month == 12:
        next_month_start = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month_start = month_start.replace(month=month_start.month + 1)

    # WHY free_type = 'PAID':
    #   Fivetran tags every MAR row as PAID or FREE. Free MAR (e.g. the first
    #   sync of a new table, or rows from free connector types) does not count
    #   against your bill, so guarding on it would produce false alarms. We
    #   only care about the rows that actually cost money — the PAID ones.
    #
    # Named bind params (:start / :end) let SQLAlchemy render the placeholders
    # in whatever style the target database expects, so this one statement works
    # everywhere.
    sql = text(
        f"""
        SELECT connection_name, SUM(incremental_rows) AS total_mar
        FROM {_table_ref()}
        WHERE free_type = 'PAID'
          AND measured_date >= :start
          AND measured_date < :end
        GROUP BY connection_name
        """
    )

    # The `with` block guarantees the connection is returned to the pool even if
    # the query raises, so we never leak one.
    with _get_engine().connect() as conn:
        result = conn.execute(sql, {"start": month_start, "end": next_month_start})
        return {connection_name: int(total_mar) for connection_name, total_mar in result}
