# app.py
#
# Demo console for the mar-guardrail framework. It is NOT required to run the
# framework (main.py is) — it's a polished dashboard for demonstrating the
# guardrail and trying out trigger setups.
#
# Run with:  streamlit run app.py
#
# It reads CREDENTIALS from config.py, but every connector pick and MAR limit
# you set here is independent UI state — this app NEVER writes back to
# config.py. config.py stays the single source of truth for the real run.
#
# WHERE TO POINT IT AT YOUR DATA: by default it shows sample connectors so it
# runs with zero setup. Use the "Data source" control at the top of the page to
# switch to real MAR from your destination at runtime — no code edit needed. The
# USE_LIVE_DATA constant below just sets which way that control starts.

from datetime import date

import pandas as pd
import streamlit as st

import config

# The DEFAULT for the "Data source" control at the top of the page. True starts
# the app on real numbers from query.get_current_mar() (needs a working
# DATABASE_URL); False starts it on the sample data below. Either way the user
# can switch it live in the UI.
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

# Roughly ten rows tall, then the container scrolls.
SCROLL_HEIGHT_PX = 360

# --- design tokens ---------------------------------------------------------
# One palette, referenced everywhere, so the whole UI stays consistent.
COLOR_OK = "#16a34a"
COLOR_NEAR = "#d97706"
COLOR_OVER = "#dc2626"
COLOR_INK = "#0f172a"
COLOR_MUTED = "#64748b"


# ===========================================================================
# DATA
# ===========================================================================
def load_mar(use_live, free_types=None, start=None, end=None):
    """Return {connector_type: {connection_name: mar}}.

    With use_live off, returns the sample data. With it on, folds the flat
    {connection_name: mar} from the framework's real query into the same
    type-grouped shape the UI expects. free_types selects which Fivetran MAR
    types to count (defaults to config.MAR_FREE_TYPES); start/end set the
    measured_date window (None/None = current month)."""
    if not use_live:
        return DEMO_MAR

    from query import get_current_mar  # imported lazily so the demo runs without a DB

    grouped = {}
    for connection_name, mar in get_current_mar(free_types, start, end).items():
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


def status_for(mar, limit):
    """Classify a connector against its limit: OVER / NEAR / OK."""
    ratio = mar / limit if limit else 0
    if ratio >= 1.0:
        return "OVER"
    if ratio >= NEAR_LIMIT_RATIO:
        return "NEAR"
    return "OK"


STATUS_COLOR = {"OVER": COLOR_OVER, "NEAR": COLOR_NEAR, "OK": COLOR_OK}
# Activity-log statuses reuse the same palette so colors mean the same thing
# everywhere: a hard stop is red, a notification amber, all-clear green.
BADGE_COLORS = {"PAUSED": COLOR_OVER, "ALERT": COLOR_NEAR, "OK": COLOR_OK}


