# app.py
#
# This is a demo interface built on top of the mar-guardrail framework. It is
# not required to run the framework. Use it to demonstrate capabilities or as a
# starting point for your own UI.
#
# Run with:  streamlit run app.py
#
# It reads CREDENTIALS from config.py, but every connector pick and MAR limit
# you set here is independent UI state — this app NEVER writes back to
# config.py. config.py stays the single source of truth for the real run
# (main.py). Think of this as a sandbox for trying out guardrail setups.
#
# WHERE TO POINT IT AT YOUR DATA: by default it shows sample connectors so it
# runs with zero setup. Flip USE_LIVE_DATA to True (or wire load_mar() to your
# own source) once your Platform Connector + config.py are ready.

from datetime import datetime, timedelta

import streamlit as st

import config

# Set True to pull real numbers from query.get_current_mar() instead of the
# sample data below. Requires a working DATABASE_URL in config.py.
USE_LIVE_DATA = False

# Sample data so the demo runs immediately. Keys are "connector types" and each
# maps to one-or-more named connection instances with their current paid MAR.
# This mirrors how Fivetran groups multiple instances under one connector type.
DEMO_MAR = {
    "salesforce": {"salesforce_prod": 1_250_000, "salesforce_sandbox": 90_000},
    "postgres": {"postgres_analytics": 420_000, "postgres_billing": 510_000},
    "hubspot": {"hubspot_marketing": 240_000},
    "stripe": {"stripe_payments": 75_000, "stripe_eu": 60_000},
    "netsuite": {"netsuite_finance": 980_000},
}

# Default limit shown for a connector before the user overrides it. We seed
# from config.py where a connector is already configured, else use this.
DEFAULT_LIMIT = 1_000_000

# "Near limit" means at or above this share of the configured limit.
NEAR_LIMIT_RATIO = 0.8

# Roughly ten rows tall, then the container scrolls. Tuned to the spec's
# "max 10 rows visible before scrolling, vertical scroll only".
SCROLL_HEIGHT_PX = 360


def load_mar():
    """Return {connector_type: {connection_name: paid_mar}} for the demo.

    Uses sample data unless USE_LIVE_DATA is on, in which case it folds the
    flat {connection_name: mar} from the framework's real query into the same
    type-grouped shape the UI expects."""
    if not USE_LIVE_DATA:
        return DEMO_MAR

    from query import get_current_mar  # imported lazily so the demo runs without a DB

    grouped = {}
    for connection_name, mar in get_current_mar().items():
        # Infer the connector type from the name prefix (everything before the
        # first underscore). Adjust if your naming convention differs.
        connector_type = connection_name.split("_")[0]
        grouped.setdefault(connector_type, {})[connection_name] = mar
    return grouped


def configured_limit(connection_name):
    """Limit from config.py if this connection is already configured, else the
    default. Read-only — the UI never persists changes back to config."""
    for c in config.CONNECTORS:
        if c["connection_name"] == connection_name:
            return c["mar_limit"]
    return DEFAULT_LIMIT


def seed_activity_log():
    """Sample activity so the log isn't empty on first load. In a real UI you'd
    populate this from a table the framework writes to."""
    now = datetime.now()
    return [
        {"time": now - timedelta(minutes=4), "connection": "salesforce_prod", "status": "PAUSED"},
        {"time": now - timedelta(minutes=4), "connection": "netsuite_finance", "status": "ALERT"},
        {"time": now - timedelta(hours=2), "connection": "postgres_billing", "status": "ALERT"},
        {"time": now - timedelta(hours=5), "connection": "hubspot_marketing", "status": "OK"},
    ]


# --- status badge styling -------------------------------------------------
# PAUSED = we stopped the connector, ALERT = we notified, OK = under limit.
BADGE_COLORS = {"PAUSED": "#d64545", "ALERT": "#e0a300", "OK": "#2e9e5b"}


def badge(status):
    color = BADGE_COLORS.get(status, "#888")
    return (
        f"<span style='background:{color};color:white;padding:2px 8px;"
        f"border-radius:10px;font-size:0.75rem;font-weight:600'>{status}</span>"
    )


