# query.py
#
# Reads current-month MAR straight from the Fivetran Platform Connector.
#
# The Platform Connector lands a table called `incremental_mar` in your
# destination. We sum it up per connection so main.py can compare each
# connector against its limit. If your destination isn't PostgreSQL, this is
# the one place to swap the driver (e.g. snowflake-connector-python) — the
# SQL itself is standard.

from datetime import date

import psycopg2

from config import DATABASE_URL


def get_current_mar():
    """Return {connection_name: total_paid_mar} for the current calendar month.

    Only PAID rows are counted (see the free_type note below), and only rows
    measured in the current month. Connections with zero paid MAR this month
    simply won't appear in the result.
    """
    # First day of the current month, and first day of the NEXT month. MAR is
    # billed per calendar month, so we scope to "this month" by bounding both
    # ends — measured_month >= this month AND < next month — rather than a
    # rolling 30-day window or an open-ended >= that would also fold in any
    # future-dated rows.
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
    sql = """
        SELECT connection_name, SUM(incremental_rows) AS total_mar
        FROM incremental_mar
        WHERE free_type = 'PAID'
          AND measured_month >= %s
          AND measured_month < %s
        GROUP BY connection_name
    """

    # `with` blocks guarantee the connection and cursor close even if the query
    # raises, so we never leak a connection back to Neon's pool.
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (month_start, next_month_start))
            rows = cur.fetchall()

    return {connection_name: int(total_mar) for connection_name, total_mar in rows}
