# app.py
#
# The operator console for the mar-guardrail framework.
#
# It is INVENTORY-FIRST: it starts from "here are all the connectors you have"
# (the roster Fivetran reports via the REST API), then hangs MAR and limits off
# each one and lets you turn connectors on/off (pause/resume). MAR comes from the
# Platform Connector's data in your warehouse; the roster and the on/off switches
# come from the Fivetran REST API. (See the PRD's "two surfaces" — read vs act.)
#
# Run with:  streamlit run app.py
#
# Built to scale: it surfaces the connectors that need attention first rather
# than making you scroll hundreds. For a self-contained teaching demo of anomaly
# detection see examples/anomaly_demo.py.

import io
from contextlib import redirect_stderr, redirect_stdout

import pandas as pd
import streamlit as st

import config
import state as state_mod

# --- Sample data for the Demo toggle ----------------------------------------
# A connector ROSTER (what Fivetran would report), incl. paused ones and one
# with no MAR — so Demo mode shows the inventory-first behavior end to end.
DEMO_CONNECTORS = [
    {"name": "salesforce_prod", "service": "salesforce", "paused": False},
    {"name": "salesforce_sandbox", "service": "salesforce", "paused": True},
    {"name": "postgres_analytics", "service": "postgres", "paused": False},
    {"name": "postgres_billing", "service": "postgres", "paused": False},
    {"name": "hubspot_marketing", "service": "hubspot", "paused": False},
    {"name": "stripe_payments", "service": "stripe", "paused": True},
    {"name": "netsuite_finance", "service": "netsuite", "paused": False},
    {"name": "zendesk_support", "service": "zendesk", "paused": False},  # no MAR
]
DEMO_MAR = {
    "salesforce_prod": 1_250_000, "salesforce_sandbox": 90_000,
    "postgres_analytics": 420_000, "postgres_billing": 510_000,
    "hubspot_marketing": 240_000, "stripe_payments": 75_000,
    "netsuite_finance": 980_000,
}

DEFAULT_LIMIT = 1_000_000
NEAR_THRESHOLD = 0.8

STATUS_ICON = {state_mod.OK: "✅", state_mod.WARN: "⚠️",
               state_mod.ERROR: "❌", state_mod.SKIP: "➖"}
ACTIVITY_ICON = {state_mod.INFO: "•", state_mod.ACTION: "✅", state_mod.ALERT: "🚨",
                 state_mod.LOG_WARN: "⚠️", state_mod.LOG_ERROR: "❌"}
ALERT_CHANNELS = ["pause", "slack", "email", "webhook"]


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------
def load_mar_live(free_types, all_time):
    """Read current-month MAR keyed by connection_name, capturing console output
    and any failure reason. Returns (mar_by_conn, error, console_text)."""
    from query import get_mar_by_schema  # lazy: Demo mode needs no DB

    buf = io.StringIO()
    mar, error = {}, None
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            grouped = get_mar_by_schema(free_types=free_types, all_time=all_time)
        mar = {conn: v for inst in grouped.values() for conn, v in inst.items()}
    except Exception as exc:  # noqa: BLE001 — surface the exact reason
        error = f"{type(exc).__name__}: {exc}"
    return mar, error, buf.getvalue()