def render_connector_selector(card_key, mar_data):
    """Grouped, scrollable connector multi-select for one trigger card.

    Connector types are expandable (one st.expander each) so multiple instances
    collapse neatly. The whole thing lives in a fixed-height container so it
    scrolls vertically only once it passes ~10 rows. Returns the list of
    selected connection_names."""
    selected = []
    with st.container(height=SCROLL_HEIGHT_PX):
        for connector_type, instances in mar_data.items():
            # Expanded by default when a type has multiple instances, so the
            # "multiple instances" case is visible without an extra click.
            with st.expander(f"{connector_type}  ({len(instances)})", expanded=len(instances) > 1):
                for connection_name in instances:
                    if st.checkbox(connection_name, key=f"{card_key}_sel_{connection_name}"):
                        selected.append(connection_name)
    return selected


def render_limit_inputs(card_key, selected, mar_data):
    """The 'Set MAR limit' section that appears AFTER connectors are selected.

    Same scrollable-window pattern as the selector: one positive-integer input
    per selected connector, with inline validation."""
    st.markdown("**Set MAR limit** — one per selected connector")
    # Flatten so we can show the current MAR next to each input as context.
    current_mar = {n: m for inst in mar_data.values() for n, m in inst.items()}
    with st.container(height=SCROLL_HEIGHT_PX):
        for connection_name in selected:
            value = st.number_input(
                f"{connection_name}  (now: {current_mar.get(connection_name, 0):,} MAR)",
                min_value=0,
                step=10_000,
                value=configured_limit(connection_name),
                key=f"{card_key}_lim_{connection_name}",
            )
            # Inline validation — the framework's rule is "positive integer > 0".
            if value <= 0:
                st.error("Limit must be a positive integer greater than 0.")


def render_trigger_card(card_key, title, description, mar_data):
    """One trigger card: enable toggle -> connector select -> limit inputs.

    All four channels (pause/slack/email/webhook) share this exact layout so
    the demo stays consistent and the pattern is obvious to copy."""
    with st.container(border=True):
        st.subheader(title)
        st.caption(description)
        enabled = st.toggle("Enable this trigger", key=f"{card_key}_toggle")
        if not enabled:
            return

        selected = render_connector_selector(card_key, mar_data)
        # The limit section only appears once something is selected — keeps the
        # card compact until there's actually a connector to configure.
        if selected:
            render_limit_inputs(card_key, selected, mar_data)
        else:
            st.info("Select one or more connectors to set their MAR limits.")


# ===========================================================================
# PAGE
# ===========================================================================
st.set_page_config(page_title="MAR Guardrail", layout="wide")
st.title("MAR Guardrail — demo console")
st.caption(
    "Demo interface only — not required to run the framework. "
    "Selections here are local UI state and are never written back to config.py."
)

mar_data = load_mar()

# --- Account overview ------------------------------------------------------
# Flatten the grouped data once for the top-line metrics.
flat = {n: m for inst in mar_data.values() for n, m in inst.items()}
total_paid_mar = sum(flat.values())
active_count = len(flat)
near_limit = [
    n for n, m in flat.items() if m >= NEAR_LIMIT_RATIO * configured_limit(n)
]

st.header("Account overview")
col1, col2, col3 = st.columns(3)
col1.metric("Total paid MAR (this month)", f"{total_paid_mar:,}")
col2.metric("Active connectors", active_count)
col3.metric("Near or over limit", len(near_limit))
if near_limit:
    st.warning("At or above 80% of limit: " + ", ".join(near_limit))

# --- Trigger cards ---------------------------------------------------------
st.header("Triggers")
render_trigger_card(
    "pause", "Pause connector",
    "Hard stop — pauses the connector in Fivetran when it exceeds its limit.",
    mar_data,
)
render_trigger_card(
    "slack", "Slack alert",
    "Posts a message to your Slack incoming webhook.",
    mar_data,
)
render_trigger_card(
    "email", "Email alert",
    "Sends an SMTP email to your configured recipients.",
    mar_data,
)
render_trigger_card(
    "webhook", "Custom webhook",
    "POSTs the alert to any HTTP endpoint (PagerDuty, Opsgenie, your own service).",
    mar_data,
)

# --- Activity log ----------------------------------------------------------
st.header("Activity log")
if "activity" not in st.session_state:
    st.session_state.activity = seed_activity_log()
for entry in st.session_state.activity:
    cols = st.columns([2, 3, 1])
    cols[0].write(entry["time"].strftime("%Y-%m-%d %H:%M:%S"))
    cols[1].write(entry["connection"])
    cols[2].markdown(badge(entry["status"]), unsafe_allow_html=True)
