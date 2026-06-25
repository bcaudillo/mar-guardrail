# app.py
#
# The operator console for the mar-guardrail framework. It runs on your REAL
# data (the current-month PAID MAR the Platform Connector lands in your
# warehouse) and is built to scale: it surfaces the handful of connectors that
# need attention rather than making you scroll hundreds.
#
# Run with:  streamlit run app.py
#
# It is not required to run the guardrail (main.py is) — but unlike a toy demo,
# it reads live data through the same core modules (config.py, query.py,
# fivetran_api.py, triggers.py, state.py) and can fire the same real actions, so
# what you see here is what the unattended run does.
#
# For a self-contained teaching demo (anomaly detection on generated data) see
# examples/anomaly_demo.py.

import io
from contextlib import redirect_stderr, redirect_stdout

import pandas as pd
import streamlit as st

import config
import state as state_mod

# Bundled sample data for the Demo toggle — {schema: {connection: paid_mar}},
# the exact shape the live query returns, so the UI renders identically.
DEMO_MAR = {
    "salesforce": {"salesforce_prod": 1_250_000, "salesforce_sandbox": 90_000},
    "postgres": {"postgres_analytics": 420_000, "postgres_billing": 510_000},
    "marketing": {"hubspot_marketing": 240_000, "stripe_payments": 75_000},
    "finance": {"netsuite_finance": 980_000},
}

# Default monthly MAR limit applied to any connector that doesn't have an
# explicit limit in config.py. (config.py stays the source of truth for the
# unattended run; this default just lets the console show a status for the rest.)
DEFAULT_LIMIT = 1_000_000
NEAR_THRESHOLD = 0.8  # at/above this fraction of the limit -> NEAR (not yet OVER)

STATUS_ICON = {state_mod.OK: "✅", state_mod.WARN: "⚠️",
               state_mod.ERROR: "❌", state_mod.SKIP: "➖"}
ACTIVITY_ICON = {state_mod.INFO: "•", state_mod.ACTION: "✅", state_mod.ALERT: "🚨",
                 state_mod.LOG_WARN: "⚠️", state_mod.LOG_ERROR: "❌"}
ALERT_CHANNELS = ["pause", "slack", "email", "webhook"]


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------
def load_live(free_types, all_time):
    """Read grouped live MAR, capturing console output and any failure reason.
    Returns (data, error, console_text) — error is the exact exception text."""
    from query import get_mar_by_schema  # lazy import so Demo mode needs no DB

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


def resolve_limits(data, default_limit):
    """Per-connector limit: from config.py where defined, else the UI default.
    This mirrors how the unattended run treats limits, so the console is honest."""
    configured = {c["connection_name"]: c["mar_limit"] for c in config.CONNECTORS}
    return {conn: configured.get(conn, default_limit) for conn in flatten(data)}


def build_rows(data, limits):
    """One scored row per connector. `risk` floats exceptions to the top."""
    rows = []
    for schema, conns in data.items():
        for conn, mar in conns.items():
            limit = limits.get(conn, 0)
            pct = mar / limit if limit else 0.0
            status = "OVER" if mar > limit else ("NEAR" if pct >= NEAR_THRESHOLD else "OK")
            configured = any(c["connection_name"] == conn for c in config.CONNECTORS)
            risk = (1_000 if status == "OVER" else 0) + pct * 100
            rows.append({
                "connector": conn, "schema": schema, "mar": mar, "limit": limit,
                "pct": pct, "status": status, "configured": configured, "risk": risk,
            })
    return rows


# ---------------------------------------------------------------------------
# COMPONENTS
# ---------------------------------------------------------------------------
def render_overview(rows):
    total = sum(r["mar"] for r in rows)
    over = sum(1 for r in rows if r["status"] == "OVER")
    near = sum(1 for r in rows if r["status"] == "NEAR")
    with st.container(border=True):
        a, b, c, d = st.columns(4)
        a.metric("Connectors", f"{len(rows):,}")
        b.metric("Total MAR", f"{total:,}")
        c.metric("Over limit", over)
        d.metric(f"Near (≥{int(NEAR_THRESHOLD * 100)}%)", near)