def load_roster_live():
    """The connector inventory from the Fivetran REST API. Returns
    (roster_list_or_None, error). None means we couldn't list them (no creds /
    API error) — the dashboard then falls back to MAR-derived names."""
    from fivetran_api import check_api, list_connections  # lazy

    api = check_api()
    if not api["configured"]:
        return None, "Fivetran API not configured — add FIVETRAN_API_KEY/SECRET to list all connectors and toggle them."
    if not api["ok"]:
        return None, api["error"]
    try:
        return list_connections(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def resolve_limits(names, default_limit):
    """Per-connector limit from config.py where defined, else the UI default."""
    configured = {c["connection_name"]: c["mar_limit"] for c in config.CONNECTORS}
    return {n: configured.get(n, default_limit) for n in names}


def build_rows(roster, mar_by_conn, limits):
    """Join roster + MAR + limit into one scored row per connector. `paused` may
    be None when the roster is unknown (no API). `risk` floats exceptions up."""
    rows = []
    for c in roster:
        name = c["name"]
        mar = mar_by_conn.get(name, 0)
        limit = limits.get(name, 0)
        pct = mar / limit if limit else 0.0
        status = "OVER" if mar > limit else ("NEAR" if pct >= NEAR_THRESHOLD else "OK")
        risk = (1_000 if status == "OVER" else 0) + pct * 100
        rows.append({
            "connector": name, "service": c.get("service") or "—",
            "mar": mar, "limit": limit, "pct": pct, "status": status,
            "paused": c.get("paused"), "risk": risk,
        })
    return rows


# ---------------------------------------------------------------------------
# COMPONENTS
# ---------------------------------------------------------------------------
def render_overview(rows, on_off_known):
    total = sum(r["mar"] for r in rows)
    over = sum(1 for r in rows if r["status"] == "OVER")
    paused = sum(1 for r in rows if r["paused"]) if on_off_known else None
    with st.container(border=True):
        a, b, c, d = st.columns(4)
        a.metric("Connectors", f"{len(rows):,}")
        b.metric("Total MAR", f"{total:,}")
        c.metric("Over limit", over)
        d.metric("Paused", paused if paused is not None else "—")


def render_inventory(rows, on_off_known):
    """The inventory table. When the roster is known, the Active column is an
    editable on/off switch; returns the list of (name, new_paused) toggles the
    user made (applied by the caller)."""
    st.subheader("Connectors — your Fivetran inventory")
    df = pd.DataFrame(rows)

    f1, f2, f3 = st.columns([1.4, 1.6, 2])
    views = ["All", "Exceptions", "Over"] + (["Paused"] if on_off_known else [])
    view = f1.radio("Show", views, horizontal=True)
    services = sorted(df["service"].unique())
    chosen = f2.multiselect("Service", services, default=services)
    search = f3.text_input("Search connector", "")

    shown = df.copy()
    if view == "Exceptions":
        shown = shown[shown["status"].isin(["OVER", "NEAR"])]
    elif view == "Over":
        shown = shown[shown["status"] == "OVER"]
    elif view == "Paused":
        shown = shown[shown["paused"] == True]  # noqa: E712 — pandas mask
    shown = shown[shown["service"].isin(chosen)]
    if search:
        shown = shown[shown["connector"].str.contains(search, case=False)]
    shown = shown.sort_values("risk", ascending=False)
    st.caption(f"Showing {len(shown):,} of {len(df):,} connectors.")

    base = {
        "Connector": shown["connector"],
        "Service": shown["service"],
        "Status": [{"OVER": "🔴 OVER", "NEAR": "🟠 NEAR", "OK": "🟢 OK"}[s]
                   for s in shown["status"]],
        "MAR this month": shown["mar"],
        "% of limit": (shown["pct"] * 100).round(0),
        "Limit": shown["limit"],
    }
    col_cfg = {
        "MAR this month": st.column_config.NumberColumn(format="%d"),
        "Limit": st.column_config.NumberColumn(format="%d"),
        "% of limit": st.column_config.ProgressColumn(
            format="%d%%", min_value=0, max_value=150),
    }

    if not on_off_known:
        st.dataframe(pd.DataFrame(base), use_container_width=True,
                     hide_index=True, height=360, column_config=col_cfg)
        return []

    # Roster known -> editable on/off (Active = not paused) as the first column.
    disp = pd.DataFrame({"Active": [not p for p in shown["paused"]], **base})
    col_cfg["Active"] = st.column_config.CheckboxColumn(
        "Active", help="On = running, off = paused. Edit, then Apply below.")
    edited = st.data_editor(
        disp, key="inv_editor", hide_index=True, use_container_width=True, height=360,
        disabled=["Connector", "Service", "Status", "MAR this month", "% of limit", "Limit"],
        column_config=col_cfg,
    )
    current = {r["connector"]: r["paused"] for r in rows}
    changes = [(name, not active) for name, active in zip(edited["Connector"], edited["Active"])
               if current.get(name) != (not active)]
    return changes


def apply_onoff(changes, really_apply, data_mode):
    """Apply on/off toggles. Demo updates session state (simulated). Live either
    really calls the REST API (when armed) or logs a simulated intent."""
    for name, new_paused in changes:
        verb = "pause" if new_paused else "resume"
        target = "paused" if new_paused else "active"
        if data_mode == "demo":
            paused_set = st.session_state["demo_paused"]
            (paused_set.add if new_paused else paused_set.discard)(name)
            state_mod.log_event(f"[{verb}] {name} → {target} (demo)",
                                level=state_mod.ACTION, source=verb)
        elif really_apply:
            from fivetran_api import set_paused  # lazy
            ok = set_paused(name, new_paused)
            state_mod.log_event(
                f"[{verb}] {name} → {target} via REST API ({'ok' if ok else 'FAILED'})",
                level=state_mod.ACTION if ok else state_mod.LOG_ERROR, source=verb)
        else:
            state_mod.log_event(
                f"[{verb}] {name} → {target} (simulated — tick 'Actually apply' to send)",
                level=state_mod.ACTION, source=verb)


def run_pass(rows, limits, channels, really_send, data_mode):
    """Evaluate every connector and alert on the over-limit ones (acts on
    exceptions only). Reuses main.evaluate() — the same logic the CLI uses."""
    import main  # lazy

    REAL_DISPATCH = {"slack", "email", "webhook"}  # pause stays simulated here
    mar_by_conn = {r["connector"]: r["mar"] for r in rows}
    connectors = [{"connection_name": r["connector"], "mar_limit": int(limits[r["connector"]]),
                   "triggers": channels} for r in rows]
    state_mod.log_event(
        f"Guardrail pass ({data_mode}) — {len(connectors):,} connector(s)"
        + (" — LIVE dispatch" if really_send else ""),
        level=state_mod.INFO, source="run")

    results = main.evaluate(connectors, mar_by_conn)
    over = [r for r in results if r["over"]]
    for r in over:
        name = r["connection_name"]
        if not channels:
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


def render_activity_log():
    st.subheader("Activity log")
    events = state_mod.activity_log(newest_first=True)
    if not events:
        st.caption("No activity yet — toggle connectors on/off, or **Run guardrail pass**.")
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
# Open in Demo mode when there's no database configured, so a fresh checkout
# lands on a working demo instead of a connection error. Configure DATABASE_URL
# (and Fivetran creds) and it defaults to live.
st.session_state.setdefault("demo_mode", not config.config_state()["database_url_set"])
st.session_state.setdefault("hatch_system", False)
st.session_state.setdefault("hatch_all_time", False)
st.session_state.setdefault("demo_paused", {c["name"] for c in DEMO_CONNECTORS if c["paused"]})

st.title("MAR Guardrail")
st.caption("Your Fivetran connectors — current-month PAID MAR vs limits, with "
           "on/off control. Roster + switches from the REST API, MAR from the "
           "Platform Connector. Exceptions first; scales to hundreds.")

_, controls = st.columns([3, 1])
with controls:
    st.toggle("Demo mode", key="demo_mode",
              help="Off = live (your Fivetran account). On = bundled sample roster.")
    if st.button("Debug", key="debug_btn", use_container_width=True):
        st.session_state["debug_open"] = not st.session_state["debug_open"]

data_mode = "demo" if st.session_state["demo_mode"] else "live"

debug_slot = st.container()
if st.session_state["debug_open"] and data_mode == "live":
    with debug_slot:
        with st.container(border=True):
            st.markdown("**Debug — live MAR escape hatch**")
            st.caption("MAR view is PAID + current month. On a free account "
                       "(only SYSTEM rows) flip these to see live MAR.")
            h = st.columns(2)
            with h[0]:
                st.toggle("View SYSTEM rows", key="hatch_system")
            with h[1]:
                st.toggle("All time", key="hatch_all_time")

use_system = st.session_state["debug_open"] and data_mode == "live" and st.session_state["hatch_system"]
use_all_time = st.session_state["debug_open"] and data_mode == "live" and st.session_state["hatch_all_time"]

# --- Load roster + MAR ------------------------------------------------------
console_text, load_error, roster_note = "", None, None
if data_mode == "demo":
    roster = [{"name": c["name"], "service": c["service"],
               "paused": c["name"] in st.session_state["demo_paused"]}
              for c in DEMO_CONNECTORS]
    mar_by_conn = DEMO_MAR
    on_off_known = True
else:
    mar_by_conn, load_error, console_text = load_mar_live(
        ("SYSTEM",) if use_system else ("PAID",), use_all_time)
    roster, roster_note = load_roster_live()
    on_off_known = roster is not None
    if roster is None:
        # No live roster — fall back to whatever names MAR gave us (read-only).
        roster = [{"name": n, "service": "—", "paused": None} for n in sorted(mar_by_conn)]

if load_error:
    st.error(f"Couldn't load live MAR — {load_error}\n\nOpen **Debug** for the full "
             "system state, or switch on **Demo mode**.")
if roster_note:
    st.info(roster_note)
elif data_mode == "live" and not roster and not load_error:
    st.info("Connected, but no connectors/MAR for the selected window. On a free "
            "account, open **Debug** and enable *View SYSTEM rows*.")

# --- Limits + rows ----------------------------------------------------------
left, _ = st.columns([1, 3])
with left:
    default_limit = st.number_input(
        "Default monthly MAR limit", min_value=1, step=1, value=DEFAULT_LIMIT,
        help="Applied to connectors without an explicit limit in config.py.")
limits = resolve_limits([c["name"] for c in roster], int(default_limit))
rows = build_rows(roster, mar_by_conn, limits)

render_overview(rows, on_off_known)

changes = []
if rows:
    changes = render_inventory(rows, on_off_known)

# --- On/off apply -----------------------------------------------------------
if on_off_known:
    with st.container(border=True):
        really_apply = st.checkbox(
            "Actually apply on/off changes to Fivetran (live REST API)",
            key="really_apply", value=False,
            help="Off = simulated (logged only). On (live mode) = real pause/resume "
                 "via the Fivetran REST API.")
        if really_apply and data_mode == "live":
            st.caption(":red[Live — toggling Active will really pause/resume the "
                       "connector in Fivetran.]")
        label = f"Apply on/off changes ({len(changes)})" if changes else "Apply on/off changes"
        if st.button(label, type="primary", disabled=not changes):
            apply_onoff(changes, really_apply, data_mode)
            st.session_state.pop("inv_editor", None)  # reset editor to new state
            st.rerun()

# --- Run guardrail pass (alerts on over-limit) ------------------------------
st.subheader("Run guardrail pass")
with st.container(border=True):
    channels = st.multiselect("Alert channels for over-limit connectors",
                              ALERT_CHANNELS, default=["slack"])
    really_send = st.checkbox("Actually send Slack / Email / Webhook alerts",
                              key="really_send", value=False)
    rc, cc = st.columns(2)
    if rc.button("Run guardrail pass", type="primary", use_container_width=True, disabled=not rows):
        run_pass(rows, limits, channels, really_send, data_mode)
        st.rerun()
    if cc.button("Clear log", use_container_width=True):
        state_mod.clear_activity_log()
        st.rerun()

render_activity_log()

if st.session_state["debug_open"]:
    with debug_slot:
        render_debug_panel(data_mode, console_text, load_error)
