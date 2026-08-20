# config.py
#
# THIS IS THE ONLY FILE YOU NEED TO EDIT FOR BASIC SETUP.
#
# Everything the framework needs to run lives here: how to reach your
# destination database, how to talk to Fivetran, which connectors to watch,
# and how to alert when one goes over budget.
#
# Secrets (passwords, API keys, webhook URLs) are read from environment
# variables so you never commit them to git. Copy .env.example to .env and
# fill it in. The os.getenv(...) defaults below are placeholders so the file
# still imports cleanly during a dry run — replace them or set the env var.

import os

from dotenv import load_dotenv

# Loads variables from a local .env file if present. In production (cron,
# Airflow, a container) you'll typically set real environment variables
# instead, and this call quietly does nothing.
load_dotenv()


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
# Where the Fivetran Platform Connector lands its data. The framework connects
# through SQLAlchemy, so ANY database works — you choose it entirely by this
# URL (and by installing that database's driver; see requirements.txt). Use a
# SQLAlchemy connection URL:
#
#   Postgres : postgresql://user:password@host:5432/dbname
#   Snowflake: snowflake://user:password@account/dbname?warehouse=WH&role=ROLE
#   BigQuery : bigquery://project/dataset
#   Redshift : redshift+psycopg2://user:password@host:5439/dbname
#   Databricks: databricks://token:<token>@host?http_path=...&catalog=...
#
# Swap destinations by changing this one line (or the DATABASE_URL env var).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://user:password@host.neon.tech/dbname?sslmode=require",
)

# The schema (BigQuery: dataset) the Platform Connector writes its tables into.
# Setting this means the MAR query finds incremental_mar regardless of the
# connection's search_path. Leave it blank to use the connection's default
# schema. Note: Fivetran's schema name varies by deployment — newer Platform
# Connector setups land in "fivetran_platform", others in "fivetran_metadata".
PLATFORM_SCHEMA = os.getenv("FIVETRAN_PLATFORM_SCHEMA", "fivetran_metadata")


# ---------------------------------------------------------------------------
# FIVETRAN API
# ---------------------------------------------------------------------------
# Used to pause connectors via the REST API. Fivetran authenticates with an
# API KEY + API SECRET pair (HTTP Basic auth) — generate them in the Fivetran
# dashboard under Account Settings. ACCOUNT_ID is kept here for convenience /
# logging; the pause call resolves connectors by name, not account.
FIVETRAN_API_KEY = os.getenv("FIVETRAN_API_KEY", "your_fivetran_api_key")
FIVETRAN_API_SECRET = os.getenv("FIVETRAN_API_SECRET", "your_fivetran_api_secret")
FIVETRAN_ACCOUNT_ID = os.getenv("FIVETRAN_ACCOUNT_ID", "your_fivetran_account_id")


# ---------------------------------------------------------------------------
# CONNECTORS TO WATCH
# ---------------------------------------------------------------------------
# One entry per connector you want guarded. Each entry has:
#
#   connection_name : must match the connection_name in your Fivetran
#                     incremental_mar table exactly (see query.py).
#   mar_limit       : the monthly PAID MAR ceiling for this connector.
#                     Must be a positive integer greater than 0.
#   triggers        : which actions to fire when the limit is exceeded,
#                     in the order you want them to run. Valid values:
#                       "pause"   -> pause the connector in Fivetran
#                       "slack"   -> post to your Slack webhook
#                       "email"   -> send an SMTP email
#                       "webhook" -> POST to any custom endpoint
#
# To stop watching a connector, delete its entry. To change how aggressive a
# guardrail is, change its mar_limit. To add a brand-new alert channel, see
# the note in triggers.py.
CONNECTORS = [
    {
        "connection_name": "salesforce_prod",
        "mar_limit": 1_000_000,
        "triggers": ["pause", "slack", "email", "webhook"],
    },
    {
        "connection_name": "postgres_analytics",
        "mar_limit": 500_000,
        "triggers": ["slack"],
    },
    {
        "connection_name": "hubspot_marketing",
        "mar_limit": 250_000,
        "triggers": ["pause", "email"],
    },
]


