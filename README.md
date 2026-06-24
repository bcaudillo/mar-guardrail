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

## The Streamlit demo console

The Streamlit app (`app.py`) is a **demo / UI layer only** — it is not required
to run the framework (`main.py` is). All real logic stays in the core modules
(`config.py`, `query.py`, `fivetran_api.py`, `triggers.py`, `state.py`); the app
only renders what they return.

```bash
streamlit run app.py
```

- **Live by default.** On load it reads real current-month **PAID** MAR from your
  destination, grouped by schema. A low-prominence **Demo** toggle switches to
  bundled sample data for layout/testing without a database.
- **Debug panel.** A **Debug** button toggles an inline panel showing the
  pre-flight **system state** (config loaded, database connection, MAR table,
  Fivetran API reachable) — with the *exact* reason when something fails — plus
  the captured console log. No popups.
- **Connectors** are grouped by schema, multi-select, and scroll vertically
  (~10 rows). Each selected connector takes an **integer** MAR limit (no
  percentages, no cost math) and shows a live OVER/OK status.
- The demo's selections and limits are independent UI state; it never writes
  back to `config.py`.

> On a **free** Fivetran account there are no PAID rows (only `SYSTEM` metadata),
> so the live view is empty by design. The Debug panel has an escape hatch
> (*View SYSTEM rows* / *All time*) to see that data while testing.

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
