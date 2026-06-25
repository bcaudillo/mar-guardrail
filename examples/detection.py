# examples/detection.py
#
# PROTOTYPE detection logic for the anomaly demo.
#
# This is where we try out the PRD's OPEN-8 "detection model" BEFORE deciding to
# promote anything into the core (query.py / a future detect.py). Keeping it in
# examples/ is intentional: the exact signal and sensitivity are still an open
# product question, so we prototype here rather than bake a guess into the
# framework.
#
# Two signals, matching PRD §5.2:
#   - threshold : cumulative current-month PAID MAR vs a limit. Mirrors the real
#                 core (query.get_current_mar + main.evaluate), which is already
#                 [VERIFIED] in the framework.
#   - anomaly   : a single day's MAR deviating from the connector's own recent
#                 baseline (trailing-window mean/std). Catches spikes the monthly
#                 limit can't see coming. Illustrative — method/sensitivity are
#                 OPEN-8.

import random
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class DayPoint:
    """One day of a connector's PAID MAR, plus what detection thought of it."""
    day: date
    mar: int
    baseline: float   # trailing-window mean used as the "normal" reference
    multiple: float   # mar / baseline (how many times normal)
    z: float          # standardized deviation from baseline
    is_anomaly: bool


def generate_series(days, baseline=40_000, noise=0.08, spike_day=None,
                    spike_mar=620_000, seed=7):
    """Daily PAID MAR for one connector across `days`, mirroring the shape of
    incremental_mar (one value per measured_date). Optionally inject a spike on
    `spike_day` (0-based index) to reproduce the PRD's worked example.

    Deterministic for a given seed so the demo is stable across reruns."""
    rnd = random.Random(seed)
    start = date.today().replace(day=1)
    series = []
    for i in range(days):
        value = int(baseline * (1 + rnd.uniform(-noise, noise)))
        if spike_day is not None and i == spike_day:
            value = spike_mar
        series.append((start + timedelta(days=i), value))
    return series


def detect(series, limit, window=14, z_threshold=5.0, min_multiple=3.0):
    """Run both detection signals over a daily series.

    Anomaly rule (deliberately simple, and deliberately conservative): a day is
    anomalous only if it is BOTH far from baseline in standard deviations
    (z >= z_threshold) AND a large multiple of baseline (mar >= min_multiple x
    baseline). Requiring both keeps a noisy-but-tiny connector from tripping on
    absolute-small wiggles. This is the knob OPEN-8 is about.

    Returns (points, summary):
      points  : list[DayPoint]
      summary : {"mtd", "pct", "status" (OK/NEAR/OVER), "anomalies": [DayPoint]}
    """
    values = [v for _, v in series]
    points = []
    for i, (d, v) in enumerate(series):
        prior = values[max(0, i - window):i]
        if len(prior) >= 3:
            mean = sum(prior) / len(prior)
            std = (sum((x - mean) ** 2 for x in prior) / len(prior)) ** 0.5
            z = (v - mean) / std if std > 0 else 0.0
            multiple = v / mean if mean > 0 else 0.0
            is_anomaly = z >= z_threshold and multiple >= min_multiple
        else:
            # Not enough history yet to judge — treat as normal.
            mean, z, multiple, is_anomaly = float(v), 0.0, 1.0, False
        points.append(DayPoint(d, v, mean, multiple, z, is_anomaly))

    mtd = sum(values)
    pct = mtd / limit if limit else 0.0
    status = "OVER" if mtd > limit else ("NEAR" if pct >= 0.8 else "OK")
    anomalies = [p for p in points if p.is_anomaly]
    return points, {"mtd": mtd, "pct": pct, "status": status, "anomalies": anomalies}


# ---------------------------------------------------------------------------
# FLEET — the scalability prototype
#
# Some customers run hundreds of connectors. The point these helpers make: the
# whole fleet is evaluated in ONE pass with NO per-connector configuration,
# because anomaly detection learns each connector's own baseline. A hard budget
# limit is derived from a simple *policy* (a multiple of the connector's
# baseline), not hand-set per connector — which is what actually breaks down at
# scale.
# ---------------------------------------------------------------------------

# Buffer applied to baseline to derive a default budget limit, so a connector
# running at its normal rate lands comfortably under its cap. This is the
# "policy default" that replaces hand-setting hundreds of integer limits.
_LIMIT_POLICY_MULTIPLE = 1.5

_DESTINATIONS = ["salesforce", "postgres", "marketing", "finance",
                 "product", "support", "ads"]


def generate_fleet(n=300, days=18, seed=11, anomaly_rate=0.04, over_rate=0.015):
    """Generate a fleet of `n` connectors, each with a daily PAID MAR series.

    A small fraction get an injected spike (anomaly) and another small fraction
    run at a sustained-high rate (budget overage) — so the demo has a realistic
    handful of exceptions hiding in a large, mostly-healthy fleet.

    Returns {connection_name: {"destination", "baseline", "series"}}.
    """
    rnd = random.Random(seed)
    fleet = {}
    for i in range(n):
        dest = rnd.choice(_DESTINATIONS)
        name = f"{dest}_conn_{i:03d}"
        baseline = int(rnd.choice([5_000, 12_000, 40_000, 90_000, 150_000])
                       * rnd.uniform(0.7, 1.4))

        is_over = rnd.random() < over_rate
        is_anomalous = (not is_over) and rnd.random() < anomaly_rate

        # Sustained level: over-budget connectors simply run hot all month.
        level = int(baseline * (rnd.uniform(1.7, 2.2) if is_over
                                else rnd.uniform(0.6, 1.1)))
        spike_day = rnd.randint(max(3, days - 6), days - 1) if is_anomalous else None
        spike_mar = int(baseline * rnd.uniform(8, 25)) if is_anomalous else None

        series = generate_series(days, baseline=level, noise=0.1,
                                 spike_day=spike_day, spike_mar=spike_mar,
                                 seed=rnd.randint(0, 10 ** 6))
        fleet[name] = {"destination": dest, "baseline": baseline, "series": series}
    return fleet


def evaluate_fleet(fleet, days, window=14, z_threshold=5.0, min_multiple=3.0):
    """Evaluate every connector in the fleet in one pass.

    No per-connector config: the budget limit is derived from policy
    (baseline x _LIMIT_POLICY_MULTIPLE x days) and the anomaly baseline is the
    connector's own trailing window. Returns a list of per-connector dicts,
    each with a `risk` score so the UI can surface exceptions first.
    """
    rows = []
    for name, info in fleet.items():
        limit = int(info["baseline"] * _LIMIT_POLICY_MULTIPLE * days)
        points, summary = detect(info["series"], limit, window=window,
                                 z_threshold=z_threshold, min_multiple=min_multiple)
        today = points[-1]
        worst = max(points, key=lambda p: p.multiple)  # biggest deviation in the window
        is_anom = bool(summary["anomalies"])
        # Risk: anomalies and overages float to the top, then by % of limit.
        risk = (2_000 if is_anom else 0) + (1_000 if summary["status"] == "OVER" else 0) \
            + summary["pct"] * 100
        rows.append({
            "connector": name,
            "destination": info["destination"],
            "mtd": summary["mtd"],
            "limit": limit,
            "pct": summary["pct"],
            "status": summary["status"],
            "anomaly": is_anom,
            "today": today.mar,
            "baseline": today.baseline,
            # The worst day in the window — what the ANOMALY flag is actually about.
            "worst_day": worst.day,
            "worst_mar": worst.mar,
            "worst_baseline": worst.baseline,
            "worst_multiple": worst.multiple,
            "risk": risk,
        })
    return rows

