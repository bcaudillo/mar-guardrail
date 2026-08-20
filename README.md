# MAR Guardrail

A small, ready-to-run framework that watches your Fivetran **MAR** (Monthly
Active Rows — the rows Fivetran bills you for) and takes action when a
connector goes over the budget you set for it. It can pause the connector,
ping Slack, send an email, or hit a webhook — your choice, per connector.

It's built on the **Fivetran Platform Connector**, which lands your usage data
in your own destination. This project reads that data, compares it to your
limits, and reacts. You own all of it — clone it, fill in a config file, and
change anything you like.

---

## What this is and what problem it solves

MAR is what drives your Fivetran bill. It's easy for one chatty connector to
quietly run up a lot of paid rows before anyone notices. This framework gives
you a guardrail: set a monthly MAR limit per connector, and when a connector
crosses it, the framework automatically alerts you — or pauses the connector
outright so spend stops.

It is intentionally simple and transparent so you can read every line and make
it your own.

---

## Setup

1. **Install dependencies** (Python 3.10+):
   ```bash
   pip install -r requirements.txt
   ```

2. **Add your secrets.** Copy the example env file and fill it in:
   ```bash
   cp .env.example .env
   ```
   Your `.env` holds your database URL, Fivetran API key/secret, and any
   Slack/email/webhook details. It's gitignored, so it never gets committed.

3. **Fill in `config.py`.** This is the only file you need to edit for normal
   use. List the connectors you want to watch, set a `mar_limit` for each, and
   choose which triggers fire when a limit is exceeded. Every field has a
   comment explaining what to change.

4. **Run it:**
   ```bash
   python main.py
   ```
   It checks each connector once and prints a clean summary, then exits.

> **Tip:** MAR limits must be positive whole numbers greater than 0. The
> framework validates this at startup and tells you exactly what's wrong if a
> limit is missing or invalid.

---

## Database support

