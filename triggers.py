# triggers.py
#
# One function per action the framework can take when a connector exceeds its
# MAR limit. They all follow the SAME shape on purpose:
#
#   def trigger_xxx(connection_name, current_mar, limit, ...channel config):
#       ... do the thing ...
#       _log("...")   # always leave a timestamped breadcrumb
#
# TO ADD A NEW TRIGGER (e.g. PagerDuty, Teams, a database row):
#   1. Copy one of these functions and change the body.
#   2. Add its name to the "triggers" list of any connector in config.py.
#   3. Wire the name -> function in main.py's dispatch table.
# That's the whole contract — three small edits, all obvious.
#
# TO CHANGE WHAT AN ALERT SAYS: edit the message string inside the relevant
# function below. The shared _build_message() gives every channel the same
# wording by default; override it per-channel if you want.

import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import requests

from fivetran_api import pause_connector


def _log(message):
    """Print one timestamped line. Every trigger calls this so the console (and
    anything capturing stdout, like cron mail or a log file) shows exactly what
    fired and when."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def _build_message(connection_name, current_mar, limit):
    """The default alert wording, shared by every channel. Edit here to change
    the message everywhere at once, or ignore it and write per-channel copy."""
    overage = current_mar - limit
    return (
        f"MAR limit exceeded for '{connection_name}': "
        f"{current_mar:,} paid MAR this month vs a limit of {limit:,} "
        f"(over by {overage:,})."
    )


def trigger_pause(connection_name, current_mar, limit):
    """Pause the connector in Fivetran. The hard stop — use this when you want
    spend to actually halt, not just be alerted on. Pausing is reversible from
    the Fivetran dashboard (or by editing fivetran_api.py)."""
    ok = pause_connector(connection_name)
    status = "paused connector" if ok else "PAUSE FAILED (see message above)"
    _log(f"[pause] {status} — {_build_message(connection_name, current_mar, limit)}")


def trigger_slack(connection_name, current_mar, limit, webhook_url):
    """Post the alert to a Slack incoming webhook. To change formatting (add
    emoji, @-mentions, blocks), edit the `payload` dict — Slack accepts its
    Block Kit JSON here too."""
    payload = {"text": f":rotating_light: {_build_message(connection_name, current_mar, limit)}"}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        _log(f"[slack] alert sent for '{connection_name}'.")
    except requests.exceptions.RequestException as exc:
        # Caught so one bad webhook can't stop the other connectors' checks.
        _log(f"[slack] FAILED to alert for '{connection_name}': {exc}")


def trigger_email(connection_name, current_mar, limit, smtp_config):
    """Send the alert over SMTP. To change the subject or body, edit the lines
    below. smtp_config comes straight from EMAIL_SMTP_CONFIG in config.py."""
    body = _build_message(connection_name, current_mar, limit)
    msg = MIMEText(body)
    msg["Subject"] = f"[MAR Guardrail] {connection_name} over limit"
    msg["From"] = smtp_config["from_addr"]
    msg["To"] = ", ".join(smtp_config["to_addrs"])
    try:
        # STARTTLS is the common case (port 587). Switch to SMTP_SSL if your
        # provider requires implicit TLS on port 465.
        with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30) as server:
            server.starttls()
            server.login(smtp_config["username"], smtp_config["password"])
            server.sendmail(
                smtp_config["from_addr"], smtp_config["to_addrs"], msg.as_string()
            )
        _log(f"[email] alert sent for '{connection_name}' to {msg['To']}.")
    except (smtplib.SMTPException, OSError) as exc:
        # OSError covers connection/timeout problems; both are logged, not raised.
        _log(f"[email] FAILED to alert for '{connection_name}': {exc}")


def trigger_webhook(connection_name, current_mar, limit, url, auth_header):
    """POST the alert to any HTTP endpoint (PagerDuty, Opsgenie, an internal
    service). To change the payload shape an endpoint expects, edit `payload`;
    to change auth, edit how `headers` is built."""
    payload = {
        "connection_name": connection_name,
        "current_mar": current_mar,
        "limit": limit,
        "message": _build_message(connection_name, current_mar, limit),
    }
    headers = {"Authorization": auth_header} if auth_header else {}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        _log(f"[webhook] alert POSTed for '{connection_name}' to {url}.")
    except requests.exceptions.RequestException as exc:
        _log(f"[webhook] FAILED to alert for '{connection_name}': {exc}")
