# examples/anomaly_demo.py
#
# A self-contained demo of the PRD's core idea: catch a runaway connector EARLY
# with anomaly detection, not just when it crosses a monthly limit. It brings the
# PRD's worked example (§5.3) to life — a connector with a baseline of ~40k
# rows/day that spikes to ~620k after an upstream schema change — and lets you
# play with the detection knobs (OPEN-8) and the action posture (OPEN-5).
#
# Run from the repo root:   streamlit run examples/anomaly_demo.py
#
# It needs NO database and NO Fivetran account: the daily MAR is generated to
# mirror the shape of incremental_mar, and the "pause" action is simulated
# (logged, never sent). It reuses the framework's real activity log (state.py)
# to show how a detector plugs into the existing pieces.

import os
import sys

import altair as alt
import pandas as pd
import streamlit as st

# Make both this folder (for `detection`) and the repo root (for `state`)
# importable, whether launched via `streamlit run` or Streamlit's AppTest.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # repo root
sys.path.insert(0, _HERE)                   # examples/
import state as state_mod  # noqa: E402 — after sys.path bootstrap

from detection import detect, generate_series  # noqa: E402 — same folder

st.set_page_config(page_title="MAR Guardrail — anomaly demo", layout="centered")

st.title("MAR Guardrail — anomaly detection demo")
st.caption("Prototype for the PRD's two-signal detection (§5.2) and worked "
           "example (§5.3). Self-contained: generated data, simulated actions.")

# --- Controls ---------------------------------------------------------------
with st.sidebar:
    st.header("Scenario")
    connector = st.text_input("Connector", "salesforce_prod")
    days = st.slider("Days elapsed this month", 7, 28, 18)
    baseline = st.number_input("Normal daily MAR (baseline)", 1_000, 500_000, 40_000, step=5_000)
    limit = st.number_input("Monthly MAR limit", 100_000, 10_000_000, 2_000_000, step=100_000)

    st.header("The spike")
    inject = st.checkbox("Inject a spike", value=True)
    spike_day = st.slider("Spike on day", 1, days, min(18, days), disabled=not inject) - 1
    spike_mar = st.number_input("Spike size (rows that day)", 50_000, 5_000_000, 620_000, step=10_000,
                                disabled=not inject)

    st.header("Detection knobs — OPEN-8")
    window = st.slider("Baseline window (days)", 3, 21, 14)
    z_threshold = st.slider("Sensitivity: min z-score", 2.0, 10.0, 5.0, step=0.5)
    min_multiple = st.slider("Sensitivity: min × baseline", 1.5, 10.0, 3.0, step=0.5)

    st.header("Action posture — OPEN-5")
    anomaly_pauses = st.checkbox("Arm: anomaly → pause connector", value=False,
                                 help="Off (recommended default) = anomalies alert only. "
                                      "On = an anomaly pauses via the (simulated) REST API.")

# --- Detect -----------------------------------------------------------------
series = generate_series(days, baseline=baseline, noise=0.08,
                         spike_day=(spike_day if inject else None), spike_mar=spike_mar)
points, summary = detect(series, limit, window=window,
                         z_threshold=z_threshold, min_multiple=min_multiple)

# --- Top-line status --------------------------------------------------------
today = points[-1]
color = {"OK": "green", "NEAR": "orange", "OVER": "red"}[summary["status"]]
with st.container(border=True):
    a, b, c = st.columns(3)
    a.metric("Month-to-date MAR", f"{summary['mtd']:,}")
    b.metric("% of limit", f"{summary['pct'] * 100:.0f}%")
    c.metric("Anomalies", len(summary["anomalies"]))
    st.markdown(f"Budget status: :{color}[**{summary['status']}**]  ·  "
                f"latest day {today.mar:,} vs baseline {today.baseline:,.0f} "
                f"({today.multiple:.1f}×)")

# This is the punchline: threshold quiet, anomaly loud.
if summary["status"] != "OVER" and summary["anomalies"]:
    st.info("A static limit would say **" + summary["status"] +
            "** and stay silent — but anomaly detection already caught the spike. "
            "That's the early warning the daily history buys you.")

# --- Chart ------------------------------------------------------------------
df = pd.DataFrame([
    {"date": p.day, "MAR": p.mar, "baseline": p.baseline, "anomaly": p.is_anomaly}
    for p in points
])
bars = alt.Chart(df).mark_bar().encode(
    x=alt.X("date:T", title="Day"),
    y=alt.Y("MAR:Q", title="PAID MAR / day"),
    color=alt.condition(alt.datum.anomaly, alt.value("#d62728"), alt.value("#4c78a8")),
    tooltip=["date:T", "MAR:Q", alt.Tooltip("baseline:Q", format=",.0f")],
)
baseline_line = alt.Chart(df).mark_line(color="#999", strokeDash=[4, 4]).encode(
    x="date:T", y="baseline:Q",
)
st.altair_chart(bars + baseline_line, use_container_width=True)
st.caption("Blue = normal day · red = flagged anomaly · dashed = trailing baseline.")

# --- Run a guardrail pass ---------------------------------------------------
st.subheader("Guardrail pass")
run_col, clear_col = st.columns(2)
if run_col.button("Run guardrail pass", type="primary", use_container_width=True):
    state_mod.log_event(f"Guardrail pass — {connector} (demo)", level=state_mod.INFO, source="run")

    # Threshold signal (the hard backstop).
    if summary["status"] == "OVER":
        state_mod.log_event(
            f"{connector}: {summary['mtd']:,} / {limit:,} MAR — OVER LIMIT",
            level=state_mod.ALERT, source="check")
        # Threshold OVER always pauses (that's its job).
        state_mod.log_event(
            f"[pause] PATCH /connections/{{id}} {{paused:true}} for '{connector}' "
            "(demo — simulated REST call)", level=state_mod.ACTION, source="pause")
    else:
        state_mod.log_event(
            f"{connector}: {summary['mtd']:,} / {limit:,} MAR — {summary['status']} "
            f"({summary['pct'] * 100:.0f}%)", level=state_mod.INFO, source="check")

    # Anomaly signal (the early warning).
    for p in summary["anomalies"]:
        state_mod.log_event(
            f"{connector}: {p.mar:,} on {p.day} vs ~{p.baseline:,.0f} baseline "
            f"({p.multiple:.1f}×, z={p.z:.0f}) — ANOMALY",
            level=state_mod.ALERT, source="anomaly")
        if anomaly_pauses:
            state_mod.log_event(
                f"[pause] PATCH /connections/{{id}} {{paused:true}} for '{connector}' "
                "(demo — simulated REST call, anomaly-armed)",
                level=state_mod.ACTION, source="pause")
        else:
            state_mod.log_event(
                f"[slack] anomaly alert sent for '{connector}' (demo — not actually sent)",
                level=state_mod.ACTION, source="slack")

if clear_col.button("Clear log", use_container_width=True):
    state_mod.clear_activity_log()
    st.rerun()

# --- Activity log -----------------------------------------------------------
st.subheader("Activity log")
icons = {state_mod.INFO: "•", state_mod.ACTION: "✅", state_mod.ALERT: "🚨",
         state_mod.LOG_WARN: "⚠️", state_mod.LOG_ERROR: "❌"}
events = state_mod.activity_log(newest_first=True)
if not events:
    st.caption("No activity yet — click **Run guardrail pass**.")
else:
    with st.container(height=280, border=True):
        for ev in events:
            st.markdown(f"{icons.get(ev['level'], '•')} `{ev['ts']}` "
                        f"**[{ev['source']}]** {ev['message']}")
