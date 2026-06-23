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

# Base URL for Fivetran's v1 REST API. Pinned as a constant so there's a
# single place to change it if Fivetran ever versions the path.
FIVETRAN_BASE_URL = "https://api.fivetran.com/v1"

# Basic-auth tuple reused by every request. requests turns this into the
# Authorization header for us.
_AUTH = (FIVETRAN_API_KEY, FIVETRAN_API_SECRET)

# Don't let a hung Fivetran request stall the whole guardrail run.
_TIMEOUT_SECONDS = 30


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


def pause_connector(connection_name):
    """Pause the named connector in Fivetran. Returns True on success.

    Never raises — any failure is caught and printed so the calling guardrail
    loop keeps running for the other connectors. Returns False on any error.
    """
    try:
        connection_id = _find_connection_id(connection_name)
        if connection_id is None:
            print(
                f"  [fivetran] Could not find a connection named "
                f"'{connection_name}' in your Fivetran account — nothing paused."
            )
            return False

        resp = requests.patch(
            f"{FIVETRAN_BASE_URL}/connections/{connection_id}",
            auth=_AUTH,
            json={"paused": True},  # <-- swap to False here to RESUME instead.
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        print(f"  [fivetran] Paused '{connection_name}' (id={connection_id}).")
        return True

    except requests.exceptions.RequestException as exc:
        # Covers timeouts, connection errors, and non-2xx responses. We print a
        # human-readable message (including Fivetran's response body when there
        # is one) rather than crashing the run.
        detail = ""
        if exc.response is not None:
            detail = f" — Fivetran said: {exc.response.text}"
        print(
            f"  [fivetran] Failed to pause '{connection_name}': {exc}{detail}\n"
            f"  Check your API key/secret in config.py and that the connector "
            f"name matches Fivetran."
        )
        return False
