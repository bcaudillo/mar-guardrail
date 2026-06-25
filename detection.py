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
            multiple = v / mean if mean > 0 else 0.0
            if std > 0:
                z = (v - mean) / std
                significant = z >= z_threshold
            else:
                # Flat baseline (zero variance): any rise above it is off-pattern
                # by definition. The multiple test below stops trivial changes
                # from firing.
                z = 0.0
                significant = v > mean
            is_anomaly = significant and multiple >= min_multiple
        else:
            # Not enough history yet to judge — treat as normal.
            mean, z, multiple, is_anomaly = float(v), 0.0, 1.0, False
        points.append(DayPoint(d, v, mean, multiple, z, is_anomaly))

    mtd = sum(values)
    pct = mtd / limit if limit else 0.0
    status = "OVER" if mtd > limit else ("NEAR" if pct >= 0.8 else "OK")
    anomalies = [p for p in points if p.is_anomaly]
    return points, {"mtd": mtd, "pct": pct, "status": status, "anomalies": anomalies}
