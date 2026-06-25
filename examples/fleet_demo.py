# examples/fleet_demo.py
#
# The SCALABILITY prototype. Some customers run hundreds of connectors, so this
# demo evaluates a whole fleet in one pass and proves the design point:
#
#   * Detection is ONE set-based pass over all connectors (fast at any size).
#   * NO per-connector configuration — anomaly detection learns each
#     connector's own baseline; the budget limit comes from a policy default.
#   * The UI surfaces EXCEPTIONS, not inventory — you never scroll 300 rows;
#     the few that need you float to the top, with filters and search.
#
# Run from the repo root:   streamlit run examples/fleet_demo.py
#
# Self-contained: generated data, no DB, no Fivetran. The (simulated) pause acts
# only on the handful of exceptions — O(exceptions), not O(fleet).

import os
import sys
import time

import pandas as pd
import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # repo root for `state`
sys.path.insert(0, _HERE)                   # examples/ for `detection`
import state as state_mod  # noqa: E402

from detection import evaluate_fleet, generate_fleet  # noqa: E402

st.set_page_config(page_title="MAR Guardrail — fleet view", layout="wide")

st.title("MAR Guardrail — fleet view (scalability)")
st.caption("Hundreds of connectors, evaluated in one pass, no per-connector "
           "config. The UI surfaces exceptions, not inventory.")

with st.sidebar:
    st.header("Fleet")
    n = st.slider("Connectors", 50, 500, 300, step=50)
    days = st.slider("Days elapsed this month", 7, 28, 18)
    st.header("Detection knobs — OPEN-8")
    window = st.slider("Baseline window (days)", 3, 21, 14)
    z_threshold = st.slider("Min z-score", 2.0, 10.0, 5.0, step=0.5)
    min_multiple = st.slider("Min × baseline", 1.5, 10.0, 3.0, step=0.5)


@st.cache_data(show_spinner=False)
def build_fleet(n, days):
    return generate_fleet(n=n, days=days)


fleet = build_fleet(n, days)

# Time the evaluation to make the scale point concrete.
t0 = time.perf_counter()
rows = evaluate_fleet(fleet, days, window=window,
                      z_threshold=z_threshold, min_multiple=min_multiple)
elapsed_ms = (time.perf_counter() - t0) * 1000

df = pd.DataFrame(rows)
anomalous = df[df["anomaly"]]
over = df[df["status"] == "OVER"]
near = df[df["status"] == "NEAR"]

# --- KPIs -------------------------------------------------------------------
with st.container(border=True):
    a, b, c, d, e = st.columns(5)
    a.metric("Connectors", f"{len(df):,}")
    b.metric("Total MAR", f"{int(df['mtd'].sum()):,}")
    c.metric("Anomalies", len(anomalous))
    d.metric("Over budget", len(over))
    e.metric("Near (≥80%)", len(near))
    st.caption(f"Evaluated **{len(df):,}** connectors in **{elapsed_ms:.0f} ms** "
               "— one query-shaped pass, no per-connector configuration.")

# --- Exception-focused table ------------------------------------------------
st.subheader("Connectors — most at risk first")

f1, f2, f3 = st.columns([1.2, 1.5, 2])
view = f1.radio("Show", ["Exceptions", "Anomalies", "Over", "Near", "All"],
                horizontal=False)
dests = sorted(df["destination"].unique())
chosen = f2.multiselect("Destination", dests, default=dests)
search = f3.text_input("Search connector", "")

shown = df.copy()
if view == "Exceptions":
    shown = shown[(shown["anomaly"]) | (shown["status"].isin(["OVER", "NEAR"]))]
elif view == "Anomalies":
    shown = shown[shown["anomaly"]]
elif view == "Over":
    shown = shown[shown["status"] == "OVER"]
elif view == "Near":
    shown = shown[shown["status"] == "NEAR"]
