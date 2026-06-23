# main.py
#
# The entry point. Run it with:  python main.py
#
# It reads your config, pulls current-month paid MAR from the Platform
# Connector, compares each watched connector against its limit, and fires the
# configured triggers for any connector that's over. One pass, then it exits —
# scheduling is your call.
#
# HOW TO SCHEDULE IT (pick one — the framework doesn't care which):
#   cron        : 0 * * * * cd /path/to/mar-guardrail && python main.py   # hourly
#   APScheduler : scheduler.add_job(run, "interval", hours=1)             # in your own script
#   Airflow     : BashOperator(task_id="mar_guardrail", bash_command="python /path/main.py")
#
# Design rule: a single connector's problem must never stop the others from
# being checked. Missing data is warned-and-skipped; trigger errors are caught
# inside triggers.py. The loop always runs to completion.

import config
from query import get_current_mar
from triggers import (
    trigger_email,
    trigger_pause,
    trigger_slack,
    trigger_webhook,
)


def _fire_trigger(name, connector, current_mar):
    """Dispatch one trigger by name, pulling its channel config from config.py.

    This is the table you extend when you add a new trigger: add a branch here
    that calls your new trigger_* function with whatever config it needs."""
    connection_name = connector["connection_name"]
    limit = connector["mar_limit"]

    if name == "pause":
        trigger_pause(connection_name, current_mar, limit)
    elif name == "slack":
        trigger_slack(connection_name, current_mar, limit, config.SLACK_WEBHOOK_URL)
    elif name == "email":
        trigger_email(connection_name, current_mar, limit, config.EMAIL_SMTP_CONFIG)
    elif name == "webhook":
        trigger_webhook(
            connection_name,
            current_mar,
            limit,
            config.CUSTOM_WEBHOOK_URL,
            config.CUSTOM_WEBHOOK_AUTH_HEADER,
        )


def run():
    """Run one full guardrail pass over every connector in config.CONNECTORS."""
    # Fail fast and loud on bad config — before we touch the database or
    # Fivetran — so setup mistakes surface clearly instead of mid-run.
    config.validate_config()

    print("=" * 64)
    print("MAR Guardrail — checking current-month PAID MAR against limits")
    print("=" * 64)

    mar_by_connection = get_current_mar()

    for connector in config.CONNECTORS:
        name = connector["connection_name"]
        limit = connector["mar_limit"]

        # A connector configured here but absent from the query results just
        # means it has no paid MAR this month yet. That's not an error — warn
        # and move on rather than crashing the whole run.
        if name not in mar_by_connection:
            print(f"\n[WARN] '{name}' not found in MAR results — skipping (no paid MAR yet?).")
            continue

        current_mar = mar_by_connection[name]
        over = current_mar > limit
        status = "OVER LIMIT" if over else "ok"

        print(f"\n• {name}: {current_mar:,} / {limit:,} paid MAR — {status}")

        if over:
            # Fire triggers in the order the customer listed them in config.
            for trigger_name in connector["triggers"]:
                _fire_trigger(trigger_name, connector, current_mar)

    print("\n" + "=" * 64)
    print("Guardrail pass complete.")
    print("=" * 64)


if __name__ == "__main__":
    run()
