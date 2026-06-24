# app.py
#
# Demo / UI layer for the mar-guardrail framework. It is NOT required to run the
# guardrail (main.py is) — it's a minimal console for trying things out.
#
# Run with:  streamlit run app.py
#
# Design: plain Streamlit primitives only (containers with borders, columns,
# metrics) — no custom CSS or decorative markup. All real logic lives in the
# core modules (config.py, query.py, fivetran_api.py, triggers.py, state.py);
# this file only reads from them and draws the result.

import io
from contextlib import redirect_stderr, redirect_stdout

import streamlit as st

import config
import state as state_mod

# Demo data: {schema: {connection_name: paid_mar}}, the same shape the live
# query returns, so the UI renders identically in either mode.
DEMO_MAR = {
    "salesforce": {"salesforce_prod": 1_250_000, "salesforce_sandbox": 90_000},
    "postgres": {"postgres_analytics": 420_000, "postgres_billing": 510_000},
    "marketing": {"hubspot_marketing": 240_000, "stripe_payments": 75_000},
    "finance": {"netsuite_finance": 980_000},
}

DEFAULT_LIMIT = 1_000_000
# ~10 rows visible, then the connector list scrolls vertically (never sideways).
SCROLL_HEIGHT_PX = 340

STATUS_ICON = {state_mod.OK: "✅", state_mod.WARN: "⚠️",
               state_mod.ERROR: "❌", state_mod.SKIP: "➖"}

# Activity-log level -> icon. Mirrors the levels state.log_event records.
ACTIVITY_ICON = {state_mod.INFO: "•", state_mod.ACTION: "✅", state_mod.ALERT: "🚨",
                 state_mod.LOG_WARN: "⚠️", state_mod.LOG_ERROR: "❌"}

# Channels the demo can "fire" when a connector is over. The send is simulated
# (logged, not actually dispatched) so the demo never spams real Slack/email.
ALERT_CHANNELS = ["pause", "slack", "email", "webhook"]


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------
def load_live(free_types, all_time):
    """Read grouped live MAR, capturing console output and any failure reason.

    Returns (data, error, console_text). `error` is the exact exception text so
    the UI can explain precisely why a connection failed — never just 'failed'."""
    from query import get_mar_by_schema  # imported lazily so demo mode needs no DB

    buf = io.StringIO()
    data, error = {}, None
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            data = get_mar_by_schema(free_types=free_types, all_time=all_time)
    except Exception as exc:  # noqa: BLE001 — report any driver/connection error verbatim
        error = f"{type(exc).__name__}: {exc}"
    return data, error, buf.getvalue()


def flatten(data):
    """{schema: {conn: mar}} -> {conn: mar}."""
    return {conn: mar for instances in data.values() for conn, mar in instances.items()}


# ---------------------------------------------------------------------------
# COMPONENTS
# ---------------------------------------------------------------------------
def render_overview(data, limits):
    flat = flatten(data)
    total_mar = sum(flat.values())
    over = sum(1 for conn, mar in flat.items() if conn in limits and mar > limits[conn])
    with st.container(border=True):
        a, b, c = st.columns(3)
        a.metric("Connectors", len(flat))
        b.metric("Total MAR", f"{total_mar:,}")
        c.metric("Over limit", over)


def render_connectors(data):
    """Connectors grouped by schema/destination, multi-select, vertical scroll.

    Returns the list of selected connection_names."""
    selected = []
    st.subheader("Connectors")
    st.caption("Grouped by schema. Select connectors to watch and set limits.")
    if not data:
        st.caption("No connectors to show.")
        return selected
    with st.container(height=SCROLL_HEIGHT_PX, border=True):
        for schema in sorted(data):
            st.markdown(f"**{schema}**")
            for conn in sorted(data[schema]):
                mar = data[schema][conn]
                if st.checkbox(f"{conn}  ·  {mar:,} MAR", key=f"sel_{conn}"):
                    selected.append(conn)
    return selected


def render_limits(selected, flat):
    """One integer MAR limit per selected connector, with live OVER/OK status.
    Integer limits only — no percentages, no cost math."""
    st.subheader("Limits")
    limits = {}
    if not selected:
        st.caption("Select one or more connectors above to set integer MAR limits.")
        return limits
    with st.container(border=True):
        for conn in selected:
            row = st.columns([3, 1])
            limits[conn] = row[0].number_input(
                f"{conn} — monthly MAR limit",
                min_value=1, step=1, value=DEFAULT_LIMIT, key=f"lim_{conn}",
            )
            mar = flat.get(conn, 0)
            row[1].markdown(
                "**:red[OVER]**" if mar > limits[conn] else "**:green[OK]**"
            )
    return limits


def run_check(selected, limits, flat, channels, data_mode):
    """Run one guardrail evaluation over the selected connectors and record it to
    the activity log. Reuses main.evaluate() — the SAME decision logic the real
    CLI uses — so the demo's activity feed matches production behavior.

    Alert dispatch is simulated (logged, never actually sent) so the demo is
    safe to click repeatedly without paging anyone."""
    import main  # lazy: pulls in triggers/requests only when a check is run

    connectors = [
        {"connection_name": name, "mar_limit": int(limits[name]), "triggers": channels}
        for name in selected
    ]
    state_mod.log_event(
        f"Guardrail check started ({data_mode}) — {len(connectors)} connector(s)",
        level=state_mod.INFO, source="run",
    )
    results = main.evaluate(connectors, flat)

    for result in results:
        if not result["over"]:
            continue
        if not channels:
            state_mod.log_event(
                f"{result['connection_name']} is over limit but no alert channels selected",
                level=state_mod.LOG_WARN, source="run",
            )
        for channel in channels:
            state_mod.log_event(
                f"[{channel}] alert sent for '{result['connection_name']}' "
                "(demo — not actually dispatched)",
                level=state_mod.ACTION, source=channel,
            )

    over_count = sum(1 for r in results if r["over"])
    state_mod.log_event(
        f"Check complete — {over_count} over limit of {len(results)} checked",
        level=state_mod.ALERT if over_count else state_mod.INFO, source="run",
    )


