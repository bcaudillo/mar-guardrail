# MAR Guardrail — Product Requirements Document

> **Status:** Draft v0.3 · **Owner:** @bcaudillo · **Last updated:** 2026-06-24
> **Type:** Living document. Sections marked **[OPEN]** are unresolved product
> decisions; sections marked **[VERIFIED]** reflect what the codebase already
> does today. This is a framework — fill, cut, and argue with it.

---

## 0. How to read this doc

- **[VERIFIED]** — true in the code right now (file references included).
- **[AVAILABLE]** — data/capability that exists but we haven't wired up.
- **[OPEN]** — a decision we owe ourselves before building. Each has a
  recommendation, but the call is the owner's.
- **[FUTURE]** — explicitly out of scope for the first shippable version.

---

## 1. Summary

MAR Guardrail watches per-connector **Monthly Active Rows (MAR)** — the metric
Fivetran bills on — and acts before a connector runs past its budget. It works
by reading the MAR history that the **Fivetran Platform Connector** lands in your
own warehouse, which lets it do both **budget enforcement** (vs a limit) and
**anomaly detection** (spotting abnormal spikes early). The core loop is small
and already exists: read current-month PAID MAR → compare against a per-connector
limit (and its baseline) → fire actions (alert and/or pause the connector) →
record what happened.

This PRD covers the next stage: a **single cohesive product** that merges the
visual clarity of the dashboard prototype, the at-a-glance automation of the
early console, and the operability of the current debug panel — all pointed at
one end goal:

> **When a connector's MAR crosses its limit, pause the connector and durably
> log that it happened.**

---

## 2. Problem & background

- Fivetran bills on MAR. A misconfigured or runaway connector can quietly run up
  spend, and the overage isn't visible until it's already incurred.
- MAR is **billed per calendar month** and accrues daily; there is no hard cap
  in Fivetran itself, so overruns are a budgeting/ops problem, not a platform
  one.
- The dangerous failures are often **anomalies**, not gradual drift: a schema
  change re-ingests a whole table, a misconfigured sync loops, a backfill runs
  away. A static monthly limit only catches these *after* they've accumulated;
  the daily history from the Platform Connector lets us catch the spike early.
- Teams need (a) **visibility** into where MAR is going this month, (b)
  **early warning** when a connector behaves abnormally, and (c) an
  **automatic backstop** that pauses or alerts before a connector blows its
  budget — without babysitting a dashboard.

---

## 3. Goals & non-goals