def connector_frame(mar_data):
    """Flatten the grouped data into a sorted DataFrame the UI renders from.

    One row per connection, most-at-risk first (highest MAR/limit ratio), so
    the connectors that need attention are always at the top."""
    rows = []
    for connector_type, instances in mar_data.items():
        for connection_name, mar in instances.items():
            limit = configured_limit(connection_name)
            ratio = mar / limit if limit else 0
            rows.append(
                {
                    "type": connector_type,
                    "connection": connection_name,
                    "mar": mar,
                    "limit": limit,
                    "ratio": ratio,
                    "status": status_for(mar, limit),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("ratio", ascending=False).reset_index(drop=True)
    return df


# ===========================================================================
# STYLING
# ===========================================================================
def inject_css():
    """All custom styling in one place. Keeps the Python below readable and the
    look consistent — change a token here, it changes everywhere."""
    st.markdown(
        f"""
        <style>
          /* Tighten Streamlit's default chrome for a dashboard feel. */
          #MainMenu, footer {{visibility: hidden;}}
          [data-testid="stToolbar"], .stDeployButton,
          [data-testid="stDecoration"] {{display: none !important;}}
          [data-testid="stHeader"] {{background: transparent;}}
          .block-container {{padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1180px;}}
          html, body, [class*="css"] {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            color: {COLOR_INK};
          }}

          /* --- header bar --- */
          .app-header {{
            display: flex; align-items: center; justify-content: space-between;
            padding: 22px 28px; border-radius: 16px; margin-bottom: 22px;
            background: linear-gradient(120deg, #4f46e5 0%, #7c3aed 100%);
            color: #fff; box-shadow: 0 10px 30px rgba(79,70,229,.25);
          }}
          .app-header h1 {{font-size: 1.5rem; font-weight: 700; margin: 0; color: #fff;}}
          .app-header .sub {{font-size: .9rem; opacity: .85; margin-top: 4px;}}
          .mode-pill {{
            font-size: .72rem; font-weight: 700; letter-spacing: .04em;
            padding: 6px 12px; border-radius: 999px; text-transform: uppercase;
            background: rgba(255,255,255,.18); border: 1px solid rgba(255,255,255,.35);
          }}

          /* --- KPI tiles --- */
          .kpi {{
            background: #fff; border: 1px solid #e7ebf0; border-radius: 14px;
            padding: 18px 20px; box-shadow: 0 1px 2px rgba(16,24,40,.04);
            border-left: 4px solid var(--accent, #4f46e5); height: 100%;
          }}
          .kpi .label {{font-size: .78rem; color: {COLOR_MUTED}; font-weight: 600;
            text-transform: uppercase; letter-spacing: .04em;}}
          .kpi .value {{font-size: 1.9rem; font-weight: 750; line-height: 1.15; margin-top: 6px;}}
          .kpi .sub {{font-size: .8rem; color: {COLOR_MUTED}; margin-top: 2px;}}

          /* --- connector rows --- */
          .conn-card {{
            background: #fff; border: 1px solid #e7ebf0; border-radius: 14px;
            padding: 6px 4px; box-shadow: 0 1px 2px rgba(16,24,40,.04);
          }}
          .conn-row {{
            display: grid; grid-template-columns: 230px 1fr 150px 78px;
            align-items: center; gap: 16px; padding: 12px 18px;
            border-bottom: 1px solid #f1f4f8;
          }}
          .conn-row:last-child {{border-bottom: none;}}
          .conn-name {{font-weight: 650; font-size: .92rem;}}
          .conn-type {{color: {COLOR_MUTED}; font-size: .74rem; font-weight: 500;}}
          .track {{background: #eef2f7; border-radius: 999px; height: 9px; width: 100%; overflow: hidden;}}
          .fill {{height: 100%; border-radius: 999px;}}
          .conn-meta {{font-size: .8rem; color: {COLOR_MUTED}; text-align: right;
            font-variant-numeric: tabular-nums;}}
          .pill {{font-size: .68rem; font-weight: 700; letter-spacing: .03em;
            padding: 4px 10px; border-radius: 999px; color: #fff; text-align: center;}}

          /* --- section headings --- */
          .sec {{font-size: 1.05rem; font-weight: 700; margin: 26px 0 12px;}}
          .sec .hint {{font-weight: 400; color: {COLOR_MUTED}; font-size: .85rem;}}

          /* --- activity table --- */
          .act {{background:#fff; border:1px solid #e7ebf0; border-radius:14px; overflow:hidden;
            box-shadow: 0 1px 2px rgba(16,24,40,.04);}}
          .act-row {{display:grid; grid-template-columns: 200px 1fr 90px; align-items:center;
            gap:16px; padding: 11px 18px; border-bottom:1px solid #f1f4f8; font-size:.86rem;}}
          .act-row:last-child {{border-bottom:none;}}
          .act-time {{color:{COLOR_MUTED}; font-variant-numeric: tabular-nums;}}
        </style>
        """,
        unsafe_allow_html=True,
    )


def header_bar(use_live):
    mode = "Live data" if use_live else "Sample data"
    st.markdown(
        f"""
        <div class="app-header">
          <div>
            <h1>MAR Guardrail</h1>
            <div class="sub">Monitor Fivetran MAR spend &middot; alert or pause connectors before they overrun.</div>
          </div>
          <div class="mode-pill">{mode}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def kpi(label, value, sub, accent):
    return (
        f"<div class='kpi' style='--accent:{accent}'>"
        f"<div class='label'>{label}</div>"
        f"<div class='value'>{value}</div>"
        f"<div class='sub'>{sub}</div></div>"
    )


def section(title, hint=""):
    hint_html = f" <span class='hint'>— {hint}</span>" if hint else ""
    st.markdown(f"<div class='sec'>{title}{hint_html}</div>", unsafe_allow_html=True)


# ===========================================================================
# COMPONENTS
# ===========================================================================
def render_overview(df):
    """KPI tiles + per-connector progress bars, all from the connector frame."""
    total_mar = int(df["mar"].sum()) if not df.empty else 0
    active = len(df)
    over = int((df["status"] == "OVER").sum()) if not df.empty else 0
    near = int((df["status"] == "NEAR").sum()) if not df.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(kpi("Paid MAR this month", f"{total_mar:,}", "across all connectors", "#4f46e5"),
                unsafe_allow_html=True)
    c2.markdown(kpi("Active connectors", f"{active}", "reporting paid MAR", "#0ea5e9"),
                unsafe_allow_html=True)
    c3.markdown(kpi("Over limit", f"{over}", "exceeding their cap", COLOR_OVER),
                unsafe_allow_html=True)
    c4.markdown(kpi("Near limit", f"{near}", f"at {int(NEAR_LIMIT_RATIO*100)}%+ of cap", COLOR_NEAR),
                unsafe_allow_html=True)

    section("Connectors", "MAR against limit, most at-risk first")
    if df.empty:
        st.info("No connectors reporting paid MAR this month.")
        return

    rows = []
    for _, r in df.iterrows():
        color = STATUS_COLOR[r["status"]]
        pct = min(r["ratio"] * 100, 100)
        rows.append(
            f"<div class='conn-row'>"
            f"<div class='conn-name'>{r['connection']}<br><span class='conn-type'>{r['type']}</span></div>"
            f"<div class='track'><div class='fill' style='width:{pct:.1f}%;background:{color}'></div></div>"
            f"<div class='conn-meta'>{int(r['mar']):,} / {int(r['limit']):,}<br>{r['ratio']*100:.0f}% of limit</div>"
            f"<div class='pill' style='background:{color}'>{r['status']}</div>"
            f"</div>"
        )
    st.markdown("<div class='conn-card'>" + "".join(rows) + "</div>", unsafe_allow_html=True)


def render_connector_selector(card_key, mar_data):
    """Grouped, scrollable connector multi-select for one trigger card.

    Returns the list of selected connection_names."""
    selected = []
    with st.container(height=SCROLL_HEIGHT_PX):
        for connector_type, instances in mar_data.items():
            with st.expander(f"{connector_type}  ({len(instances)})", expanded=len(instances) > 1):
                for connection_name in instances:
                    if st.checkbox(connection_name, key=f"{card_key}_sel_{connection_name}"):
                        selected.append(connection_name)
    return selected


def render_limit_inputs(card_key, selected, mar_data):
    """One positive-integer MAR-limit input per selected connector, with inline
    validation. Shows current MAR next to each as context."""
    st.markdown("**Set MAR limit** — one per selected connector")
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
            if value <= 0:
                st.error("Limit must be a positive integer greater than 0.")


def render_trigger_card(card_key, title, description, mar_data):
    """One trigger card: enable toggle -> connector select -> limit inputs.

    All four channels share this layout so the pattern is obvious to copy."""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.caption(description)
        enabled = st.toggle("Enable this trigger", key=f"{card_key}_toggle")
        if not enabled:
            return
        selected = render_connector_selector(card_key, mar_data)
        if selected:
            render_limit_inputs(card_key, selected, mar_data)
        else:
            st.info("Select one or more connectors to set their MAR limits.")


def render_activity():
    section("Activity log", "guardrail actions taken this session")
    # Starts empty — real entries would be appended as the framework pauses or
    # alerts on connectors. No fabricated sample rows.
    activity = st.session_state.get("activity", [])
    if not activity:
        st.caption("No guardrail actions yet. Entries appear here when a "
                   "connector crosses its limit and a trigger fires.")
        return
    rows = []
    for e in activity:
        color = BADGE_COLORS.get(e["status"], COLOR_MUTED)
        rows.append(
            f"<div class='act-row'>"
            f"<div class='act-time'>{e['time'].strftime('%Y-%m-%d %H:%M:%S')}</div>"
            f"<div>{e['connection']}</div>"
            f"<div class='pill' style='background:{color}'>{e['status']}</div>"
            f"</div>"
        )
    st.markdown("<div class='act'>" + "".join(rows) + "</div>", unsafe_allow_html=True)


# ===========================================================================
# PAGE
# ===========================================================================
st.set_page_config(page_title="MAR Guardrail", layout="wide", page_icon="🛡️",
                   initial_sidebar_state="collapsed")
inject_css()

# Reserve the header at the very top, then render the data-source control right
# beneath it. The control lives in the MAIN page (not the sidebar, which
# auto-collapses and hid it) so it's always visible while using the app.
header_slot = st.container()

with st.container(border=True):
    pick_col, ctl_col = st.columns([3, 2])
    with pick_col:
        st.markdown("**Data source**")
        st.caption("Sample data needs no setup. Live data reads real MAR from "
                   "your destination via DATABASE_URL + FIVETRAN_PLATFORM_SCHEMA.")
    with ctl_col:
        choice = st.radio(
            "Data source",
            options=["Sample data", "Live data"],
            index=1 if USE_LIVE_DATA else 0,
            horizontal=True,
            label_visibility="collapsed",
            key="ds_source",
        )
    use_live = choice == "Live data"

    # In live mode, let the user choose which Fivetran MAR types to count.
    # Production watches PAID; a free account has only SYSTEM rows, so exposing
    # this here means the dashboard can actually show live numbers instead of
    # coming back empty. Defaults to whatever config.MAR_FREE_TYPES is set to.
    free_types = None
    time_window = "This month"
    if use_live:
        fcol, tcol = st.columns(2)
        with fcol:
            options = ["PAID", "SYSTEM", "FREE"]
            default_types = [t for t in config.MAR_FREE_TYPES if t in options] or ["PAID"]
            free_types = st.multiselect(
                "Count which MAR types",
                options,
                default=default_types,
                help="Fivetran tags each row PAID (billable), SYSTEM (internal), "
                     "or FREE. A free Fivetran account only has SYSTEM rows — "
                     "pick SYSTEM to see live data.",
                key="ds_types",
            )
        with tcol:
            time_window = st.radio(
                "Time window",
                options=["This month", "All time"],
                horizontal=True,
                help="MAR is billed per calendar month, so the guardrail uses "
                     "'This month'. If your data is from an earlier month, pick "
                     "'All time' to see it.",
                key="ds_window",
            )

# Translate the window choice into a measured_date range for the query. "All
# time" passes wide bounds; "This month" passes None so the query defaults to
# the current calendar month (the real guardrail behavior).
win_start, win_end = (None, None)
if time_window == "All time":
    win_start, win_end = date(1970, 1, 1), date(2999, 1, 1)

with header_slot:
    header_bar(use_live)

# In live mode a missing/misconfigured database would otherwise crash the whole
# page; catch it, show a clear message, and fall back to an empty dashboard so
# the user can simply switch back to sample data.
load_error = None
mar_data = {}
if use_live and not free_types:
    st.info("Select at least one MAR type above to load live data.")
else:
    try:
        mar_data = load_mar(use_live, free_types, win_start, win_end)
    except Exception as exc:  # noqa: BLE001 — surface any driver/connection error
        load_error = exc

if load_error is not None:
    st.error(
        f"Couldn't load live MAR from the database: {load_error}\n\n"
        "Check DATABASE_URL and FIVETRAN_PLATFORM_SCHEMA in your .env, then "
        "reload — or switch **Data source** back to *Sample data* above."
    )
elif use_live and free_types and not mar_data:
    extra = (" Try the **All time** window above — your data may be from an "
             "earlier month." if time_window == "This month" else "")
    st.info(
        f"Connected fine, but no {' / '.join(free_types)} MAR rows match "
        f"({time_window.lower()}).{extra} On a free Fivetran account, MAR is "
        "tagged **SYSTEM** — make sure that's selected above."
    )

df = connector_frame(mar_data)

render_overview(df)

# MAR-by-connector chart for an at-a-glance comparison.
section("MAR by connector", "MAR per connection for the selected window")
if not df.empty:
    chart_df = df.set_index("connection")["mar"]
    st.bar_chart(chart_df, height=260, color=COLOR_INK)

section("Triggers", "what happens when a connector crosses its limit")
st.caption("Selections here are local UI state and are never written back to config.py.")
t1, t2 = st.columns(2)
with t1:
    render_trigger_card(
        "pause", "Pause connector",
        "Hard stop — pauses the connector in Fivetran when it exceeds its limit.",
        mar_data,
    )
    render_trigger_card(
        "email", "Email alert",
        "Sends an SMTP email to your configured recipients.",
        mar_data,
    )
with t2:
    render_trigger_card(
        "slack", "Slack alert",
        "Posts a message to your Slack incoming webhook.",
        mar_data,
    )
    render_trigger_card(
        "webhook", "Custom webhook",
        "POSTs the alert to any HTTP endpoint (PagerDuty, Opsgenie, your own service).",
        mar_data,
    )

render_activity()