def render_run_controls(selected, limits, flat, data_mode):
    st.subheader("Run")
    with st.container(border=True):
        channels = st.multiselect(
            "Alert channels to fire when a connector is over",
            ALERT_CHANNELS, default=["slack"],
            help="Simulated in the demo — events are logged, nothing is actually sent.",
        )
        run_col, clear_col = st.columns(2)
        run_clicked = run_col.button(
            "Run guardrail check", key="run_btn", type="primary",
            use_container_width=True, disabled=not selected,
        )
        if clear_col.button("Clear log", key="clear_btn", use_container_width=True):
            state_mod.clear_activity_log()
            st.rerun()
        if not selected:
            st.caption("Select at least one connector above to run a check.")
    if run_clicked:
        run_check(selected, limits, flat, channels, data_mode)


def render_activity_log():
    st.subheader("Activity log")
    events = state_mod.activity_log(newest_first=True)
    if not events:
        st.caption("No activity yet — select connectors, set limits, then "
                   "**Run guardrail check**. Real guardrail runs (main.py) also "
                   "appear here when run in the same process.")
        return
    with st.container(height=320, border=True):
        for ev in events:
            icon = ACTIVITY_ICON.get(ev["level"], "•")
            st.markdown(f"{icon} `{ev['ts']}` **[{ev['source']}]** {ev['message']}")


def render_debug_panel(data_mode, console_text, load_error):
    """Inline debug/observability panel — only rendered when the Debug button is
    toggled open. Shows the pre-flight system state and the captured console."""
    with st.container(border=True):
        st.markdown("**Debug**")

        snap = state_mod.snapshot(data_mode)
        st.caption(f"System state · mode: {snap['data_mode'].upper()} · {snap['timestamp']}")
        for check in snap["checks"]:
            st.write(f"{STATUS_ICON[check['status']]}  **{check['label']}** — {check['detail']}")

        st.markdown("**Console / log**")
        log = (console_text or "").strip()
        if load_error:
            log = (log + "\n" if log else "") + f"ERROR: {load_error}"
        st.code(log or "(no output)", language="text")


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
st.set_page_config(page_title="MAR Guardrail", layout="centered")

st.session_state.setdefault("debug_open", False)
st.session_state.setdefault("demo_mode", False)      # LIVE is the default view
st.session_state.setdefault("hatch_system", False)
st.session_state.setdefault("hatch_all_time", False)

st.title("MAR Guardrail")
st.caption("Current-month PAID MAR per connector, with limits and alerts.")

# Secondary, low-prominence controls: a quiet Demo toggle and a Debug button,
# pushed to the right so they don't compete with the main content.
_, controls = st.columns([3, 1])
with controls:
    # Widget keys ARE the state (no manual reassignment), so the toggle/button
    # values survive reruns and are addressable.
    st.toggle(
        "Demo mode", key="demo_mode",
        help="Off = live data from your destination. On = bundled sample data.",
    )
    if st.button("Debug", key="debug_btn", use_container_width=True):
        st.session_state["debug_open"] = not st.session_state["debug_open"]

data_mode = "demo" if st.session_state["demo_mode"] else "live"

# Debug panel sits here; we reserve its slot now but fill it after loading so the
# captured console is included. Its escape-hatch toggles must be read *before*
# the load, so they're rendered first.
debug_slot = st.container()
if st.session_state["debug_open"] and data_mode == "live":
    with debug_slot:
        with st.container(border=True):
            st.markdown("**Debug — live data escape hatch**")
            st.caption("The main view is always PAID + current month. These are "
                       "for testing on a free account (only SYSTEM rows exist).")
            h = st.columns(2)
            with h[0]:
                st.toggle("View SYSTEM rows", key="hatch_system")
            with h[1]:
                st.toggle("All time", key="hatch_all_time")

use_system = st.session_state["debug_open"] and data_mode == "live" and st.session_state["hatch_system"]
use_all_time = st.session_state["debug_open"] and data_mode == "live" and st.session_state["hatch_all_time"]

# Load the data for the current mode.
console_text, load_error = "", None
if data_mode == "demo":
    data = DEMO_MAR
else:
    free_types = ("SYSTEM",) if use_system else ("PAID",)
    data, load_error, console_text = load_live(free_types, use_all_time)

# A visible, specific error log in the main area (full detail also in Debug).
if load_error:
    st.error(
        f"Couldn't load live MAR — {load_error}\n\n"
        "Open **Debug** for the full system state and console. Or switch on "
        "**Demo mode** to explore the layout without a database."
    )
elif data_mode == "live" and not data:
    hint = ("" if use_system else
            " On a free Fivetran account there are no PAID rows — open **Debug** "
            "and enable *View SYSTEM rows* to see live data.")
    st.info(f"Connected, but no {'SYSTEM' if use_system else 'PAID'} MAR "
            f"for the selected window.{hint}")

flat = flatten(data)

# Overview pinned to the top, but it needs the limits computed below, so reserve
# its slot and fill it after the limit inputs render.
overview_slot = st.container()
selected = render_connectors(data)
limits = render_limits(selected, flat)
render_run_controls(selected, limits, flat, data_mode)
render_activity_log()
with overview_slot:
    render_overview(data, limits)

# Fill the debug panel's state + console now that the load has run.
if st.session_state["debug_open"]:
    with debug_slot:
        render_debug_panel(data_mode, console_text, load_error)
