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

from sqlalchemy import bindparam, create_engine, text

from config import DATABASE_URL, MAR_FREE_TYPES, PLATFORM_SCHEMA

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


def get_current_mar(free_types=None, start=None, end=None):
    """Return {connection_name: total_mar} for a measured_date window.

    Only rows whose free_type is in `free_types` are counted (defaults to
    config.MAR_FREE_TYPES — normally ["PAID"]). Connections with zero matching
    MAR in the window simply won't appear in the result.

    Time window (half-open [start, end)):
      - start and end both omitted -> the current calendar month (the normal
        guardrail behavior: MAR is billed per month).
      - pass explicit dates to widen or shift it. The demo UI uses this for an
        "All time" view so historical data (e.g. last month's rows) still shows.

    Pass free_types to override the configured default for one call (e.g. the
    demo UI lets you view SYSTEM rows on a free account that has no PAID MAR).
    """
    # None means "use the configured default"; an explicitly empty list is a
    # caller error (an IN () clause is invalid SQL), so reject it loudly.
    types = list(MAR_FREE_TYPES if free_types is None else free_types)
    if not types:
        raise ValueError("free_types is empty — specify at least one MAR free_type.")
    # First day of the current month, and first day of the NEXT month. MAR is
    # billed per calendar month, so we scope to "this month" by bounding both
    # ends — measured_date >= this month AND < next month — rather than a
    # rolling 30-day window or an open-ended >= that would also fold in any
    # future-dated rows. incremental_mar stamps each row with a daily
    # measured_date (there is no measured_month column — that only appears as a
    # date_trunc() expression in Fivetran's sample queries), so a half-open
    # [month_start, next_month_start) range captures exactly this month's days.
    if start is None and end is None:
        # Default: the current calendar month, bounded on both ends so we capture
        # exactly this month's days and never fold in future-dated rows.
        start = date.today().replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    else:
        # Caller widened/shifted the window; fill in an open side with a bound
        # wide enough to mean "no limit" in practice.
        start = start or date(1970, 1, 1)
        end = end or date(2999, 1, 1)

    # WHY filter on free_type:
    #   Fivetran tags every MAR row PAID (billable), SYSTEM (its own internal
    #   MAR), or FREE. A real guardrail counts only PAID, so it never false-alarms
    #   on rows that don't cost money. Which types to count is configurable
    #   (config.MAR_FREE_TYPES) so a free account — which has no PAID rows — can
    #   still see live SYSTEM numbers while testing.
    #
    # `free_type IN :free_types` uses an expanding bind param: SQLAlchemy expands
    # the list into the right number of placeholders for the target database, so
    # the type list is bound safely (never string-formatted into the SQL). Named
    # params (:start / :end) likewise render in whatever style the DB expects.
    sql = text(
        f"""
        SELECT connection_name, SUM(incremental_rows) AS total_mar
        FROM {_table_ref()}
        WHERE free_type IN :free_types
          AND measured_date >= :start
          AND measured_date < :end
        GROUP BY connection_name
        """
    ).bindparams(bindparam("free_types", expanding=True))

    # The `with` block guarantees the connection is returned to the pool even if
    # the query raises, so we never leak one.
    with _get_engine().connect() as conn:
        result = conn.execute(
            sql,
            {"free_types": types, "start": start, "end": end},
        )
        return {connection_name: int(total_mar) for connection_name, total_mar in result}
