# state.py
#
# Observability core: one place that assembles everything the app knows about
# itself at a given moment — config loaded, database reachable, MAR table
# present, Fivetran API reachable — into a single structured snapshot.
#
# This is deliberately core logic (not in app.py): the Streamlit layer only
# renders what snapshot() returns. Nothing here mutates state or fires triggers;
# it is a pure, non-raising read so the debug panel can always show *something*,
# even when the database is down.

from datetime import datetime

import config
from fivetran_api import check_api
from query import check_connection

# Status vocabulary the UI maps to icons/colors. Keep it tiny on purpose.
OK = "ok"        # working
WARN = "warn"    # not configured / optional, but not an error
ERROR = "error"  # broken — and we say exactly why
SKIP = "skip"    # not checked in this mode (e.g. DB checks in demo mode)


def snapshot(data_mode="live"):
    """Return a structured report of current system state.

    data_mode: "live" probes the database and Fivetran; "demo" skips those
    (demo runs on bundled data and needs no connectivity).

    Shape:
        {
          "timestamp": "YYYY-MM-DD HH:MM:SS",
          "data_mode": "live" | "demo",
          "config": {...},      # raw config.config_state()
          "database": {...},    # raw query.check_connection() (live only)
          "fivetran": {...},    # raw fivetran_api.check_api() (live only)
          "checks": [           # flat, render-ready list
              {"label": str, "status": OK|WARN|ERROR|SKIP, "detail": str},
              ...
          ],
        }
    """
    cfg = config.config_state()

    if data_mode == "live":
        db = check_connection()
        api = check_api()
    else:
        db = {"ok": None, "error": None, "table": "—", "table_found": None}
        api = {"ok": None, "configured": None, "error": None}

    checks = []

    # --- config ---
    if cfg["config_valid"]:
        checks.append({"label": "Config loaded", "status": OK,
                       "detail": f"{cfg['connector_count']} connector(s) watched"})
    else:
        checks.append({"label": "Config loaded", "status": ERROR,
                       "detail": cfg["config_error"]})

    checks.append({
        "label": "DATABASE_URL set",
        "status": OK if cfg["database_url_set"] else WARN,
        "detail": "configured" if cfg["database_url_set"]
        else "still a placeholder — set it in .env",
    })

    # --- database (live only) ---
    if data_mode != "live":
        checks.append({"label": "Database connection", "status": SKIP,
                       "detail": "demo mode — database not used"})
        checks.append({"label": f"MAR table ({db['table']})", "status": SKIP,
                       "detail": "demo mode — database not used"})
    else:
        if db["ok"]:
            checks.append({"label": "Database connection", "status": OK,
                           "detail": "connected"})
        else:
            checks.append({"label": "Database connection", "status": ERROR,
                           "detail": db["error"] or "unknown error"})

        if db["table_found"]:
            checks.append({"label": f"MAR table ({db['table']})", "status": OK,
                           "detail": "found"})
        elif db["ok"]:
            checks.append({"label": f"MAR table ({db['table']})", "status": ERROR,
                           "detail": db["error"] or "table not found"})
        else:
            checks.append({"label": f"MAR table ({db['table']})", "status": SKIP,
                           "detail": "skipped — no connection"})

    # --- Fivetran API (live only; needed for the pause trigger) ---
    if data_mode != "live":
        checks.append({"label": "Fivetran API", "status": SKIP,
                       "detail": "demo mode — not contacted"})
    elif api["ok"]:
        checks.append({"label": "Fivetran API", "status": OK, "detail": "reachable"})
    elif not api["configured"]:
        checks.append({"label": "Fivetran API", "status": WARN,
                       "detail": api["error"]})  # optional unless you use 'pause'
    else:
        checks.append({"label": "Fivetran API", "status": ERROR, "detail": api["error"]})

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_mode": data_mode,
        "config": cfg,
        "database": db,
        "fivetran": api,
        "checks": checks,
    }
