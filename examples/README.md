# Examples

Runnable demos that bring the [PRD](../docs/PRD.md) to life. These are
**prototypes** — a place to try ideas (notably the anomaly-detection model the
PRD leaves open as **OPEN-8**) before promoting anything into the core
framework.

## `anomaly_demo.py` — two-signal detection

Brings the PRD's worked example (§5.3) to life: a connector with a ~40k
rows/day baseline that spikes to ~620k after an upstream schema change. It runs
**both** detection signals and shows why the pairing matters:

- **Threshold** — cumulative month-to-date MAR vs the limit (OK / NEAR / OVER).
  This mirrors the real core (`query.get_current_mar` + `main.evaluate`).
- **Anomaly** — a single day's MAR vs the connector's own trailing baseline.
  Catches the spike *the same day*, while the monthly limit is still "fine."

Run it from the repo root:

```bash
streamlit run examples/anomaly_demo.py
```

No database and no Fivetran account required — the daily MAR is generated to
mirror the shape of `incremental_mar`, and the **pause action is simulated**
(logged as a `PATCH /connections/{id}` REST call, never actually sent). It does
reuse the framework's real activity log (`state.py`) to show how a detector
plugs into the existing pieces.

### What you can play with

- **The spike** — size and which day it lands on.
- **Detection knobs (OPEN-8)** — baseline window, min z-score, and min ×baseline.
  This is the open question the demo exists to explore: how sensitive should
  anomaly detection be before it's useful without crying wolf?
- **Action posture (OPEN-5)** — anomalies *alert only* (recommended default) vs.
  *anomaly → pause*. Pausing is always simulated here.

### Files

- `detection.py` — the prototype detection logic (`generate_series`, `detect`).
  Pure functions, no Streamlit. This is the candidate to harden and promote into
  core once OPEN-8 is settled.
- `anomaly_demo.py` — the Streamlit UI around it.