def render_table(rows):
    """Exception-focused, sortable/filterable table. Scales from 5 to 500
    connectors: you see the at-risk ones first, not a wall of checkboxes."""
    st.subheader("Connectors — most at risk first")
    df = pd.DataFrame(rows)

    f1, f2, f3 = st.columns([1.3, 1.6, 2])
    view = f1.radio("Show", ["Exceptions", "Over", "Near", "All"], horizontal=True)
    schemas = sorted(df["schema"].unique())
    chosen = f2.multiselect("Schema", schemas, default=schemas)
    search = f3.text_input("Search connector", "")

    shown = df.copy()
    if view == "Exceptions":
        shown = shown[shown["status"].isin(["OVER", "NEAR"])]
    elif view == "Over":
        shown = shown[shown["status"] == "OVER"]
    elif view == "Near":
        shown = shown[shown["status"] == "NEAR"]
    shown = shown[shown["schema"].isin(chosen)]
    if search:
        shown = shown[shown["connector"].str.contains(search, case=False)]
    shown = shown.sort_values("risk", ascending=False)

    disp = pd.DataFrame({
        "Connector": shown["connector"],
        "Schema": shown["schema"],
        "Status": [{"OVER": "🔴 OVER", "NEAR": "🟠 NEAR", "OK": "🟢 OK"}[s]
                   for s in shown["status"]],
        "MAR this month": shown["mar"],
        "% of limit": (shown["pct"] * 100).round(0),
        "Limit": shown["limit"],
    })
    st.caption(f"Showing {len(disp):,} of {len(df):,} connectors.")
    st.dataframe(
        disp, use_container_width=True, hide_index=True, height=360,
        column_config={
            "MAR this month": st.column_config.NumberColumn(format="%d"),
            "Limit": st.column_config.NumberColumn(format="%d"),
            "% of limit": st.column_config.ProgressColumn(
                format="%d%%", min_value=0, max_value=150),
        },
    )


def run_pass(data, limits, channels, really_send, data_mode):
    """Evaluate EVERY connector (one pass) and act on the exceptions only —
    O(exceptions), not O(fleet). Reuses main.evaluate(), the same decision logic
    the unattended CLI uses, so the activity log matches a real run."""
    import main  # lazy: pulls in triggers/requests only when a pass runs

    REAL_DISPATCH = {"slack", "email", "webhook"}  # pause is always simulated here
    flat = flatten(data)
    connectors = [{"connection_name": c, "mar_limit": int(limits[c]), "triggers": channels}
                  for c in flat]

    state_mod.log_event(
        f"Guardrail pass ({data_mode}) — {len(connectors):,} connector(s)"
        + (" — LIVE dispatch" if really_send else ""),
        level=state_mod.INFO, source="run")

    results = main.evaluate(connectors, flat)
    over = [r for r in results if r["over"]]
    for r in over:
        name = r["connection_name"]
        if not channels:
            state_mod.log_event(f"{name} is over limit but no alert channels selected",
                                level=state_mod.LOG_WARN, source="run")
            continue
        connector = {"connection_name": name, "mar_limit": int(limits[name])}
        for channel in channels:
            if really_send and channel in REAL_DISPATCH:
                main._fire_trigger(channel, connector, r["current_mar"])
            else:
                note = ("pause is never fired from the console" if channel == "pause"
                        else "demo — not actually dispatched")
                state_mod.log_event(f"[{channel}] alert for '{name}' ({note})",
                                    level=state_mod.ACTION, source=channel)

    state_mod.log_event(
        f"Pass complete — acted on {len(over)} over-limit of {len(results):,} checked",
        level=state_mod.ALERT if over else state_mod.INFO, source="run")


