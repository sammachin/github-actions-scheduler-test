#!/usr/bin/env python3
"""Analyze logs/slot-probe.log.

The slot-probe workflow schedules 288 distinct daily crons (one per 5-minute
slot). Each run records the slot it belongs to, so this report can distinguish:

  * dropped  -- a slot with no run on a given day
  * delayed  -- a run whose start is well after its slot's intended time
  * overlap  -- a run that started at/after the *next* slot (delay >= 5 min),
                i.e. the delay "ran into" the following job

Usage:  python3 scripts/slot_report.py [logs/slot-probe.log]
Pure standard library; no dependencies.
"""
import sys
import statistics
from datetime import datetime, timezone
from collections import defaultdict

SLOTS_PER_DAY = 288
SLOT_SECONDS = 300  # 5 minutes
ALL_SLOTS = [f"{h:02d}:{m:02d}" for h in range(24) for m in range(0, 60, 5)]


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_line(line):
    fields = {}
    for kv in line.strip().split(" | "):
        if "=" in kv:
            k, v = kv.split("=", 1)
            fields[k.strip()] = v.strip()
    return fields


def pctl(sorted_vals, p):
    if not sorted_vals:
        return 0
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return int(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def fmt(secs):
    secs = int(secs)
    s, m, h = secs % 60, (secs // 60) % 60, secs // 3600
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fl = parse_line(line)
            if fl.get("trigger") != "schedule":
                continue  # skip manual workflow_dispatch runs
            if fl.get("intended_utc", "manual") == "manual":
                continue
            try:
                intended = parse_ts(fl["intended_utc"])
                started = parse_ts(fl["run_started_at"])
            except (KeyError, ValueError):
                continue
            try:
                delay = int(fl["delay_sec"])
            except (KeyError, ValueError):
                delay = int((started - intended).total_seconds())
            rows.append({
                "intended": intended,
                "started": started,
                "delay": delay,
                "schedule": fl.get("schedule", ""),
                "run_id": fl.get("run_id", ""),
            })
    return rows


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "logs/slot-probe.log"
    try:
        rows = load(path)
    except FileNotFoundError:
        print(f"No log file at {path} yet -- the slot-probe workflow has not run.")
        return 0
    if not rows:
        print(f"No scheduled slot-probe entries found in {path}.")
        return 0

    rows.sort(key=lambda r: r["intended"])
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["intended"].date()].append(r)
    days = sorted(by_day)

    print(f"slot-probe report -- {path}")
    print(f"entries: {len(rows)} | days: {len(days)} ({days[0]} .. {days[-1]})")
    print("(first and last day are partial: the schedule was not active for all 288 slots)\n")

    # ---- per-day summary ----
    hdr = f"{'day':12} {'ran':>4}/{SLOTS_PER_DAY}  {'drop%':>6}  {'p50':>7} {'p90':>7} {'max':>8}  {'overlap':>7}"
    print(hdr)
    print("-" * len(hdr))
    for i, d in enumerate(days):
        rs = by_day[d]
        delays = sorted(r["delay"] for r in rs)
        ran = len(rs)
        droppct = 100.0 * (SLOTS_PER_DAY - ran) / SLOTS_PER_DAY
        over = sum(1 for x in delays if x >= SLOT_SECONDS)
        tag = "  (partial)" if i in (0, len(days) - 1) else ""
        print(f"{str(d):12} {ran:>4}/{SLOTS_PER_DAY}  {droppct:>5.1f}%  "
              f"{fmt(pctl(delays,50)):>7} {fmt(pctl(delays,90)):>7} {fmt(delays[-1]):>8}  {over:>7}{tag}")

    # ---- overall delay distribution ----
    alld = sorted(r["delay"] for r in rows)
    print("\ndelay distribution (all scheduled runs):")
    for label, p in [("min", 0), ("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99), ("max", 100)]:
        print(f"  {label:4} {fmt(pctl(alld, p))}")
    print(f"  mean {fmt(int(statistics.mean(alld)))}")

    # ---- dropped slots on full days ----
    print("\nmissing (dropped) slots on full days:")
    full_days = days[1:-1] if len(days) > 2 else []
    if not full_days:
        print("  (need at least one full day of data)")
    for d in full_days:
        present = {r["intended"].strftime("%H:%M") for r in by_day[d]}
        missing = [s for s in ALL_SLOTS if s not in present]
        if not missing:
            print(f"  {d}: 0 dropped (all 288 ran)")
        else:
            preview = ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else "")
            print(f"  {d}: {len(missing)} dropped -> {preview}")

    # ---- overlaps: delay ran into the next slot ----
    overlaps = [r for r in rows if r["delay"] >= SLOT_SECONDS]
    print(f"\noverlaps -- run started at/after its next slot (delay >= 5m): {len(overlaps)}")
    for r in overlaps[:40]:
        n = r["delay"] // SLOT_SECONDS
        print(f"  slot {r['intended'].strftime('%Y-%m-%d %H:%M')} started "
              f"{r['started'].strftime('%H:%M:%S')}  (+{fmt(r['delay'])}, ~{n} slot(s) late)")
    if len(overlaps) > 40:
        print(f"  ... and {len(overlaps) - 40} more")

    # ---- out-of-order execution ----
    ooo = 0
    prev = None
    for r in rows:  # already sorted by intended
        if prev is not None and r["started"] < prev["started"]:
            ooo += 1
        prev = r
    print(f"\nout-of-order starts (a later slot began before an earlier slot): {ooo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
