# fivetran_api.py
#
# The one piece of the framework that changes state in Fivetran: pausing a
# connector that has blown past its MAR limit.
#
# Fivetran REST API reference:
#   https://fivetran.com/docs/rest-api/api-reference/connections
# Auth is HTTP Basic with your API key as the username and API secret as the
# password (see config.py).
#
# Fivetran's MAR table refers to connectors by NAME, but the REST API pauses
# them by ID — so we look the ID up first, then patch it. If you'd rather
# RESUME a connector instead of pausing it, the only change is the JSON body:
# send {"paused": False} where we send {"paused": True} below (see the marked
# line in pause_connector).

import requests

from config import FIVETRAN_API_KEY, FIVETRAN_API_SECRET

# Obvious leftovers from .env.example — treat these as "not configured" so the
# debug panel doesn't claim the API is set up when it plainly isn't.
_PLACEHOLDER_CREDS = ("", "your_fivetran_api_key", "your_fivetran_api_secret")

# Base URL for Fivetran's v1 REST API. Pinned as a constant so there's a
# single place to change it if Fivetran ever versions the path.
FIVETRAN_BASE_URL = "https://api.fivetran.com/v1"

# Basic-auth tuple reused by every request. requests turns this into the
# Authorization header for us.
_AUTH = (FIVETRAN_API_KEY, FIVETRAN_API_SECRET)

# Don't let a hung Fivetran request stall the whole guardrail run.
_TIMEOUT_SECONDS = 30


def check_api():
    """Probe Fivetran reachability without raising. Returns a dict the debug
    panel renders:

        {"ok": bool,          # creds set AND the API answered 2xx
         "configured": bool,  # are real (non-placeholder) creds present?
         "error": str | None} # exact reason when not ok

    Used for observability only — it never pauses anything."""
    if FIVETRAN_API_KEY in _PLACEHOLDER_CREDS or FIVETRAN_API_SECRET in _PLACEHOLDER_CREDS:
        return {"ok": False, "configured": False,
                "error": "Fivetran API key/secret not set (still placeholder)."}
    try:
        resp = requests.get(
            f"{FIVETRAN_BASE_URL}/groups", auth=_AUTH, timeout=_TIMEOUT_SECONDS
        )
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "configured": True, "error": f"Could not reach Fivetran: {exc}"}

    if resp.status_code == 401:
        return {"ok": False, "configured": True,
                "error": "Fivetran rejected the credentials (HTTP 401)."}
    if not resp.ok:
        return {"ok": False, "configured": True,
                "error": f"Fivetran returned HTTP {resp.status_code}: {resp.text[:200]}"}
    return {"ok": True, "configured": True, "error": None}


def _find_connection_id(connection_name):
    """Resolve a connection_name to its Fivetran connection id, or None.

    Walks every group/destination in the account and matches on the
    connection's schema name (what the MAR table calls connection_name). If
    your naming doesn't line up, this is the matching logic to adjust.
    """
    # Groups (a.k.a. destinations) are the top level of Fivetran's hierarchy;
    # connections live inside them, so we page through each group's
    # connections looking for our name.
    groups_resp = requests.get(
        f"{FIVETRAN_BASE_URL}/groups", auth=_AUTH, timeout=_TIMEOUT_SECONDS
    )
    groups_resp.raise_for_status()

    for group in groups_resp.json().get("data", {}).get("items", []):
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            conns_resp = requests.get(
                f"{FIVETRAN_BASE_URL}/groups/{group['id']}/connections",
                auth=_AUTH,
                params=params,
                timeout=_TIMEOUT_SECONDS,
            )
            conns_resp.raise_for_status()
            data = conns_resp.json().get("data", {})

            for conn in data.get("items", []):
                # `schema` is Fivetran's name for the connection; it matches
                # connection_name in incremental_mar for most setups.
                if conn.get("schema") == connection_name:
                    return conn.get("id")

            cursor = data.get("next_cursor")
            if not cursor:
                break

    return None


def _as_bool(value):
    """Coerce an API value to a real bool. Guards against a footgun: a JSON
    string "false" is truthy in Python, so bool("false") is True — which would
    make every connector look paused. Treat only genuine true-ish values as True;
    "false"/None/"" become False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "t")
    return bool(value)


def list_connections():
    """Return the full connector roster from Fivetran (the REST API).

    This is "here are all the connectors you have" — including paused ones and
    ones with no recent MAR — which the dashboard uses as its inventory and to
    drive the on/off (pause/resume) toggles. Each item:

        {"name", "service", "paused", "sync_state", "group_id", "connection_id"}

    `name` is Fivetran's connection schema, matching incremental_mar.connection_name
    so MAR joins onto it. Raises on API error (the caller surfaces it)."""
    items = []
    groups_resp = requests.get(
        f"{FIVETRAN_BASE_URL}/groups", auth=_AUTH, timeout=_TIMEOUT_SECONDS
    )
    groups_resp.raise_for_status()

    for group in groups_resp.json().get("data", {}).get("items", []):
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            conns_resp = requests.get(
                f"{FIVETRAN_BASE_URL}/groups/{group['id']}/connections",
                auth=_AUTH, params=params, timeout=_TIMEOUT_SECONDS,
            )
            conns_resp.raise_for_status()
            data = conns_resp.json().get("data", {})
            for conn in data.get("items", []):
                status = conn.get("status") or {}
                items.append({
                    "name": conn.get("schema"),
                    "service": conn.get("service"),
                    "paused": _as_bool(conn.get("paused")),
                    "sync_state": status.get("sync_state"),
                    "group_id": group["id"],
                    "connection_id": conn.get("id"),
                })
            cursor = data.get("next_cursor")
            if not cursor:
                break
    return items


def set_paused(connection_name, paused):
    """Pause (paused=True) or resume (paused=False) the named connector. Returns
    True on success. Never raises — failures are caught and printed so a bulk
    apply keeps going for the other connectors.

    This is the single 'turn it on/off' call: the dashboard's toggles route
    here, and pause_connector() below is just set_paused(name, True)."""
    verb = "Paused" if paused else "Resumed"
    try:
        connection_id = _find_connection_id(connection_name)
        if connection_id is None:
            print(f"  [fivetran] Could not find a connection named "
                  f"'{connection_name}' — nothing changed.")
            return False
        resp = requests.patch(
            f"{FIVETRAN_BASE_URL}/connections/{connection_id}",
            auth=_AUTH, json={"paused": bool(paused)}, timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        print(f"  [fivetran] {verb} '{connection_name}' (id={connection_id}).")
        return True
    except requests.exceptions.RequestException as exc:
        detail = ""
        if exc.response is not None:
            detail = f" — Fivetran said: {exc.response.text}"
        print(f"  [fivetran] Failed to {verb.lower()[:-1]} '{connection_name}': "
              f"{exc}{detail}\n  Check your API key/secret and the connector name.")
        return False


def pause_connector(connection_name):
    """Pause the named connector. Thin wrapper kept for the trigger dispatch in
    triggers.py / main.py — equivalent to set_paused(name, True)."""
    return set_paused(connection_name, True)