def render_run_controls(data, limits, data_mode):
    st.subheader("Run")
    with st.container(border=True):
        channels = st.multiselect(
            "Alert channels to fire for over-limit connectors",
            ALERT_CHANNELS, default=["slack"])
        really_send = st.checkbox(
            "Actually send Slack / Email / Webhook alerts", key="really_send", value=False,
            help="On: those three channels really send using your config.py settings. "
                 "Off: everything is simulated. Pause is always simulated here.")
        if really_send:
            st.caption(":red[Live dispatch — Slack/Email/Webhook alerts will really "
                       "be sent using your configured channels.]")
        run_col, clear_col = st.columns(2)
        if run_col.button("Run guardrail pass", key="run_btn", type="primary",
                          use_container_width=True, disabled=not data):
            run_pass(data, limits, channels, really_send, data_mode)
        if clear_col.button("Clear log", key="clear_btn", use_container_width=True):
            state_mod.clear_activity_log()
            st.rerun()


def render_activity_log():
    st.subheader("Activity log")
    events = state_mod.activity_log(newest_first=True)
    if not events:
        st.caption("No activity yet — **Run guardrail pass**. Real `main.py` runs in "
                   "the same process appear here too.")
        return
    with st.container(height=300, border=True):
        for ev in events:
            st.markdown(f"{ACTIVITY_ICON.get(ev['level'], '•')} `{ev['ts']}` "
                        f"**[{ev['source']}]** {ev['message']}")


def render_debug_panel(data_mode, console_text, load_error):
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
st.set_page_config(page_title="MAR Guardrail", layout="wide")

st.session_state.setdefault("debug_open", False)
st.session_state.setdefault("demo_mode", False)   # LIVE is the default
st.session_state.setdefault("hatch_system", False)
st.session_state.setdefault("hatch_all_time", False)

st.title("MAR Guardrail")
st.caption("Current-month PAID MAR per connector — live from your warehouse, "
           "with limits and alerts. Surfaces exceptions first, scales to hundreds.")

_, controls = st.columns([3, 1])
with controls:
    st.toggle("Demo mode", key="demo_mode",
              help="Off = live data from your destination. On = bundled sample data.")
    if st.button("Debug", key="debug_btn", use_container_width=True):
        st.session_state["debug_open"] = not st.session_state["debug_open"]

data_mode = "demo" if st.session_state["demo_mode"] else "live"

# Debug escape-hatch toggles must be read before the load, so render them first.
debug_slot = st.container()
if st.session_state["debug_open"] and data_mode == "live":
    with debug_slot:
        with st.container(border=True):
            st.markdown("**Debug — live data escape hatch**")
            st.caption("The main view is always PAID + current month. These are for "
                       "testing on a free account (only SYSTEM rows exist).")
            h = st.columns(2)
            with h[0]:
                st.toggle("View SYSTEM rows", key="hatch_system")
            with h[1]:
                st.toggle("All time", key="hatch_all_time")

use_system = st.session_state["debug_open"] and data_mode == "live" and st.session_state["hatch_system"]
use_all_time = st.session_state["debug_open"] and data_mode == "live" and st.session_state["hatch_all_time"]

# Load data.
console_text, load_error = "", None
if data_mode == "demo":
    data = DEMO_MAR
else:
    data, load_error, console_text = load_live(("SYSTEM",) if use_system else ("PAID",), use_all_time)

if load_error:
    st.error(f"Couldn't load live MAR — {load_error}\n\nOpen **Debug** for the full "
             "system state and console, or switch on **Demo mode** to explore the layout.")
elif data_mode == "live" and not data:
    hint = ("" if use_system else " On a free Fivetran account there are no PAID rows — "
            "open **Debug** and enable *View SYSTEM rows* to see live data.")
    st.info(f"Connected, but no {'SYSTEM' if use_system else 'PAID'} MAR for the "
            f"selected window.{hint}")

# Limits + scored rows.
left, right = st.columns([1, 3])
with left:
    default_limit = st.number_input(
        "Default monthly MAR limit", min_value=1, step=1, value=DEFAULT_LIMIT,
        help="Applied to connectors without an explicit limit in config.py.")
limits = resolve_limits(data, int(default_limit))
rows = build_rows(data, limits)

render_overview(rows)
if rows:
    render_table(rows)
render_run_controls(data, limits, data_mode)
render_activity_log()

if st.session_state["debug_open"]:
    with debug_slot:
        render_debug_panel(data_mode, console_text, load_error)