The Platform Connector lands its data in **your** destination, and this
framework reads it through [SQLAlchemy](https://www.sqlalchemy.org/) — so it
works with **any** database SQLAlchemy supports (Postgres, Snowflake, BigQuery,
Redshift, Databricks, MySQL, and more). You pick the database with two settings:

1. **`DATABASE_URL`** — a SQLAlchemy connection URL for your destination:

   | Destination | Example URL |
   |-------------|-------------|
   | Postgres | `postgresql://user:pass@host:5432/dbname` |
   | Snowflake | `snowflake://user:pass@account/dbname?warehouse=WH&role=ROLE` |
   | BigQuery | `bigquery://project/dataset` |
   | Redshift | `redshift+psycopg2://user:pass@host:5439/dbname` |

2. **`FIVETRAN_PLATFORM_SCHEMA`** — the schema (BigQuery: dataset) the Platform
   Connector writes into, e.g. `fivetran_platform`. This lets the query find
   `incremental_mar` no matter what the connection's default search path is.

Postgres works out of the box. For another destination, install its driver
(the comments in `requirements.txt` list the package per database) — the query
itself is standard SQL and doesn't change. The framework only ever **reads**
one table (`incremental_mar`), so a **read-only** database user is enough.

---

## How to schedule it

`main.py` runs one check and exits — you decide how often to run it. Pick
whatever you already use:

- **cron** (hourly):
  ```
  0 * * * * cd /path/to/mar-guardrail && python main.py
  ```
- **APScheduler** (inside your own Python script):
  ```python
  scheduler.add_job(run, "interval", hours=1)
  ```
- **Airflow**:
  ```python
  BashOperator(task_id="mar_guardrail", bash_command="python /path/main.py")
  ```

Hourly is a sensible starting point. Run it more often if you want tighter
control.

---

## How to add a new trigger

Triggers are the actions taken when a connector is over its limit. Adding one
is three small, obvious edits:

1. **`triggers.py`** — copy one of the existing `trigger_*` functions and change
   its body to do your new thing (e.g. post to Microsoft Teams).
2. **`config.py`** — add your trigger's name to the `triggers` list of any
   connector that should use it, and add any settings it needs.
3. **`main.py`** — add one line to the dispatch table so the name maps to your
   new function.

Every trigger follows the same shape and logs what it did with a timestamp, so
new ones slot right in.

---

## The operator console (`app.py`)

The Streamlit app (`app.py`) is the **operator console**. It is not required to
run the guardrail (`main.py` is), but it runs on your **real** data through the
same core modules (`config.py`, `query.py`, `fivetran_api.py`, `triggers.py`,
`state.py`) and can fire the same real actions — so what you see is what the
unattended run does. It's built to **scale**: it surfaces the handful of
connectors that need attention rather than making you scroll hundreds.

```bash
streamlit run app.py
```

- **Inventory-first.** It starts from **all the connectors you have** — the
  roster Fivetran reports via the REST API — not just the ones with MAR this
  month. Paused connectors and ones with zero recent MAR still appear. A
  low-prominence **Demo** toggle switches to a bundled sample roster.
- **On/off control.** Each connector has an **Active** switch (the roster's
  `paused` state). Toggle it and **Apply** to pause/resume via the Fivetran REST
  API. This is **gated**: simulated (logged) by default; tick **"Actually apply
  on/off changes"** to write live.
- **Two detection signals.** Each connector is scored on **budget** (month-to-date
  MAR vs limit → OVER / NEAR (≥80%) / OK) *and* **anomaly** (a day's MAR vs the
  connector's own trailing baseline → an early warning even while the monthly
  total is still fine). A per-connector **daily MAR chart** flags the spike.
- **Exception-focused table.** Shown **most-at-risk-first**, filterable by
  Exceptions / Over / Anomalies / Paused, by service, or search. This is what
  scales to hundreds — you never scroll a wall.
- **Editable limits.** A connector's limit defaults from `config.py` (else a UI
  **Default monthly MAR limit**), but you can **edit it inline** in the table and
  **Apply** — status and anomaly baselines recompute. MAR + daily history come
  from the Platform Connector's `incremental_mar`; roster + switches from the REST API.
- **Run guardrail pass.** Evaluates **every** connector via the *same*
  `main.evaluate()` the CLI uses, alerts on the **over-limit**, and logs an
  early-warning for **anomalies** — to the **activity log**. Alert dispatch is
  **simulated** by default; tick **"Actually send Slack / Email / Webhook alerts"**
  to route those through the real `trigger_*` functions from `config.py`.
- **Debug panel.** A **Debug** button toggles an inline panel showing the
  pre-flight **system state** (config, DB connection, MAR table, Fivetran API) —
  with the *exact* reason when something fails — plus the captured console.
- The console never writes back to `config.py`.

> On a **free** Fivetran account there are no PAID rows (only `SYSTEM` metadata),
> so the live view is empty by design. The Debug panel has an escape hatch
> (*View SYSTEM rows* / *All time*) to see that data while testing.
>
> **Demo mode** (the default when no database is configured) runs the whole
> console — including anomaly detection on generated daily history — with zero
> setup, so you can see everything before wiring up real credentials.

---

## Known limitations

- **History mode MAR isn't visible here.** The Platform Connector's
  `incremental_mar` table doesn't break out history-mode rows, so connectors
  using history mode may show usage this framework can't fully see.
- **No cost figures, by design.** This tool tracks rows (MAR), not dollars. It
  does not calculate cost or reproduce Fivetran's pricing. For cost
  implications, use the official
  [Fivetran pricing estimator](https://www.fivetran.com/pricing) rather than
  inferring spend from the numbers here.
- **Connector-name matching.** Pausing relies on the connection name in your
  MAR data matching Fivetran's connection (`schema`) name. If your naming
  differs, adjust the lookup in `fivetran_api.py`.