# ---------------------------------------------------------------------------
# TRIGGER SETTINGS
# ---------------------------------------------------------------------------
# Shared configuration for each alert channel. A connector only uses the
# channels listed in its "triggers" above, so it's fine to leave a section
# blank if you never reference it.

# Slack: create an "Incoming Webhook" in your Slack workspace and paste the
# URL here. Change the message wording in triggers.py (trigger_slack).
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# Email: standard SMTP. For Gmail/Google Workspace use an App Password, not
# your login password. "to_addrs" is who receives the alert.
EMAIL_SMTP_CONFIG = {
    "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "port": int(os.getenv("SMTP_PORT", "587")),
    "username": os.getenv("SMTP_USERNAME", ""),
    "password": os.getenv("SMTP_PASSWORD", ""),  # App Password, not a login password.
    "from_addr": os.getenv("SMTP_FROM_ADDR", ""),
    "to_addrs": [a.strip() for a in os.getenv("SMTP_TO_ADDRS", "").split(",") if a.strip()],
}

# Custom webhook: POST the alert to any HTTP endpoint (PagerDuty, Opsgenie,
# your own service). auth_header is sent as the Authorization header — leave
# it empty if the endpoint is unauthenticated.
CUSTOM_WEBHOOK_URL = os.getenv("CUSTOM_WEBHOOK_URL", "")
CUSTOM_WEBHOOK_AUTH_HEADER = os.getenv("CUSTOM_WEBHOOK_AUTH_HEADER", "")


# ---------------------------------------------------------------------------
# STARTUP VALIDATION
# ---------------------------------------------------------------------------
# Catches the most common setup mistake — a missing or nonsensical mar_limit —
# before the framework does any work, so you get a clear message instead of a
# confusing failure mid-run. main.py calls this on startup.
def validate_config():
    """Raise ValueError if any connector config is malformed. No-op on success."""
    if not CONNECTORS:
        raise ValueError("CONNECTORS is empty — add at least one connector to watch.")

    valid_triggers = {"pause", "slack", "email", "webhook"}

    for i, connector in enumerate(CONNECTORS):
        name = connector.get("connection_name")
        if not name or not isinstance(name, str):
            raise ValueError(f"CONNECTORS[{i}] is missing a valid 'connection_name'.")

        limit = connector.get("mar_limit")
        # bool is a subclass of int in Python — exclude it explicitly so that
        # True/False can't sneak through as a valid limit.
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError(
                f"'{name}': mar_limit must be a positive integer greater than 0, "
                f"got {limit!r}."
            )

        triggers = connector.get("triggers")
        if not triggers or not isinstance(triggers, list):
            raise ValueError(f"'{name}': 'triggers' must be a non-empty list.")
        unknown = set(triggers) - valid_triggers
        if unknown:
            raise ValueError(
                f"'{name}': unknown trigger(s) {sorted(unknown)}. "
                f"Valid options are {sorted(valid_triggers)}."
            )


def _is_set(value):
    """A secret/URL counts as 'set' only if it's non-empty and not an obvious
    placeholder left over from .env.example."""
    if not value:
        return False
    placeholders = ("your_", "user:password@", "host.neon.tech")
    return not any(p in value for p in placeholders)


def config_state():
    """Report what config currently knows — no secrets, just presence. The UI's
    debug panel renders this as part of the pre-flight system state so you can
    see, before anything runs, what is and isn't configured."""
    try:
        validate_config()
        config_valid, config_error = True, None
    except ValueError as exc:
        config_valid, config_error = False, str(exc)

    channels = sorted({t for c in CONNECTORS for t in c.get("triggers", [])})
    return {
        "config_valid": config_valid,
        "config_error": config_error,
        "database_url_set": _is_set(DATABASE_URL),
        "platform_schema": PLATFORM_SCHEMA or "(connection default)",
        "connector_count": len(CONNECTORS),
        "configured_channels": channels,
        "fivetran_creds_set": _is_set(FIVETRAN_API_KEY) and _is_set(FIVETRAN_API_SECRET),
        "slack_set": _is_set(SLACK_WEBHOOK_URL),
        "email_set": _is_set(EMAIL_SMTP_CONFIG["username"]) and bool(EMAIL_SMTP_CONFIG["to_addrs"]),
        "webhook_set": _is_set(CUSTOM_WEBHOOK_URL),
    }
