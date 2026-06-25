# app.py
#
# The operator console — deliberately simple. For each Fivetran connector you see
# its month-to-date MAR vs its limit, and you set ONE thing: what the guardrail
# does when it goes over —
#
#     Warn          — send an alert
#     Pause          — stop the connector
#     Warn & Pause   — both
#
# Plus a manual on/off (pause/start) per connector. "Run guardrail" executes the
# chosen action on whatever's over its limit. That's the whole app.
#
#   MAR + anomalies  -> the Platform Connector's data in your warehouse
#   roster + on/off  -> the Fivetran REST API
#
# Run with:  streamlit run app.py   (Demo mode is the default with no DB set.)

import io
from contextlib import redirect_stderr, redirect_stdout

import altair as alt
import pandas as pd
import streamlit as st

import config
import detection
import state as state_mod

DEMO_CONNECTORS = [
    {"name": "salesforce_prod", "paused": False}, {"name": "salesforce_sandbox", "paused": True},
    {"name": "postgres_analytics", "paused": False}, {"name": "postgres_billing", "paused": False},
    {"name": "hubspot_marketing", "paused": False}, {"name": "stripe_payments", "paused": True},
    {"name": "netsuite_finance", "paused": False}, {"name": "zendesk_support", "paused": False},
]
DEMO_MAR = {
    "salesforce_prod": 1_250_000, "salesforce_sandbox": 90_000, "postgres_analytics": 420_000,
    "postgres_billing": 510_000, "hubspot_marketing": 240_000, "stripe_payments": 75_000,
    "netsuite_finance": 980_000,
}
DEMO_SPIKES = {"postgres_billing": (13, 9.0), "hubspot_marketing": (15, 11.0)}
DEMO_DAYS = 18

DEFAULT_LIMIT = 1_000_000
NEAR_THRESHOLD = 0.8
ANOMALY = (14, 5.0, 3.0)            # fixed anomaly sensitivity (window, z, ×base)
ACTIONS = ["Warn", "Pause", "Warn & Pause"]