shown = shown[shown["destination"].isin(chosen)]
if search:
    shown = shown[shown["connector"].str.contains(search, case=False)]

shown = shown.sort_values("risk", ascending=False)

# Friendly display frame.
disp = pd.DataFrame({
    "Connector": shown["connector"],
    "Dest": shown["destination"],
    "Status": [("🚨 ANOMALY" if a else {"OVER": "🔴 OVER", "NEAR": "🟠 NEAR",
               "OK": "🟢 OK"}[s]) for a, s in zip(shown["anomaly"], shown["status"])],
    "MTD MAR": shown["mtd"],
    "% of limit": (shown["pct"] * 100).round(0),
    "Worst ×base": shown["worst_multiple"].round(1),
})
st.caption(f"Showing {len(disp):,} of {len(df):,} connectors.")
st.dataframe(
    disp, use_container_width=True, hide_index=True, height=380,
    column_config={
        "MTD MAR": st.column_config.NumberColumn(format="%d"),
        "% of limit": st.column_config.ProgressColumn(
            format="%d%%", min_value=0, max_value=150),
        "Worst ×base": st.column_config.NumberColumn(
            help="Worst day in the window vs its trailing baseline", format="%.1fx"),
    },
)

# --- Guardrail pass (acts only on exceptions) -------------------------------
st.subheader("Guardrail pass")
left, right = st.columns(2)
arm_pause = left.checkbox("Arm: auto-pause exceptions (anomaly or over)", value=False,
                          help="Simulated REST PATCH. Pause acts on exceptions only.")
if left.button("Run guardrail pass", type="primary"):
    exceptions = df[(df["anomaly"]) | (df["status"] == "OVER")].sort_values("risk", ascending=False)
    state_mod.log_event(
        f"Fleet pass — {len(df):,} connectors evaluated in {elapsed_ms:.0f} ms; "
        f"{len(exceptions)} exception(s) to act on", level=state_mod.INFO, source="run")
    for _, r in exceptions.iterrows():
        if r["anomaly"]:
            state_mod.log_event(
                f"{r['connector']}: {int(r['worst_mar']):,} on {r['worst_day']} vs "
                f"~{int(r['worst_baseline']):,} baseline ({r['worst_multiple']:.1f}×) — ANOMALY",
                level=state_mod.ALERT, source="anomaly")
        if r["status"] == "OVER":
            state_mod.log_event(
                f"{r['connector']}: {int(r['mtd']):,} / {int(r['limit']):,} MAR — OVER LIMIT",
                level=state_mod.ALERT, source="check")
        if arm_pause:
            state_mod.log_event(
                f"[pause] PATCH /connections/{{id}} {{paused:true}} for "
                f"'{r['connector']}' (demo — simulated REST call)",
                level=state_mod.ACTION, source="pause")
        else:
            state_mod.log_event(
                f"[slack] alert sent for '{r['connector']}' (demo — not actually sent)",
                level=state_mod.ACTION, source="slack")
    state_mod.log_event(
        f"Fleet pass complete — acted on {len(exceptions)} of {len(df):,} "
        f"(API touched only the exceptions)", level=state_mod.INFO, source="run")

if right.button("Clear log"):
    state_mod.clear_activity_log()
    st.rerun()

# --- Activity log -----------------------------------------------------------
st.subheader("Activity log")
icons = {state_mod.INFO: "•", state_mod.ACTION: "✅", state_mod.ALERT: "🚨",
         state_mod.LOG_WARN: "⚠️", state_mod.LOG_ERROR: "❌"}
events = state_mod.activity_log(newest_first=True)
if not events:
    st.caption("No activity yet — click **Run guardrail pass**. Note it acts on "
               "the exceptions only, not the whole fleet.")
else:
    with st.container(height=300, border=True):
        for ev in events:
            st.markdown(f"{icons.get(ev['level'], '•')} `{ev['ts']}` "
                        f"**[{ev['source']}]** {ev['message']}")
