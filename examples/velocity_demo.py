# examples/velocity_demo.py
#
# PROTOTYPE of the two-speed model (PRD §5.4): a fast "velocity" lane that catches
# a connector moving lots of data *right now*, next to the lagging daily "budget"
# lane. The point it makes: the daily MAR can't catch an hours-scale runaway —
# the per-sync LOG `records_modified` stream can.
#
# Self-contained (generated data, simulated pause). Run from the repo root:
#   streamlit run examples/velocity_demo.py
#
# It models what the LOG table gives you: rows written per sync. The daily MAR is
# shown as a stale "as of yesterday" number to make the contrast concrete.

import os
import random
import sys
from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import state as state_mod  # noqa: E402 — after sys.path bootstrap


def detect_velocity(series, window, z_threshold, min_multiple):
    """Flag syncs whose rows-written spikes above the connector's own baseline.

    Robust on purpose: anomalous syncs do NOT feed the baseline, so a *sustained*
    runaway stays flagged the whole time (instead of becoming the new normal).
    Returns a list of per-sync dicts."""
    out, baseline_hist = [], []
    for t, v in series:
        prior = baseline_hist[-window:]
        if len(prior) >= 3:
            mean = sum(prior) / len(prior)
            std = (sum((x - mean) ** 2 for x in prior) / len(prior)) ** 0.5
            mult = v / mean if mean else 0.0
            if std > 0:
                significant = (v - mean) / std >= z_threshold
            else:
                significant = v > mean
            anom = significant and mult >= min_multiple
        else:
            mean, mult, anom = float(v), 1.0, False
        out.append({"t": t, "rows": v, "baseline": mean, "mult": mult, "anom": anom})
        if not anom:
            baseline_hist.append(v)
    return out


st.set_page_config(page_title="MAR Guardrail — velocity lane", layout="centered")
st.title("MAR Guardrail — velocity lane (catch data moving fast)")
st.caption("Two-speed detection. The **daily budget** lane is a day late; the "
           "**per-sync velocity** lane (LOG `records_modified`) catches a runaway "
           "in minutes. Prototype on generated data.")

with st.sidebar:
    st.header("Connector")
    connector = st.text_input("Name", "postgres_billing")
    conn_sync = st.select_slider("Connector's own sync (min) — clock #1",
                                 options=[5, 15, 30, 60, 120, 360, 1440], value=15)
    sync_min = st.select_slider("Platform Connector sync (min) — clock #2",
                                options=[5, 10, 15, 30, 60], value=15)
    baseline = st.number_input("Normal rows / sync", 500, 200_000, 5_000, step=500)
    st.header("The runaway")
    start_h = st.slider("Started (hours ago)", 1, 20, 6)
    spike_x = st.slider("× normal during runaway", 2.0, 50.0, 24.0, step=1.0)
    st.header("Daily budget lane (lagging)")
    mtd = st.number_input("MAR month-to-date — as of yesterday", 0, 5_000_000, 1_200_000, step=50_000)
    limit = st.number_input("Monthly MAR limit", 100_000, 10_000_000, 2_000_000, step=100_000)
    st.header("Velocity sensitivity")
    window = st.slider("Baseline window (syncs)", 3, 30, 12)
    z = st.slider("Min z-score", 2.0, 10.0, 5.0, step=0.5)
    mult = st.slider("Min × baseline", 1.5, 20.0, 3.0, step=0.5)

# --- Generate the per-sync write-volume series over the last 24h -------------
rnd = random.Random(7)
now = datetime.now().replace(second=0, microsecond=0)
n = int(24 * 60 / sync_min)
start_idx = n - int(start_h * 60 / sync_min)
series = []
for i in range(n):
    t = now - timedelta(minutes=sync_min * (n - 1 - i))
    if i >= start_idx:
        v = int(baseline * spike_x * (1 + rnd.uniform(-0.1, 0.1)))
    else:
        v = int(baseline * (1 + rnd.uniform(-0.15, 0.15)))
    series.append((t, v))

points = detect_velocity(series, window, z, mult)
anoms = [p for p in points if p["anom"]]
spiking = bool(anoms)
onset = anoms[0]["t"] if spiking else None
peak_mult = max((p["mult"] for p in anoms), default=1.0)
current = series[-1][1]
runaway_rows = sum(p["rows"] for p in anoms)

# --- Budget lane status ------------------------------------------------------
pct = mtd / limit if limit else 0
budget = "🔴 OVER" if mtd > limit else ("🟠 NEAR" if pct >= 0.8 else "🟢 OK")

