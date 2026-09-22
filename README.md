# github-actions-scheduler-test

A minimal repo for observing how GitHub Actions **scheduled** (`cron`) workflows
actually behave. Four independent workflows each append a line to their own log
file in `logs/` every time they run, then commit and push it back to the repo.

## Workflows

| Workflow | Cron (UTC) | Intended cadence | Log file |
|---|---|---|---|
| [`every-5-min`](.github/workflows/every-5-min.yml)   | `3,8,13,...,58 * * * *` | every 5 min, offset to :03/:08/… | `logs/every-5-min.log` |
| [`every-30-min`](.github/workflows/every-30-min.yml) | `12,42 * * * *`| every 30 min, at :12 and :42 | `logs/every-30-min.log` |
| [`every-4-hours`](.github/workflows/every-4-hours.yml)| `19 */4 * * *` | every 4 hours, at :19 (00:19, 04:19, …) | `logs/every-4-hours.log` |
| [`daily`](.github/workflows/daily.yml)               | `47 6 * * *`   | once a day at 06:47 | `logs/daily.log` |

The minutes are deliberately offset off the common `:00`/`:30` ticks (where
GitHub's shared cron infrastructure is busiest and most delayed) and staggered
so the four workflows don't all fire in the same minute.

Each workflow also has `workflow_dispatch`, so you can trigger it by hand from
the **Actions** tab to confirm it works without waiting for the schedule.

## What gets logged

One line per run, for example:

```
actual_run=2026-09-22T17:08:12Z | run_started_at=2026-09-22T17:08:08Z | scheduled_cron=3,8,13,18,23,28,33,38,43,48,53,58 * * * * | trigger=schedule | run_id=1234567890
```

- **`actual_run`** — wall-clock UTC time the log step executed.
- **`run_started_at`** — GitHub's own `github.run_started_at` for the run
  (closer to when the run was actually dispatched).
- **`scheduled_cron`** — `github.event.schedule`: the cron expression that
  triggered this run. This is the closest thing GitHub exposes to "what it was
  scheduled as." On a manual run it shows `manual`.
- **`trigger`** — `schedule` or `workflow_dispatch`.
- **`run_id`** — links back to the run in the Actions tab.

> **On "scheduled to run" time:** GitHub does **not** expose the exact intended
> tick timestamp (e.g. "17:05:00") to the workflow — only the cron _expression_
> via `github.event.schedule`. Comparing that expression + `run_started_at`
> against `actual_run` is how you measure GitHub's scheduling drift, which is
> the whole point of this repo.

## Known GitHub scheduling caveats (expected, not bugs)

- **Cron is UTC only.** No timezone support.
- **Delays are normal.** Scheduled runs are queued on shared infrastructure and
  are frequently late — the 5-minute job in particular will often skip or bunch
  up during high-load periods. GitHub only promises best-effort, not precision.
- **Auto-disable after 60 days.** If the repo has no commit activity for 60
  days, scheduled workflows are paused (you get an email to re-enable). Because
  these workflows commit on every run, an active repo stays enabled.
- **First schedule takes a while.** After you push, the very first scheduled
  trigger can take several minutes (up to ~15) to register.

## Push races

All four workflows commit to the same branch. They write to *separate* log
files (no content conflicts), and the push step does `git pull --rebase` with a
short retry loop to survive two workflows pushing at nearly the same time.

## Setup

1. Create an empty repo on GitHub (no README).
2. From this folder:

   ```bash
   git init
   git add .
   git commit -m "initial scheduler test scaffold"
   git branch -M main
   git remote add origin git@github.com:<you>/github-actions-scheduler-test.git
   git push -u origin main
   ```

3. Scheduled workflows only run on the **default branch**, and only once the
   workflow files exist there — so they start ticking after the first push.
4. Optionally open the **Actions** tab and run each workflow once via
   *Run workflow* to confirm the log + commit flow works immediately.
