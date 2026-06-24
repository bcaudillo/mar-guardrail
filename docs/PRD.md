# MAR Guardrail — Product Requirements Document

> **Status:** Draft v0.1 · **Owner:** @bcaudillo · **Last updated:** 2026-06-24
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
Fivetran bills on — and acts before a connector runs past its budget. The core
loop is small and already exists: read current-month PAID MAR → compare against
a per-connector limit → fire actions (alert and/or pause the connector) → record
what happened.

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
- Teams need (a) **visibility** into where MAR is going this month, and (b) an
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

---

## 4. Users & personas

| Persona | Cares about | Primary surface |
|---|---|---|
| **Data/Platform Eng (operator)** | Not getting surprised by a bill; safe auto-pause | Dashboard + config |
| **Eng manager / FinOps** | Where is MAR going; are we trending over | Dashboard overview |
| **On-call** | "Something paused — why?" | Activity log + debug panel |

*(Trim to the real audience — placeholder personas.)*

---

## 5. End-to-end story (the spine)

The product's reason to exist, mapped to what already works:

1. **Detect** — sum current-month PAID `incremental_mar` per connection;
   compare to its limit. → `query.get_current_mar()`, `main.evaluate()` **[VERIFIED]**
2. **Act** — fire the connector's configured triggers in order. The hard stop is
   pause. → `triggers.py`, `fivetran_api.pause_connector()` **[VERIFIED]**
3. **Record** — append a structured event (who, what, when, outcome) to the
   activity log. → `state.log_event()` **[VERIFIED, but in-memory only — see OPEN-2]**
4. **Surface** — show all of the above in one UI with a debug/observability
   panel. → `app.py`, `state.snapshot()` **[VERIFIED, partial]**

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

## 10. Open questions / decisions

- **[OPEN-1] Freshness.** `incremental_mar` is batch (synced ~daily). Is
  "catch within a sync cycle" acceptable, or is a tighter SLA expected? *(Rec:
  accept batch; document the lag prominently.)*
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
