# app.py
#
# The operator console for the mar-guardrail framework.
#
# INVENTORY-FIRST: it starts from "here are all the connectors you have" (the
# roster Fivetran reports via the REST API), then hangs MAR, limits, on/off, and
# anomaly detection off each one. MAR comes from the Platform Connector's data
# in your warehouse; the roster + on/off switches come from the Fivetran REST
# API. (See the PRD's "two surfaces" — read vs act.)
#
# Two detection signals, both shown here:
#   - budget   : month-to-date MAR vs an (editable) limit -> OVER / NEAR / OK
#   - anomaly  : a day's MAR vs the connector's own recent baseline -> early
#                warning, even while the monthly total is still fine
#
# Run with:  streamlit run app.py
# Demo mode (default when no DB is configured) runs the whole thing — including
# anomaly detection — on a generated sample, so you can see it with zero setup.

import io
from contextlib import redirect_stderr, redirect_stdout

import altair as alt
import pandas as pd
import streamlit as st

import config
import detection
import state as state_mod

# --- Sample data for the Demo toggle ----------------------------------------
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
# Connectors that get an injected spike in Demo mode -> {name: (spike_day, ×base)}.
# postgres_billing stays UNDER budget (early-warning case); hubspot is pushed OVER.
DEMO_SPIKES = {"postgres_billing": (13, 9.0), "hubspot_marketing": (15, 11.0)}
DEMO_DAYS = 18

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
def demo_daily():
    """Generated daily MAR per connector (mirrors incremental_mar's shape), with
    a couple of injected spikes so Demo mode exercises anomaly detection."""
    series = {}
    for i, (name, total) in enumerate(DEMO_MAR.items()):
        base = max(total // DEMO_DAYS, 1)
        spike_day, mult = DEMO_SPIKES.get(name, (None, None))
        series[name] = detection.generate_series(
            DEMO_DAYS, baseline=base, noise=0.08, spike_day=spike_day,
            spike_mar=int(base * mult) if spike_day is not None else None, seed=1000 + i)
    return series


def load_daily_live(free_types, all_time):
    """Per-day MAR from the warehouse, capturing console + any failure reason.
    Returns (daily_by_conn, error, console_text)."""
    from query import get_daily_mar  # lazy: Demo mode needs no DB

    buf = io.StringIO()
    daily, error = {}, None
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            daily = get_daily_mar(free_types=free_types, all_time=all_time)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    return daily, error, buf.getvalue()


def load_roster_live():
    """Connector inventory from the Fivetran REST API. (roster_or_None, error)."""
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


def effective_limits(names, default_limit):
    """Limit precedence: in-session edit > config.py > UI default."""
    cfg = {c["connection_name"]: c["mar_limit"] for c in config.CONNECTORS}
    ov = st.session_state["limit_overrides"]
    return {n: int(ov.get(n, cfg.get(n, default_limit))) for n in names}


def build(roster, daily, limits, knobs):
    """Score every connector with both signals. Returns (rows, points_by_conn)."""
    window, z, mult = knobs
    rows, points_by_conn = [], {}
    for c in roster:
        name = c["name"]
        series = daily.get(name, [])
        limit = limits.get(name, 0)
        if series:
            pts, summ = detection.detect(series, limit, window=window,
                                         z_threshold=z, min_multiple=mult)
            mtd, pct, status = summ["mtd"], summ["pct"], summ["status"]
            anoms = summ["anomalies"]
            worst = max((p.multiple for p in anoms), default=0.0)
        else:
            pts, mtd, pct, status, anoms, worst = [], 0, 0.0, "OK", [], 0.0
        points_by_conn[name] = pts
        anomaly = bool(anoms)
        risk = (2_000 if anomaly else 0) + (1_000 if status == "OVER" else 0) + pct * 100
        rows.append({
            "connector": name, "service": c.get("service") or "—", "mar": mtd,
            "limit": limit, "pct": pct, "status": status, "paused": c.get("paused"),
            "anomaly": anomaly, "worst": worst, "risk": risk,
        })
    return rows, points_by_conn


# ---------------------------------------------------------------------------
# COMPONENTS
# ---------------------------------------------------------------------------
def render_overview(rows, on_off_known):
    total = sum(r["mar"] for r in rows)
    with st.container(border=True):
        a, b, c, d = st.columns(4)
        a.metric("Total MAR", f"{total:,}")
        b.metric("Over limit", sum(1 for r in rows if r["status"] == "OVER"))
        c.metric("Anomalies", sum(1 for r in rows if r["anomaly"]))
        paused = sum(1 for r in rows if r["paused"]) if on_off_known else None
        d.metric("Paused", paused if paused is not None else "—")


def render_inventory(rows, on_off_known):
    """Inventory table with editable Active (on/off) and Limit columns. Returns
    (active_changes, limit_changes) for the caller to apply."""
    st.subheader("Connectors")
    df = pd.DataFrame(rows)

    f1, f2 = st.columns([2, 2])
    views = ["All", "Exceptions", "Over", "Anomalies"] + (["Paused"] if on_off_known else [])
    view = f1.radio("Show", views, horizontal=True)
    search = f2.text_input("Search connector", "")

    shown = df.copy()
    if view == "Exceptions":
        shown = shown[(shown["status"].isin(["OVER", "NEAR"])) | (shown["anomaly"])]
    elif view == "Over":
        shown = shown[shown["status"] == "OVER"]
    elif view == "Anomalies":
        shown = shown[shown["anomaly"]]
    elif view == "Paused":
        shown = shown[shown["paused"] == True]  # noqa: E712
    if search:
        shown = shown[shown["connector"].str.contains(search, case=False)]
    shown = shown.sort_values("risk", ascending=False)
    st.caption(f"Showing {len(shown):,} of {len(df):,} connectors.")

    base = {
        "Connector": shown["connector"],
        "Service": shown["service"],
        "Status": [{"OVER": "🔴 OVER", "NEAR": "🟠 NEAR", "OK": "🟢 OK"}[s]
                   for s in shown["status"]],
        "Anomaly": [f"🚨 {w:.0f}×" if a else "" for a, w in zip(shown["anomaly"], shown["worst"])],
        "MAR this month": shown["mar"],
        "% of limit": (shown["pct"] * 100).round(0),
        "Limit": shown["limit"],
    }
    col_cfg = {
        "MAR this month": st.column_config.NumberColumn(format="%d"),
        "% of limit": st.column_config.ProgressColumn(format="%d%%", min_value=0, max_value=150),
        "Limit": st.column_config.NumberColumn(
            format="%d", min_value=1, step=1000,
            help="Editable — the monthly MAR ceiling. Edit, then Apply below."),
    }
    disabled = ["Connector", "Service", "Status", "Anomaly", "MAR this month", "% of limit"]

    if on_off_known:
        disp = pd.DataFrame({"Active": [not p for p in shown["paused"]], **base})
        col_cfg["Active"] = st.column_config.CheckboxColumn(
            "Active", help="On = running, off = paused. Edit, then Apply below.")
    else:
        disp = pd.DataFrame(base)

    edited = st.data_editor(
        disp, key="inv_editor", hide_index=True, use_container_width=True, height=360,
        disabled=disabled, column_config=col_cfg)

    cur_limit = {r["connector"]: r["limit"] for r in rows}
    limit_changes = {name: int(lim) for name, lim in zip(edited["Connector"], edited["Limit"])
                     if int(lim) > 0 and int(lim) != cur_limit.get(name)}
    active_changes = []
    if on_off_known:
        cur_paused = {r["connector"]: r["paused"] for r in rows}
        active_changes = [(name, not active)
                          for name, active in zip(edited["Connector"], edited["Active"])
                          if cur_paused.get(name) != (not active)]
    return active_changes, limit_changes


def render_chart(rows, points_by_conn):
    """Per-connector daily MAR with anomalies flagged + the trailing baseline."""
    names = [r["connector"] for r in sorted(rows, key=lambda r: r["risk"], reverse=True)
             if points_by_conn.get(r["connector"])]
    if not names:
        st.caption("No daily history to chart (a connector with no MAR has nothing to show).")
        return
    pick = st.selectbox("Connector", names)
    pts = points_by_conn[pick]
    cdf = pd.DataFrame([{"date": p.day, "MAR": p.mar, "baseline": p.baseline,
                         "anomaly": p.is_anomaly} for p in pts])
    bars = alt.Chart(cdf).mark_bar().encode(
        x=alt.X("date:T", title="Day"), y=alt.Y("MAR:Q", title="PAID MAR / day"),
        color=alt.condition(alt.datum.anomaly, alt.value("#d62728"), alt.value("#4c78a8")),
        tooltip=["date:T", "MAR:Q", alt.Tooltip("baseline:Q", format=",.0f")])
    line = alt.Chart(cdf).mark_line(color="#999", strokeDash=[4, 4]).encode(
        x="date:T", y="baseline:Q")
    st.altair_chart(bars + line, use_container_width=True)
    st.caption("Blue = normal day · red = flagged anomaly · dashed = trailing baseline.")


def apply_onoff(changes, really_apply, data_mode):
    for name, new_paused in changes:
        verb, target = ("pause", "paused") if new_paused else ("resume", "active")
        if data_mode == "demo":
            (st.session_state["demo_paused"].add if new_paused
             else st.session_state["demo_paused"].discard)(name)
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
    """Evaluate every connector and act on the exceptions: budget alerts on the
    over-limit ones (via main.evaluate) plus an early-warning log for anomalies."""
    import main  # lazy

    REAL_DISPATCH = {"slack", "email", "webhook"}
    mar_by_conn = {r["connector"]: r["mar"] for r in rows}
    connectors = [{"connection_name": r["connector"], "mar_limit": int(limits[r["connector"]]),
                   "triggers": channels} for r in rows]
    state_mod.log_event(
        f"Guardrail pass ({data_mode}) — {len(connectors):,} connector(s)"
        + (" — LIVE dispatch" if really_send else ""), level=state_mod.INFO, source="run")

    results = main.evaluate(connectors, mar_by_conn)
    over = [r for r in results if r["over"]]
    for r in over:
        name = r["connection_name"]
        for channel in channels:
            if really_send and channel in REAL_DISPATCH:
                main._fire_trigger(channel, {"connection_name": name,
                                             "mar_limit": int(limits[name])}, r["current_mar"])
            else:
                note = ("pause is never fired from the console" if channel == "pause"
                        else "demo — not actually dispatched")
                state_mod.log_event(f"[{channel}] alert for '{name}' ({note})",
                                    level=state_mod.ACTION, source=channel)
    for r in rows:
        if r["anomaly"]:
            state_mod.log_event(
                f"{r['connector']}: daily MAR {r['worst']:.0f}× its baseline — ANOMALY (early warning)",
                level=state_mod.ALERT, source="anomaly")

    state_mod.log_event(
        f"Pass complete — {len(over)} over-limit, "
        f"{sum(1 for r in rows if r['anomaly'])} anomalous of {len(results):,} checked",
        level=state_mod.ALERT if (over or any(r["anomaly"] for r in rows)) else state_mod.INFO,
        source="run")


def render_activity_log():
    st.subheader("Activity log")
    events = state_mod.activity_log(newest_first=True)
    if not events:
        st.caption("No activity yet — toggle connectors on/off, edit a limit, or **Run guardrail pass**.")
        return
    with st.container(height=300, border=True):
        for ev in events:
            st.markdown(f"{ACTIVITY_ICON.get(ev['level'], '•')} `{ev['ts']}` "
                        f"**[{ev['source']}]** {ev['message']}")


def render_settings(data_mode):
    """One collapsed home for the knobs that used to clutter the main page:
    default limit, anomaly sensitivity, alert channels, and the safety gates.
    Returns everything the page needs. Rendered before the table so the knobs
    take effect on this run."""
    with st.expander("Settings & alerts"):
        a, b = st.columns(2)
        default_limit = a.number_input(
            "Default monthly MAR limit", min_value=1, step=1000, value=DEFAULT_LIMIT,
            help="For connectors without a config.py limit or an inline edit.")
        channels = b.multiselect("Alert channels for over-limit connectors",
                                 ALERT_CHANNELS, default=["slack"])
        st.caption(
            "Limits are in **MAR**, not dollars. Pricing is tiered and annual "
            "commitments are discounted, so cost is specific to your plan — use "
            "[Fivetran's pricing estimator](https://www.fivetran.com/pricing) to "
            "decide what MAR level you want and what it costs you.")
        st.caption("Anomaly detection sensitivity (OPEN-8)")
        k1, k2, k3 = st.columns(3)
        window = k1.slider("Baseline window (days)", 3, 21, 14)
        z_threshold = k2.slider("Min z-score", 2.0, 10.0, 5.0, step=0.5)
        min_multiple = k3.slider("Min × baseline", 1.5, 10.0, 3.0, step=0.5)
        g1, g2 = st.columns(2)
        really_apply = g1.checkbox(
            "Actually apply on/off to Fivetran (live REST API)", key="really_apply",
            help="Off = simulated. On (live) = real pause/resume.")
        really_send = g2.checkbox(
            "Actually send Slack / Email / Webhook alerts", key="really_send")
        if data_mode == "live" and (really_apply or really_send):
            st.caption(":red[Live dispatch armed — actions will really hit Fivetran / your channels.]")
    return int(default_limit), (window, z_threshold, min_multiple), channels, really_send, really_apply


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

st.title("MAR Guardrail")
st.caption("Your Fivetran connectors — MAR vs limits, anomalies, and on/off. "
           "Demo mode runs it all with no setup.")

_, controls = st.columns([3, 1])
with controls:
    st.toggle("Demo mode", key="demo_mode",
              help="Off = live (your Fivetran account). On = generated sample, "
                   "including daily history so anomaly detection runs.")
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

# --- Load roster + daily MAR ------------------------------------------------
console_text, load_error, roster_note = "", None, None
if data_mode == "demo":
    roster = [{"name": c["name"], "service": c["service"],
               "paused": c["name"] in st.session_state["demo_paused"]}
              for c in DEMO_CONNECTORS]
    daily = demo_daily()
    on_off_known = True
else:
    daily, load_error, console_text = load_daily_live(
        ("SYSTEM",) if use_system else ("PAID",), use_all_time)
    roster, roster_note = load_roster_live()
    on_off_known = roster is not None
    if roster is None:
        roster = [{"name": n, "service": "—", "paused": None} for n in sorted(daily)]

if load_error:
    st.error(f"Couldn't load live MAR — {load_error}\n\nOpen **Debug** for the full "
             "system state, or switch on **Demo mode**.")
if roster_note:
    st.info(roster_note)
elif data_mode == "live" and not roster and not load_error:
    st.info("Connected, but no connectors/MAR for the selected window. On a free "
            "account, open **Debug** and enable *View SYSTEM rows*.")

# Collapsed settings drive limits, anomaly sensitivity, channels, and gates.
default_limit, knobs, channels, really_send, really_apply = render_settings(data_mode)

limits = effective_limits([c["name"] for c in roster], default_limit)
rows, points_by_conn = build(roster, daily, limits, knobs)

render_overview(rows, on_off_known)

active_changes, limit_changes = ([], {})
if rows:
    active_changes, limit_changes = render_inventory(rows, on_off_known)

# --- Actions: one row — apply edits · run pass · clear ----------------------
n_changes = len(active_changes) + len(limit_changes)
a1, a2, a3 = st.columns(3)
if a1.button(f"Apply changes ({n_changes})" if n_changes else "Apply changes",
             type="primary", use_container_width=True, disabled=not n_changes):
    st.session_state["limit_overrides"].update(limit_changes)
    apply_onoff(active_changes, really_apply, data_mode)
    st.session_state.pop("inv_editor", None)  # reset editor to the new state
    st.rerun()
if a2.button("Run guardrail pass", use_container_width=True, disabled=not rows):
    run_pass(rows, limits, channels, really_send, data_mode)
    st.rerun()
if a3.button("Clear log", use_container_width=True):
    state_mod.clear_activity_log()
    st.rerun()

if rows:
    with st.expander("Daily MAR & anomalies"):
        render_chart(rows, points_by_conn)

render_activity_log()

if st.session_state["debug_open"]:
    with debug_slot:
        render_debug_panel(data_mode, console_text, load_error)
