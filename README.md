# Hudson Sailing Monitor

Polls the Hudson Sailing Club calendar every 15 minutes and sends a push
notification (via [ntfy.sh](https://ntfy.sh)) when an event matching a keyword
has an open spot.

## How it works

1. A GitHub Actions cron job runs `monitor.py` every 15 minutes.
2. The script fetches the calendar for each date in your configured range,
   finds events whose title contains your keyword, and parses the
   `N/M attending` text.
3. It compares against `state/seen.json` (committed back to the repo each
   run) and posts to your ntfy topic when an event transitions from full to
   open, or the first time it sees an event that's already open.

## One-time setup

### 1. Set up ntfy (~2 minutes)

1. Install the **ntfy** app on your phone (iOS / Android).
2. Pick a topic name — make it **long and unguessable**, since anyone with the
   name can read your notifications. Example: `hudson-sail-alerts-7f3k9q2`.
3. In the app, tap **+** and subscribe to that topic.
4. (Optional) For email: open `https://ntfy.sh/<your-topic>` in a browser,
   click the bell icon, and add an email forward. Or send notifications with
   the `Email:` header — but the simplest path is the phone app, which already
   covers push + lets you forward to email from the app settings.

### 2. Push this project to GitHub

```bash
cd hudson-sailing-monitor
git init
git add .
git commit -m "Initial commit"
gh repo create hudson-sailing-monitor --private --source=. --push
```

(Or create the repo on github.com and push manually.) **Use a private repo**
— the state file is harmless, but there's no reason to make your monitoring
public.

### 3. Configure secrets and variables

In your repo on github.com, go to **Settings → Secrets and variables → Actions**:

**Secrets** tab → New repository secret (one each):
- `NTFY_TOPIC` → your ntfy topic name (e.g. `hudson-sail-alerts-7f3k9q2`)
- `HUDSON_USERNAME` → your sailing-club login email
- `HUDSON_PASSWORD` → your sailing-club password

> **Security note.** GitHub secrets are encrypted at rest, only exposed to
> your own workflows, and never printed in logs. That said, your password
> is sitting in a third-party system. The recommended mitigation is to
> create a **separate account on the sailing site with a unique password**
> used only for this monitor — that way the worst-case blast radius is
> "someone can view the sailing calendar." Don't reuse a password you use
> anywhere else.

**Variables** tab → New repository variable (one each):
- `EVENT_KEYWORD` → `The Morning Race`
- `WEEKDAYS` → `Wed` (also accepts e.g. `Sat,Sun` or numeric `0,2,4` where Mon=0)
- `LOOKAHEAD_DAYS` → `30`

The script auto-computes "all Wednesdays in the next 30 days" each run — it's a
rolling window, so you never have to update dates by hand.

### 4. Run it once manually to confirm

Go to **Actions** tab → **Monitor Hudson Sailing Events** → **Run workflow**.

Watch the logs. You should see something like:

```
2026-05-20: found 1 matching event(s).
  -> NOTIFIED: The Morning Race (4/5)
Done. 1 notification(s) sent.
```

…and a push on your phone. After that, the cron takes over.

## Tweaking

**Different keyword, weekdays, or window?** Update the Variables in the repo
settings — no code change needed.

  - `EVENT_KEYWORD`: any substring to match in event titles
  - `WEEKDAYS`: e.g. `Wed`, `Sat,Sun`, or numeric `0,2,4` (Mon=0..Sun=6)
  - `LOOKAHEAD_DAYS`: how many days ahead to scan (default 30)

**Parsing breaks?** The site blocks automated previewing, so I wrote the
HTML parser using a robust "find the attending text, walk up to find the
title" heuristic instead of relying on specific CSS classes. If it misses
events, the fix is usually in `parse_events()` in `monitor.py`. You can run
the script locally against a saved copy of the page:

```bash
pip install -r requirements.txt
export HUDSON_USERNAME=you@example.com
export HUDSON_PASSWORD='your-password'
export NTFY_TOPIC=test-topic-ignore
export WEEKDAYS=Wed
export LOOKAHEAD_DAYS=30
python monitor.py
```

**Want to test notifications without an event opening?** Temporarily delete
`state/seen.json` and run the workflow — the first observation of any open
event will trigger a notification.

## Notes & caveats

- **GitHub Actions cron is best-effort.** It usually runs on time but can be
  delayed by minutes during peak load. For a sailing club calendar checked
  every 15 minutes, that's fine.
- **Free tier limits:** public repos get unlimited Actions minutes; private
  repos get 2,000 free minutes/month. This job uses ~15 seconds per run × 4
  runs/hour × 24h × 30 days ≈ 300 minutes/month (4–5 Wednesdays fetched per
  run). You're well under.
- **Be polite to the site.** One request per date per 15 minutes is light
  traffic, but don't crank the schedule way up.
- **Notifications fire on full→open transitions** (so you don't get spammed
  every 15 min while a spot stays open). You'll get one alert when a spot
  appears; if it fills and reopens, you'll get another.