STATUS_ICON = {state_mod.OK: "✅", state_mod.WARN: "⚠️", state_mod.ERROR: "❌", state_mod.SKIP: "➖"}
ACTIVITY_ICON = {state_mod.INFO: "•", state_mod.ACTION: "✅", state_mod.ALERT: "🚨",
                 state_mod.LOG_WARN: "⚠️", state_mod.LOG_ERROR: "❌"}


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------
def demo_daily():
    series = {}
    for i, (name, total) in enumerate(DEMO_MAR.items()):
        base = max(total // DEMO_DAYS, 1)
        spike_day, mult = DEMO_SPIKES.get(name, (None, None))
        series[name] = detection.generate_series(
            DEMO_DAYS, baseline=base, noise=0.08, spike_day=spike_day,
            spike_mar=int(base * mult) if spike_day is not None else None, seed=1000 + i)
    return series


def load_daily_live(free_types, all_time):
    from query import get_daily_mar  # lazy

    buf = io.StringIO()
    daily, error = {}, None
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            daily = get_daily_mar(free_types=free_types, all_time=all_time)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    return daily, error, buf.getvalue()


def load_roster_live():
    from fivetran_api import check_api, list_connections  # lazy

    api = check_api()
    if not api["configured"]:
        return None, "Fivetran API not configured — add FIVETRAN_API_KEY/SECRET to list connectors and toggle them."
    if not api["ok"]:
        return None, api["error"]
    try:
        return list_connections(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def effective_limits(names):
    cfg = {c["connection_name"]: c["mar_limit"] for c in config.CONNECTORS}
    ov = st.session_state["limit_overrides"]
    return {n: int(ov.get(n, cfg.get(n, DEFAULT_LIMIT))) for n in names}


def build_rows(roster, daily, limits):
    window, z, mult = ANOMALY
    rows, points_by_conn = [], {}
    for c in roster:
        name = c["name"]
        series = daily.get(name, [])
        limit = limits.get(name, 0)
        if series:
            pts, summ = detection.detect(series, limit, window=window, z_threshold=z, min_multiple=mult)
            mtd, pct, status, anomaly = summ["mtd"], summ["pct"], summ["status"], bool(summ["anomalies"])
        else:
            pts, mtd, pct, status, anomaly = [], 0, 0.0, "OK", False
        points_by_conn[name] = pts
        risk = (2_000 if anomaly else 0) + (1_000 if status == "OVER" else 0) + pct * 100
        rows.append({"connector": name, "mar": mtd, "limit": limit, "status": status,
                     "paused": c.get("paused"), "anomaly": anomaly, "risk": risk})
    return rows, points_by_conn


def render_chart(rows, points_by_conn):
    """Per-connector daily MAR with anomalies flagged + the trailing baseline."""
    names = [r["connector"] for r in sorted(rows, key=lambda r: r["risk"], reverse=True)
             if points_by_conn.get(r["connector"])]
    if not names:
        st.caption("No daily history to chart.")
        return
    pick = st.selectbox("Connector", names, key="chart_pick")
    pts = points_by_conn[pick]
    cdf = pd.DataFrame([{"date": p.day, "MAR": p.mar, "baseline": p.baseline, "anomaly": p.is_anomaly}
                        for p in pts])
    bars = alt.Chart(cdf).mark_bar().encode(
        x=alt.X("date:T", title="Day"), y=alt.Y("MAR:Q", title="PAID MAR / day"),
        color=alt.condition(alt.datum.anomaly, alt.value("#d62728"), alt.value("#4c78a8")),
        tooltip=["date:T", "MAR:Q", alt.Tooltip("baseline:Q", format=",.0f")])
    line = alt.Chart(cdf).mark_line(color="#999", strokeDash=[4, 4]).encode(x="date:T", y="baseline:Q")
    st.altair_chart(bars + line, use_container_width=True)
    st.caption("Blue = normal day · red = anomaly · dashed = trailing baseline.")


# ---------------------------------------------------------------------------
# ACTIONS — the three responses
# ---------------------------------------------------------------------------
def _toggle(name, paused, live, data_mode):
    verb, target = ("pause", "paused") if paused else ("resume", "active")
    if data_mode == "demo":
        (st.session_state["demo_paused"].add if paused else st.session_state["demo_paused"].discard)(name)
        state_mod.log_event(f"{name} → {target} (demo)", level=state_mod.ACTION, source=verb)
    elif live:
        from fivetran_api import set_paused  # lazy
        ok = set_paused(name, paused)
        state_mod.log_event(f"{name} → {target} via Fivetran ({'ok' if ok else 'FAILED'})",
                            level=state_mod.ACTION if ok else state_mod.LOG_ERROR, source=verb)
    else:
        state_mod.log_event(f"{name} → {target} (preview — tick Live to apply)",
                            level=state_mod.ACTION, source=verb)


def _warn(name, mar, limit, live, data_mode):
    if data_mode == "live" and live:
        import main  # lazy
        fired = [ch for ch in ("slack", "email", "webhook") if config.config_state()[f"{ch}_set"]]
        if fired:
            for ch in fired:
                main._fire_trigger(ch, {"connection_name": name, "mar_limit": limit}, mar)
        else:
            state_mod.log_event(f"warn {name}: over limit, but no alert channels configured",
                                level=state_mod.LOG_WARN, source="warn")
    else:
        state_mod.log_event(f"warn {name}: {mar:,} / {limit:,} over limit (preview)",
                            level=state_mod.ALERT, source="warn")


def run_guardrail(rows, actions, live, data_mode):
    over = [r for r in rows if r["status"] == "OVER"]
    state_mod.log_event(f"Guardrail run — {len(over)} connector(s) over limit", level=state_mod.INFO, source="run")
    for r in over:
        action = actions.get(r["connector"], "Warn")
        if action in ("Warn", "Warn & Pause"):
            _warn(r["connector"], r["mar"], r["limit"], live, data_mode)
        if action in ("Pause", "Warn & Pause"):
            _toggle(r["connector"], True, live, data_mode)


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
st.session_state.setdefault("demo_mode", not config.config_state()["database_url_set"])
st.session_state.setdefault("hatch_system", False)
st.session_state.setdefault("hatch_all_time", False)
st.session_state.setdefault("demo_paused", {c["name"] for c in DEMO_CONNECTORS if c["paused"]})
st.session_state.setdefault("limit_overrides", {})
st.session_state.setdefault("actions", {})

st.title("MAR Guardrail")
st.caption("For each connector, set its MAR **limit** and what to do **when it's "
           "over** — **Warn**, **Pause**, or **Warn & Pause**. Both are configurable "
           "per connector. Demo mode runs with no setup.")

_, controls = st.columns([3, 1])
with controls:
    st.toggle("Demo mode", key="demo_mode")
    if st.button("Debug", key="debug_btn", use_container_width=True):
        st.session_state["debug_open"] = not st.session_state["debug_open"]

data_mode = "demo" if st.session_state["demo_mode"] else "live"

debug_slot = st.container()
if st.session_state["debug_open"] and data_mode == "live":
    with debug_slot:
        with st.container(border=True):
            st.caption("MAR view is PAID + current month. On a free account flip these to see SYSTEM rows.")
            h = st.columns(2)
            with h[0]:
                st.toggle("View SYSTEM rows", key="hatch_system")
            with h[1]:
                st.toggle("All time", key="hatch_all_time")

use_system = st.session_state["debug_open"] and data_mode == "live" and st.session_state["hatch_system"]
use_all_time = st.session_state["debug_open"] and data_mode == "live" and st.session_state["hatch_all_time"]

# --- Load -------------------------------------------------------------------
console_text, load_error, roster_note = "", None, None
if data_mode == "demo":
    roster = [{"name": c["name"], "paused": c["name"] in st.session_state["demo_paused"]}
              for c in DEMO_CONNECTORS]
    daily = demo_daily()
    on_off_known = True
else:
    daily, load_error, console_text = load_daily_live(("SYSTEM",) if use_system else ("PAID",), use_all_time)
    roster, roster_note = load_roster_live()
    on_off_known = roster is not None
    if roster is None:
        roster = [{"name": n, "paused": None} for n in sorted(daily)]

if load_error:
    st.error(f"Couldn't load live MAR — {load_error}\n\nOpen **Debug**, or switch on **Demo mode**.")
if roster_note:
    st.info(roster_note)
elif data_mode == "live" and not roster and not load_error:
    st.info("Connected, but no connectors/MAR yet. Open **Debug** and enable *View SYSTEM rows*.")

limits = effective_limits([c["name"] for c in roster])
rows, points_by_conn = build_rows(roster, daily, limits)

over = sum(1 for r in rows if r["status"] == "OVER")
anom = sum(1 for r in rows if r["anomaly"])
summary = f"**{len(rows)}** connectors · **{over}** over limit · **{anom}** spiking"
if on_off_known:
    summary += f" · **{sum(1 for r in rows if r['paused'])}** paused"
st.markdown(summary)

# --- The one table ----------------------------------------------------------
active_changes, limit_changes, actions_now = [], {}, {}
if rows:
    search = st.text_input("Search", "", placeholder="Filter connectors…", label_visibility="collapsed")
    shown = [r for r in sorted(rows, key=lambda r: r["risk"], reverse=True)
             if search.lower() in r["connector"].lower()]

    def status_label(r):
        base = {"OVER": "🔴 OVER", "NEAR": "🟠 NEAR", "OK": "🟢 OK"}[r["status"]]
        return base + ("  🚨 spike" if r["anomaly"] else "")

    cols = {
        "Connector": [r["connector"] for r in shown],
        "MAR this month": [r["mar"] for r in shown],
        "Limit": [r["limit"] for r in shown],
        "Status": [status_label(r) for r in shown],
        "When over": [st.session_state["actions"].get(r["connector"], "Warn") for r in shown],
    }
    col_cfg = {
        "MAR this month": st.column_config.NumberColumn(format="%d"),
        "Limit": st.column_config.NumberColumn(format="%d", min_value=1, step=1000,
                                               help="Editable — the monthly MAR ceiling."),
        "When over": st.column_config.SelectboxColumn(
            "When over", options=ACTIONS, required=True,
            help="What the guardrail does when this connector is over its limit."),
    }
    disabled = ["Connector", "MAR this month", "Status"]
    if on_off_known:
        cols = {"On": [not r["paused"] for r in shown], **cols}
        col_cfg["On"] = st.column_config.CheckboxColumn("On", help="On = running, off = paused.")

    st.caption("Configure per connector: **Limit** (the MAR ceiling) and **When over** "
               "(Warn / Pause / Warn & Pause) are editable — change them, then **Apply edits**.")
    edited = st.data_editor(pd.DataFrame(cols), key="tbl", hide_index=True,
                            use_container_width=True, height=380, disabled=disabled, column_config=col_cfg)

    cur_limit = {r["connector"]: r["limit"] for r in rows}
    limit_changes = {n: int(v) for n, v in zip(edited["Connector"], edited["Limit"])
                     if int(v) > 0 and int(v) != cur_limit.get(n)}
    actions_now = dict(zip(edited["Connector"], edited["When over"]))
    if on_off_known:
        cur_paused = {r["connector"]: r["paused"] for r in rows}
        active_changes = [(n, not on) for n, on in zip(edited["Connector"], edited["On"])
                          if cur_paused.get(n) != (not on)]

# --- Actions ----------------------------------------------------------------
live = bool(data_mode == "live" and on_off_known and st.checkbox(
    "Live — actually pause/resume & send alerts in Fivetran", key="really_apply",
    help="Off = preview (logged only). On = real, via the REST API / your channels."))

b1, b2 = st.columns(2)
n_edits = len(active_changes) + len(limit_changes)
if b1.button(f"Apply edits ({n_edits})" if n_edits else "Apply edits", disabled=not n_edits,
             use_container_width=True):
    st.session_state["limit_overrides"].update(limit_changes)
    st.session_state["actions"].update(actions_now)
    for name, paused in active_changes:
        _toggle(name, paused, live, data_mode)
    st.session_state.pop("tbl", None)
    st.rerun()
if b2.button("Run guardrail", type="primary", disabled=not rows, use_container_width=True):
    st.session_state["actions"].update(actions_now)
    run_guardrail(rows, st.session_state["actions"], live, data_mode)
    st.rerun()

# --- Daily MAR & anomalies (tucked) -----------------------------------------
if rows:
    with st.expander("Daily MAR & anomalies"):
        render_chart(rows, points_by_conn)

# --- Recent activity (tucked) -----------------------------------------------
events = state_mod.activity_log(newest_first=True)
with st.expander(f"Recent activity ({len(events)})"):
    if not events:
        st.caption("Nothing yet — edit a limit / action and **Apply edits**, or **Run guardrail**.")
    else:
        if st.button("Clear"):
            state_mod.clear_activity_log()
            st.rerun()
        for ev in events[:50]:
            st.markdown(f"{ACTIVITY_ICON.get(ev['level'], '•')} `{ev['ts']}` {ev['message']}")

if st.session_state["debug_open"]:
    with debug_slot:
        render_debug_panel(data_mode, console_text, load_error)
