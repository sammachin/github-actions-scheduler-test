#!/usr/bin/env python3
"""Analyze logs/slot-probe.log.

The slot-probe workflow schedules 288 distinct daily crons (one per 5-minute
slot). Each run records the slot it belongs to, so this report can distinguish:

  * dropped  -- a slot with no run on a given day
  * delayed  -- a run whose start is well after its slot's intended time
  * overlap  -- a run that started at/after the *next* slot (delay >= 5 min),
                i.e. the delay "ran into" the following job

With --reconcile it also cross-checks the log against the GitHub API run list to
separate slots that were *genuinely dropped* (no run ever created) from slots
that *ran but failed to log* (e.g. a push race), which otherwise look identical
in the log.

Usage:
    python3 scripts/slot_report.py [logs/slot-probe.log]
    python3 scripts/slot_report.py --reconcile [--repo owner/name] [logs/...]

--reconcile reads a token from $GITHUB_TOKEN / $GH_TOKEN if present (raises the
API rate limit; not required for a public repo). Pure standard library.
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from collections import defaultdict, Counter

SLOTS_PER_DAY = 288
SLOT_SECONDS = 300  # 5 minutes
ALL_SLOTS = [f"{h:02d}:{m:02d}" for h in range(24) for m in range(0, 60, 5)]
WORKFLOW_FILE = "slot-probe.yml"


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


def floor_slot(dt):
    """Floor a timestamp to its 5-minute slot boundary."""
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


def load_lines(lines):
    rows = []
    for line in lines:
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


def load(path):
    with open(path) as f:
        return load_lines(f)


def load_from_git(ref, path):
    """Load the log from a git ref (e.g. origin/main), avoiding a stale local copy."""
    subprocess.run(["git", "fetch", "-q", "origin"], check=False,
                   stderr=subprocess.DEVNULL)
    out = subprocess.check_output(["git", "show", f"{ref}:{path}"],
                                  text=True, stderr=subprocess.DEVNULL)
    return load_lines(out.splitlines())


def report(rows, path):
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
    print("\nmissing (unlogged) slots on full days:")
    full_days = days[1:-1] if len(days) > 2 else []
    if not full_days:
        print("  (need at least one full day of data)")
    for d in full_days:
        present = {r["intended"].strftime("%H:%M") for r in by_day[d]}
        missing = [s for s in ALL_SLOTS if s not in present]
        if not missing:
            print(f"  {d}: 0 missing (all 288 logged)")
        else:
            preview = ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else "")
            print(f"  {d}: {len(missing)} missing -> {preview}")

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


# --------------------------------------------------------------------------
# Reconciliation against the GitHub API
# --------------------------------------------------------------------------

def detect_repo():
    try:
        url = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def gh_get(url, token):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "slot-report",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_runs(repo, token):
    runs, page = [], 1
    while True:
        url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
               f"{WORKFLOW_FILE}/runs?per_page=100&page={page}")
        data = gh_get(url, token)
        batch = data.get("workflow_runs", [])
        runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return runs


def reconcile(rows, repo, token):
    print("\n" + "=" * 60)
    print(f"reconciliation vs GitHub API ({repo}, {WORKFLOW_FILE})")
    print("=" * 60)
    try:
        api = fetch_runs(repo, token)
    except urllib.error.HTTPError as e:
        print(f"  API request failed: HTTP {e.code} {e.reason}")
        if e.code in (403, 429):
            print("  (rate limited -- set GITHUB_TOKEN / GH_TOKEN to raise the limit)")
        return
    except urllib.error.URLError as e:
        print(f"  API request failed: {e.reason}")
        return

    sched = [r for r in api if r.get("event") == "schedule"]
    api_ids = {str(r["id"]) for r in sched}
    log_ids = {r["run_id"] for r in rows if r["run_id"]}

    ran_and_logged = api_ids & log_ids
    ran_not_logged = api_ids - log_ids       # ran but its line never reached the log
    logged_not_api = log_ids - api_ids       # in log but API didn't return it (paging/age)

    print(f"\n  scheduled runs in API : {len(api_ids)}")
    print(f"  scheduled lines in log: {len(log_ids)}")
    print(f"  ran and logged        : {len(ran_and_logged)}")
    print(f"  ran but NOT logged    : {len(ran_not_logged)}   <- executed, line lost (e.g. push race)")
    if logged_not_api:
        print(f"  logged but not in API : {len(logged_not_api)}   (pagination boundary on a live list; harmless)")

    # Conclusion breakdown of the ran-but-not-logged runs (push races fail at the push step).
    by_id = {str(r["id"]): r for r in sched}
    if ran_not_logged:
        concl = Counter(by_id[i].get("conclusion") for i in ran_not_logged)
        print("\n  ran-but-not-logged by conclusion:")
        for c, n in concl.most_common():
            print(f"    {c or 'in_progress':12} {n}")

    # Per-day: logged vs ran-not-logged vs genuinely dropped.
    # A slot is "covered" if it has a log line OR an API run flooring to it.
    logged_slotday = {(r["intended"].date(), r["intended"].strftime("%H:%M")) for r in rows}
    unlogged_slotday = set()
    unlogged_by_day = Counter()
    for i in ran_not_logged:
        started = parse_ts(by_id[i]["run_started_at"])
        slot = floor_slot(started)
        unlogged_slotday.add((slot.date(), slot.strftime("%H:%M")))
        unlogged_by_day[slot.date()] += 1

    days = sorted({sd[0] for sd in logged_slotday} | {sd[0] for sd in unlogged_slotday})
    full_days = days[1:-1] if len(days) > 2 else []

    print("\n  per full day -- genuine drops vs ran-but-not-logged:")
    hdr = f"    {'day':12} {'logged':>6} {'ran_no_log':>10} {'genuine_drop':>12}"
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for d in full_days:
        logged = sum(1 for r in rows if r["intended"].date() == d)
        ranq = unlogged_by_day.get(d, 0)
        covered = {sd[1] for sd in logged_slotday if sd[0] == d} | \
                  {sd[1] for sd in unlogged_slotday if sd[0] == d}
        genuine = SLOTS_PER_DAY - len(covered)
        print(f"    {str(d):12} {logged:>6} {ranq:>10} {genuine:>12}")
    if not full_days:
        print("    (need at least one full day of data)")

    print("\n  note: ran-but-not-logged slots are inferred by flooring run_started_at")
    print("        to the 5-min grid, so a badly delayed run may be attributed to a")
    print("        neighbouring slot. Genuine drops are exact (no API run at all).")


def main():
    ap = argparse.ArgumentParser(description="Analyze slot-probe logs.")
    ap.add_argument("logfile", nargs="?", default="logs/slot-probe.log",
                    help="path to slot-probe.log (default: logs/slot-probe.log)")
    ap.add_argument("--reconcile", action="store_true",
                    help="cross-check the log against the GitHub API run list")
    ap.add_argument("--from-origin", action="store_true",
                    help="read the log from origin/main (git fetch first) instead of "
                         "the local working copy, so it is never stale")
    ap.add_argument("--ref", default="origin/main",
                    help="git ref to read with --from-origin (default: origin/main)")
    ap.add_argument("--repo", help="owner/name (default: from git remote origin)")
    ap.add_argument("--token", help="GitHub token (default: $GITHUB_TOKEN / $GH_TOKEN)")
    args = ap.parse_args()

    if args.from_origin:
        try:
            rows = load_from_git(args.ref, args.logfile)
        except subprocess.CalledProcessError:
            print(f"Could not read {args.ref}:{args.logfile} -- is the ref fetched and the path correct?")
            return 1
        source = f"{args.ref}:{args.logfile}"
    else:
        try:
            rows = load(args.logfile)
        except FileNotFoundError:
            print(f"No log file at {args.logfile} yet -- the slot-probe workflow has not run.")
            return 0
        source = args.logfile
    if not rows:
        print(f"No scheduled slot-probe entries found in {source}.")
        return 0

    report(rows, source)

    if args.reconcile:
        import os
        repo = args.repo or detect_repo()
        if not repo:
            print("\nreconcile: could not determine repo; pass --repo owner/name")
            return 1
        token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        reconcile(rows, repo, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