# --- Two lanes, side by side -------------------------------------------------
b, vlane = st.columns(2)
with b:
    st.markdown("**Budget lane** · daily")
    st.metric("MAR month-to-date", f"{mtd:,}", f"{pct*100:.0f}% of limit", delta_color="off")
    st.markdown(f"Status: {budget}")
    st.caption("From `incremental_mar` — Fivetran computes it **once a day**. This "
               "is yesterday's number.")
with vlane:
    st.markdown("**Velocity lane** · per sync")
    if spiking:
        st.metric("Rows / sync now", f"{current:,}", f"{peak_mult:.0f}× normal", delta_color="inverse")
        st.markdown(f"Status: :red[**🚨 SPIKING since {onset:%H:%M}**]")
    else:
        st.metric("Rows / sync now", f"{current:,}", "normal")
        st.markdown("Status: :green[**✓ normal**]")
    st.caption("From the LOG `records_modified` event — updates **every sync** "
               f"(~{sync_min} min). Rows written, not MAR (a velocity signal).")

# --- The punchline -----------------------------------------------------------
if spiking and mtd <= limit:
    st.error(
        f"The budget lane still reads **{budget}** — that's yesterday's MAR. "
        f"The velocity lane caught **{connector}** at **{onset:%H:%M}** "
        f"(**{peak_mult:.0f}× normal**, ~**{runaway_rows:,} rows** since). "
        f"That's **~{start_h}h** before the daily MAR would even show it — the "
        "difference between **stopping** the runaway and **reading about it tomorrow**.")

# --- The three clocks of freshness ------------------------------------------
# Time-to-see a spike = the slowest of the three clocks (guardrail runs
# event-driven, so it adds ~0). Fast-lane freshness = the newest sync.
worst_min = max(conn_sync, sync_min)
c1, c2, c3 = st.columns(3)
c1.metric("① Connector sync", f"{conn_sync} min", "rows move + get logged", delta_color="off")
c2.metric("② Platform Connector", f"{sync_min} min", "log → your warehouse", delta_color="off")
c3.metric("③ Guardrail run", "event-driven", "on sync-end", delta_color="off")
fresh = f"~{sync_min} min ago" if series else "—"
if worst_min >= 1440:
    st.warning(f"**Fast-lane freshness: newest `records_modified` {fresh}.** But this "
               f"connector only syncs every **{conn_sync//60}h+** (clock ①) — its intraday "
               "volume is invisible until it syncs. **Nothing** can beat clock ①.")
else:
    st.caption(f"**Fast-lane freshness: newest `records_modified` {fresh}.** "
               f"Worst-case time to *see* a spike ≈ **{worst_min} min** (the slowest clock). "
               "Set the Platform Connector as fast as possible — its MAR is free (SYSTEM).")

# --- Chart -------------------------------------------------------------------
df = pd.DataFrame(points)
bars = alt.Chart(df).mark_bar().encode(
    x=alt.X("t:T", title="last 24h, per sync"),
    y=alt.Y("rows:Q", title="rows written / sync"),
    color=alt.condition(alt.datum.anom, alt.value("#d62728"), alt.value("#4c78a8")),
    tooltip=[alt.Tooltip("t:T", title="sync"), alt.Tooltip("rows:Q", format=",")])
line = alt.Chart(df).mark_line(color="#999", strokeDash=[4, 4]).encode(x="t:T", y="baseline:Q")
st.altair_chart(bars + line, use_container_width=True)
st.caption("Blue = normal sync · red = flagged as moving fast · dashed = learned baseline.")

# --- Stop it -----------------------------------------------------------------
c1, c2 = st.columns(2)
if c1.button("⏸ Pause connector now", type="primary", disabled=not spiking, use_container_width=True):
    state_mod.log_event(
        f"[pause] {connector} paused on velocity signal — {peak_mult:.0f}× normal "
        f"since {onset:%H:%M} (demo — simulated REST call)",
        level=state_mod.ACTION, source="pause")
    st.success(f"Paused **{connector}** (simulated). In live mode this is a real "
               "`PATCH /connections/{id}` — the runaway stops here.")
if c2.button("Clear log", use_container_width=True):
    state_mod.clear_activity_log()
    st.rerun()

events = state_mod.activity_log(newest_first=True)
if events:
    st.markdown("**Activity log**")
    for ev in events[:20]:
        st.markdown(f"`{ev['ts']}` {ev['message']}")