### Goals
- G1. Show current-month PAID MAR per connector against a budget, at a glance.
- G2. Automatically take a configured action when a connector crosses its limit.
- G3. Make "pause the connector" a first-class, **reversible**, **logged** action.
- G4. Keep a durable, auditable record of every guardrail action.
- G5. Be transparently operable — you can always see *why* it did (or didn't) act.

### Non-goals
- N1. We do not try to be real-time. MAR data is as fresh as the Platform
  Connector sync. **[OPEN-1]**
- N2. We do not reduce MAR already incurred this month; pausing prevents
  *further* growth only.
- N3. We are not a general Fivetran admin tool (no resync, schema editing, etc.).
- N4. We do not write back to `config.py` from the UI; config stays the source
  of truth for unattended runs. **[VERIFIED]**
- N5. **We do not compute or display dollar cost. MAR is the unit.** Cost is MAR
  run through a *tiered* pricing curve, and annual commitments carry
  *discounted/negotiated* rates that differ per customer and plan — any dollar
  figure we showed would be wrong for someone, and a wrong cost number is worse
  than none. The guardrail enforces a **MAR limit** (the lever we can measure
  exactly); customers translate MAR ↔ cost with **Fivetran's pricing
  estimator** against their own plan. We keep them *cost-aware* by making the
  MAR limit the explicit budget and linking to that estimator — not by doing the
  dollar math ourselves. **[VERIFIED — integer MAR limits, no cost math]**

### How MAR is calculated (and why summing is correct)

MAR is **distinct active rows per calendar month** — a row active on several days
counts **once**, not once per day. Fivetran's `incremental_mar` table is built for
this: `incremental_rows` is the count of *newly*-active rows each day, so
**summing it across the month yields MAR with no double-counting** (summing a
"total rows" column instead would over-count badly). We sum **PAID** rows only.
Daily anomaly detection reads the same incremental series. This is a deliberate,
load-bearing detail — "fixing" the sum into a distinct-count over totals would be
wrong. **[VERIFIED — query.get_current_mar / get_daily_mar]**

---

## 4. Users & personas

| Persona | Cares about | Primary surface |
|---|---|---|
| **Data/Platform Eng (operator)** | Not getting surprised by a bill; safe auto-pause | Dashboard + config |
| **Eng manager / FinOps** | Where is MAR going; are we trending over | Dashboard overview |
| **On-call** | "Something paused — why?" | Activity log + debug panel |

*(Trim to the real audience — placeholder personas.)*

---

## 5. How it works

### 5.1 The mechanism — the Fivetran Platform Connector

Fivetran doesn't expose MAR through a convenient real-time meter. Instead it
offers the **Platform Connector** (a.k.a. the Fivetran log / metadata connector):
a first-party connector that syncs your *account's own operational metadata* —
MAR, connector status, sync logs, usage — into your destination, exactly like
any other source. Once it's running, your MAR history lives in your own
warehouse as queryable tables.

This is the linchpin of the whole product. Because the Platform Connector lands
**`incremental_mar`** (daily `incremental_rows` per connection, tagged
PAID / FREE / SYSTEM) into the destination, MAR becomes **queryable and
historical** instead of being locked inside Fivetran's billing UI. Everything
downstream — budgets, trends, and anomaly detection — is just SQL over that
table.

**Why this enables anomaly detection.** Because we get *daily, per-connection*
MAR with history, we are not limited to a static monthly ceiling. We can learn
each connector's normal pattern and flag when a day's MAR deviates from it — a
spike that signals a runaway sync, a schema change re-ingesting everything, or a
sync loop — and catch it **early, mid-month**, before it accumulates into a
budget breach. The Platform Connector is what makes that baseline possible; a
static limit alone can't see a spike coming.

### 5.1a Two surfaces: read vs. act

The product talks to Fivetran through **two distinct integration surfaces**, and
keeping them separate is core to the design:

| | **Read / detect** | **Act** |
|---|---|---|
| Surface | Platform Connector → destination tables | **Fivetran REST API** (`api.fivetran.com/v1`) |
| Nature | Batch, queryable history (SQL) | Live, state-changing (HTTP) |
| Auth | Database connection (`DATABASE_URL`) | API key + secret (HTTP Basic) |
| Used for | MAR, baselines, trends, anomaly detection | **Pausing / resuming connectors**, live status |
| Code | `query.py` | `fivetran_api.py` **[VERIFIED]** |

So: we **detect** by reading the warehouse the Platform Connector populates, and
we **act** by calling the **Fivetran REST API**. Pausing a connector is a
`PATCH /connections/{id}` with `{"paused": true}` (resume is the same call with
`false`); resolving a connector name to its API id is a walk of
`/groups` → `/connections`. All state changes go through the REST API — the
destination tables are read-only to this product. **[VERIFIED]**

### 5.2 The loop (the spine)

The product's reason to exist, mapped to what already works:

1. **Detect** — two complementary signals over `incremental_mar`:
   - **Budget / threshold** — cumulative current-month PAID MAR vs the
     connector's limit (OK / NEAR / OVER). → `query.get_current_mar()`,
     `main.evaluate()` **[VERIFIED]**
   - **Anomaly** — a day's MAR deviating from the connector's recent baseline,
     independent of the monthly cap (early warning). **[OPEN — see OPEN-8]**
2. **Act** — fire the connector's configured triggers in order, executing state
   changes through the **Fivetran REST API**. The hard stop is pause. →
   `triggers.py`, `fivetran_api.pause_connector()` **[VERIFIED]**
3. **Record** — append a structured event (who, what, when, outcome) to the
   activity log. → `state.log_event()` **[VERIFIED, but in-memory only — see OPEN-2]**
4. **Surface** — show all of the above in one UI with a debug/observability
   panel. → `app.py`, `state.snapshot()` **[VERIFIED, partial]**

### 5.3 Worked example

**Setup.** Connector `salesforce_prod` has a monthly PAID MAR limit of
**2,000,000**. It normally ingests **~40,000 rows/day**. On the 18th of the
month, a new field is added upstream and Fivetran re-syncs the object — that
day's load lands **620,000 rows**.

**1. Data** — the Platform Connector syncs this into `incremental_mar`:

```
measured_date | connection_name | free_type | incremental_rows
2026-06-16    | salesforce_prod | PAID      |     38,500
2026-06-17    | salesforce_prod | PAID      |     41,200
2026-06-18    | salesforce_prod | PAID      |    620,000   <- spike
```

**2. Detect** — on the next guardrail pass, two signals run over that table:

- **Budget / threshold:** month-to-date = **1,300,000 / 2,000,000 = 65%** →
  classified **OK**. *A static limit alone stays silent here.* **[VERIFIED]**
- **Anomaly:** day-18's 620,000 vs the trailing-14-day baseline (mean ≈ 40,000)
  is **~15x normal** → **flagged the same day**. **[OPEN-8]**

**3. Act** — through the **Fivetran REST API**:

- Default posture *(recommended in OPEN-5 / OPEN-8)*: anomaly is **alert-only**
  → a Slack/email goes out; nothing is paused.
- If `salesforce_prod` had **anomaly → pause** armed: `PATCH /connections/{id}`
  with `{"paused": true}`. **[VERIFIED capability]**
- **Backstop:** if MAR later crosses 2,000,000, the **threshold** trips OVER and
  pause fires regardless of the anomaly path. **[VERIFIED]**

**4. Record** — every step appends to the activity log (durable sink **[OPEN-2]**):

```
2026-06-18 04:12:07 [check]   salesforce_prod: 1,300,000 / 2,000,000 MAR — ok (65%)
2026-06-18 04:12:07 [anomaly] salesforce_prod: 620,000 today vs ~40,000 baseline (15.5x) — ANOMALY
2026-06-18 04:12:08 [slack]   anomaly alert sent for 'salesforce_prod'
```

**Why this is the whole point.** The static limit would have reported "65% —
fine," and you'd have discovered the problem at month-end on the invoice.
Because the Platform Connector gives daily history, anomaly detection caught it
on the **18th** — almost two weeks of runaway syncs earlier — and the action
went out over the REST API. Threshold = the hard backstop; anomaly = the early
warning.

### 5.4 Two-speed detection — budget (daily) + velocity (per-sync)

**Researched freshness finding:** Fivetran computes MAR **once per day** —
`incremental_mar` is a daily table (UTC, monthly reset) and the usage dashboards
are "updated daily." **No sync frequency beats that ~1-day floor** for the billed
number; tightening the Platform Connector's schedule only removes the *pull* lag
(its default is a once-a-day sync), getting you to ~1 day, not real time.

But **MAR can grow in hours** — a runaway can burn the budget in an afternoon —
so a daily-only signal can't *prevent* damage, only report it. The fix is a
second, faster signal. Detection therefore runs at **two speeds**:

| Lane | Source | Freshness | Role |
|---|---|---|---|
| **Budget** (authoritative) | `incremental_mar` (daily) | ~1 day | month-to-date vs limit, run-rate, the real billed MAR |
| **Velocity** (early warning) | LOG `records_modified` (per sync) | **minutes** | catch a runaway *as it happens*; pause to stop the bleeding |

- The **LOG table is append-only and updates every sync**; its
  `records_modified` event carries **rows written per sync** (per schema/table).
  With the Platform Connector at 5–15 min, this is **minutes-fresh** — fast
  enough to catch an afternoon runaway.
- **Caveat:** `records_modified` is *rows written, not MAR* — a row updated 50×
  counts 50 here but 1 in MAR, so it **over-counts**. It is a **velocity /
  anomaly signal, not a budget figure**: watch each connector's write-rate vs
  *its own baseline* and fire on a spike. The daily MAR stays the bill of record.
- **Pairing:** velocity = the **circuit breaker** (pause now); budget = the
  authoritative spend + run-rate projection. **[OPEN-10]**

### 5.5 Run modes

- **Event-driven** (recommended): run right after each Platform Connector sync
  (its sync-end webhook) so the velocity lane is as fresh as the data allows.
- **Scheduled** (daily/hourly cron): the simple default.
- **Background snapshot report:** each run emits a digest of every connector's
  budget status + any velocity spikes — usable as a standing report even when no
  action is taken (alert-only). **[OPEN-11]**

### 5.6 The three clocks of freshness

"Catching a spike in time" is a chain of three intervals — **you are only as
fast as the slowest**:

1. **The connector's own sync** — when its rows actually move and get logged. We
   don't set it, but we can **read it**: each connector's sync frequency and
   last-sync time live in the Platform Connector metadata (and the REST API), so
   the tool can show every connector's *freshness floor*.
2. **The Platform Connector's sync** — when those logs reach your warehouse.
   **Default is once a day — set it as fast as possible** (15 min on Standard,
   1–5 min on Enterprise). Its own MAR is **free (SYSTEM)**, so this is the
   single biggest freshness lever and it costs nothing.
3. **The guardrail run** — when we evaluate and act. Make it **event-driven**
   (Platform Connector sync-end webhook) or a tight cron.

The velocity lane (§5.4) **inherits clock #2**: the LOG `records_modified` stream
is only as fresh as the Platform Connector sync — so cranking #2 is what turns it
from daily into minutes. Tighten all three and a spike is caught **within one
sync cycle (minutes–hour)**. The only floor we cannot beat: a connector that
*itself* syncs once a day can't be seen intraday — nothing exposes rows that
haven't been written yet.

**Product surface:** a **"fast-lane freshness — last update N min ago"** badge
(from the newest `records_modified` timestamp) plus each connector's **sync
cadence**, so the customer always knows how current the signal is and where the
floor sits. **[OPEN-10]**

---

## 6. Data inventory — what's available

Two sources with very different properties.

### 6.1 Destination database (batch, synced by the Platform Connector)
- **`incremental_mar`** **[VERIFIED]** — the entire MAR signal.
  Columns used: `schema_name`, `connection_name`, `free_type`
  (PAID/FREE/SYSTEM), `measured_date` (daily), `incremental_rows`.
  - Derivable today with no extra setup **[AVAILABLE]**: daily trend within the
    month, run-rate projection to month-end, PAID/FREE/SYSTEM split,
    month-over-month comparison, account totals, top movers.
- **Other Platform Connector tables** (connection metadata, status/log tables)
  **[AVAILABLE, unverified per-deployment]** — exact names vary
  (`fivetran_platform` vs `fivetran_metadata`). Confirm via the schema
  introspector before relying on them. **[OPEN-3]**

### 6.2 Fivetran REST API (live, can change state)
- `GET /groups` → `GET /groups/{id}/connections` **[VERIFIED]** — per connection:
  `id`, `schema` (name we match on), `service` (type), `paused`,
  `status{setup_state, sync_state}`, `succeeded_at`/`failed_at`, `schedule`.
- `PATCH /connections/{id}` `{"paused": true|false}` **[VERIFIED]** — pause/resume.

### 6.3 App/config state
- Per-connector `mar_limit` + `triggers`; channel settings. **[VERIFIED]** (`config.py`)
- Activity log (structured events). **[VERIFIED]** (`state.py`)

---

## 7. Functional requirements

### 7.1 Monitoring & evaluation
- FR1. Read current-month PAID MAR per connection. **[VERIFIED]**
- FR2. Compare each watched connector to its integer `mar_limit`; classify
  OK / NEAR (≥ threshold) / OVER. **[VERIFIED]** (NEAR threshold = 80% **[OPEN-4]**)
- FR3. Surface daily trend + run-rate projection per connector. **[AVAILABLE]**
- FR3a. **Anomaly detection** — flag a connector whose daily MAR deviates
  significantly from its own recent baseline, independent of the monthly limit.
  Signal, sensitivity, and action mapping are **[OPEN-8]**.

### 7.2 Actions / triggers
- FR4. On OVER, fire the connector's triggers in configured order:
  `pause`, `slack`, `email`, `webhook`. **[VERIFIED]**
- FR5. Pause must be reversible (resume) from the product. **[AVAILABLE — resume not yet exposed]**
- FR6. Auto-pause must be explicitly **armed**; default posture is alert-only. **[OPEN-5]**

### 7.3 Activity log / audit
- FR7. Every action (and failure) logs a structured event: timestamp, source,
  connector, level, message. **[VERIFIED]**
- FR8. The log must be **durable** across restarts. **[OPEN-2]**

### 7.4 Observability / debug
- FR9. Pre-flight system state: config, DB connection, MAR table, Fivetran API —
  each with the exact failure reason. **[VERIFIED]** (`state.snapshot()`)
- FR10. Schema introspector: list real tables/columns in `PLATFORM_SCHEMA` +
  sample row. **[OPEN-3]**

---

## 8. UX / UI requirements (the merge)

The target is **dashboard prototype's clarity + early console's automation +
current debug panel**:

- UX1. **Overview KPIs** — total MAR, connectors, over, near. **[VERIFIED-ish]**
- UX2. **Connector list as the centerpiece** — per row: current MAR / limit,
  % of limit, progress bar, OK/NEAR/OVER, sorted most-at-risk first.
  *(This is the "gold standard" from the dashboard prototype.)* **[AVAILABLE]**
- UX3. **Per-trigger arm/disarm** with live status, from the early console. **[AVAILABLE]**
- UX4. **Activity log** panel, newest-first, real events. **[VERIFIED]**
- UX5. **Debug/observability panel** (system state + console + schema). **[VERIFIED, partial]**
- UX6. **Live vs Demo** data toggle; live is default. **[VERIFIED]**
- UX7. Native Streamlit components, minimal custom styling. **[OPEN-6]**
  *(The dashboard prototype used heavier custom CSS — decide how much visual
  polish vs. minimalism we keep.)*

---

## 9. Architecture (current modules)

| Module | Responsibility | Touch in this phase? |
|---|---|---|
| `config.py` | Source of truth: limits, triggers, channels | maybe (durable-log config) |
| `query.py` | Read MAR from destination (DB-agnostic) | yes (trend, introspect) |
| `fivetran_api.py` | Live state: pause/resume, status | yes (resume, status) |
| `triggers.py` | One function per action; logs each | maybe |
| `state.py` | Snapshot (what's true) + activity log (what happened) | yes (durability) |
| `main.py` | One guardrail pass; CLI entrypoint | maybe |
| `app.py` | Demo/operator UI | yes (the merge) |

---

## 9a. Footprint & compute (non-functional requirements)

**It must run cheap — no large compute.** This is a hard design rule, not an
aspiration, and it's a differentiator vs. "stand up an anomaly-detection
platform." The profile:

- **Single periodic batch job**, not a standing service. A cron / Lambda /
  Airflow task that runs for **seconds** and exits. No always-on process, no
  cluster, no queue. **[VERIFIED — main.py is one pass then exits]**
- **The warehouse does the aggregation, not us.** Detection is **one `GROUP BY`**
  over `incremental_mar`; the app reads a *small* result (hundreds–thousands of
  rows) and **never scans raw rows**. Cost is dominated by that one cheap
  aggregation. **[VERIFIED — query.py]**
- **Trivial math.** Anomaly detection is rolling mean/σ — **O(connectors × days)**,
  milliseconds and megabytes. **No ML training, no GPU, no streaming, no model
  to host.** **[VERIFIED — detection.py; 500 connectors scored in ~18 ms]**
- **Bounded data windows.** Current month + a trailing baseline window — never
  full history.
- **Act on exceptions only.** O(exceptions) REST calls, not O(fleet). **[VERIFIED]**

**Design rules that keep it there** (treat as invariants): aggregate in SQL not
Python; stateless periodic batch; simple statistics over ML; bound every window;
touch the API only for connectors that need action. An operator UI doing live
reads should **cache** the warehouse query + roster between interactions so a
click doesn't re-query. **[OPEN-9 — add caching to app.py's live loads]**

NFR target: a guardrail pass over a few hundred connectors completes in
**seconds** in a **tiny container** (no GPU, sub-GB memory).

---

## 10. Open questions / decisions

- **[OPEN-1] Freshness — RESEARCHED.** Fivetran computes MAR **once per day**
  (UTC); `incremental_mar` and the usage dashboards update daily. The ~1-day
  floor is **unbeatable via sync frequency** — tightening the Platform Connector
  only removes the *pull* lag (free, worth doing). MAR can grow in hours, so the
  resolution is the **two-speed model (§5.4)**: keep the daily budget as the
  authoritative spend, add the per-sync LOG **velocity lane** as the circuit
  breaker, and use **run-rate projection** to act on the trajectory. *(Decided.)*
- **[OPEN-2] Durable log sink.** In-memory today. DB table / file / rely on
  alert channels? *(Rec: a `guardrail_actions` table in the destination.)*
- **[OPEN-3] Schema introspector.** Build it into debug first, so "what's
  available" is answered against the real account? *(Rec: yes, build first.)*
- **[OPEN-4] NEAR threshold.** Fixed 80%, or per-connector configurable?
- **[OPEN-5] Auto-pause safety.** Default alert-only + explicit arm + resume?
  Confirmation step? Dry-run mode? *(Rec: alert-only default, arm to enable.)*
- **[OPEN-6] Visual fidelity.** How much of the dashboard prototype's custom
  styling do we keep vs. native-minimal?
- **[OPEN-7] Scheduling.** Out of scope (cron/Airflow is the user's), or do we
  ship a recommended runner?
- **[OPEN-8] Detection model.** Threshold-only, anomaly-only, or both? If
  anomaly: what's the signal (e.g. daily `incremental_rows` vs trailing N-day
  mean/σ, a % jump over baseline, or a more involved seasonal model), how
  sensitive, and how do anomalies map to actions (alert-only vs eligible for
  pause)? *(Rec: keep the budget threshold as the hard guardrail; add anomaly
  detection as an early-warning that alerts but does not auto-pause initially.)*
- **[OPEN-9] Keep it cheap (see §9a).** Cache the operator UI's live warehouse +
  roster reads so widget clicks don't re-query; confirm the unattended pass stays
  a seconds-long batch as connector counts grow. *(Rec: cache live loads with a
  short TTL, invalidate on apply/refresh.)*
- **[OPEN-10] Velocity lane (§5.4).** Build `records_modified`-based per-sync
  write-volume detection: confirm the LOG table's table name + `message_data`
  JSON shape against a real warehouse (via the introspector), baseline each
  connector's write-rate, and decide the spike→action mapping (alert-first;
  pause on sustained spike). *(Rec: alert on first spike, pause on sustained.)*
- **[OPEN-11] Background snapshot report.** What/where does the digest go — a
  written file, a Slack post, a `guardrail_actions`-style table? How often?
  *(Rec: a per-run summary to the same activity log + optional Slack digest.)*

---

## 11. Milestones (proposed, not committed)

- **M0 — PRD agreed.** This document.
- **M1 — Know the data.** Schema introspector in debug; confirm real columns.
- **M2 — Durable spine.** Persist activity log; expose resume; arm/disarm pause.
- **M3 — The merged dashboard.** Connector centerpiece + trend + KPIs + triggers.
- **M4 — Polish.** Run-rate projection, sorting, visual pass.

---

## 12. Success metrics **[OPEN]**

*(Placeholder — define what "working" means.)*
- Time-to-detect an overrun.
- % of overruns auto-paused before exceeding budget by > X%.
- Zero unexplained pauses (every pause has a log entry).

---

## 13. Risks

- R1. **Wrong pause.** Auto-pausing a healthy production connector. → mitigate
  with arm/disarm + resume + dry-run. **[OPEN-5]**
- R2. **Name mismatch.** API matches on `schema`; if naming drifts from
  `incremental_mar.connection_name`, pause silently no-ops. **[VERIFIED risk]**
- R3. **Stale data.** Acting on MAR that's a sync cycle old. **[OPEN-1]**
- R4. **Lost audit.** In-memory log disappears on restart. **[OPEN-2]**

---

## 14. Out of scope / future

- Real-time MAR. **[FUTURE]**
- Multi-account / org rollups. **[FUTURE]**
- Forecasting beyond simple run-rate. **[FUTURE]**
- Writing config back from the UI. **[FUTURE / explicit non-goal]**
