# internship-monitor

A GitHub Actions bot that watches 20 company career pages for internship openings and emails you
when one appears. Every 6 hours it renders each page in headless Chromium, keyword-matches the
visible text, and compares the result against JSON snapshots committed back to this repository —
so the workflow remembers what it has already seen between runs. New matches are emailed through
the [Resend](https://resend.com) API, one email per newly detected opening.

## Setup

1. Create a GitHub repository containing these files and push it.
2. Create a Resend API key at <https://resend.com/api-keys>.
3. Add the repository secrets (Settings → Secrets and variables → Actions → New repository
   secret):
   - `RESEND_API_KEY` — the key from step 2.
   - `ALERT_EMAIL` — where alerts go.
   - Optionally `ALERT_FROM` — override the default sender
     `Internship Monitor <onboarding@resend.dev>`.
4. Make sure Actions are enabled for the repository (Actions tab → enable workflows).
5. Trigger one run: Actions → **Internship Monitor** → **Run workflow**.

After that it runs on its own schedule (`0 */6 * * *`, i.e. every 6 hours, UTC).

## Secrets

| Secret | Required | Purpose |
| --- | --- | --- |
| `RESEND_API_KEY` | yes | Resend API key used to send the alert emails. |
| `ALERT_EMAIL` | yes | Recipient address. Comma-separate for multiple recipients. |
| `ALERT_FROM` | no | Sender address. Must be an address at a domain verified in Resend. |

## First run vs later runs

The first run has no snapshots to compare against, so it sends **one baseline digest** listing every
currently open matching role across all companies (subject
`🚨 Internship Monitor — Baseline: N matching roles across M companies`). Every run after that
sends **one email per newly detected opening** — a keyword that was absent from the snapshot and is
now present on the page — with subject `🚨 New Intern Role Detected — <Company>`. A company you add
to `companies.json` later is folded into a single baseline digest on its own first run.

## Adding a company

Edit `companies.json` and append an entry:

```json
{
  "name": "Example Corp",
  "url": "https://example.com/careers",
  "keywords": ["Intern", "2027"]
}
```

The snapshot filename is derived from the name — `Walmart Labs` becomes
`snapshots/walmart-labs.json` — and is created automatically on the first run; nothing to create by
hand. Keywords are **case-insensitive substring** matches, so a short generic keyword like `Intern`
also matches `Internship` and `International`. Removing a company from `companies.json` leaves its
snapshot file behind; delete the file manually if you want it gone.

## Local run

```bash
pip install -r requirements.txt
python -m playwright install chromium
python monitor.py
```

Alerts need the same environment variables the workflow passes in. PowerShell:

```powershell
$env:RESEND_API_KEY="re_..."
$env:ALERT_EMAIL="you@example.com"
python monitor.py
```

bash:

```bash
export RESEND_API_KEY="re_..."
export ALERT_EMAIL="you@example.com"
python monitor.py
```

Without those variables the run still fetches pages and updates snapshots; it just logs
`RESEND_API_KEY/ALERT_EMAIL not set — email skipped` instead of sending.

## Files

- `companies.json` — the monitored companies, their search URLs, and their keywords.
- `monitor.py` — rendering, keyword matching, snapshot diffing, and email sending.
- `snapshots/` — one JSON file per company (`<slug>.json`) plus `_state.json`, a heartbeat written
  every run.
- `.github/workflows/monitor.yml` — the scheduled workflow: runs the monitor, then commits snapshots.
- `requirements.txt` — Python dependencies installed by the workflow.

## Notes and limits

- Cron runs in UTC, and GitHub may delay scheduled runs when the platform is busy.
- GitHub disables scheduled workflows after 60 days without repository activity. The heartbeat
  commit each run keeps the repo active, but if the schedule ever stops, re-enable it from the
  Actions tab.
- A company is skipped when its page fails to render or yields fewer than 200 characters of text;
  its snapshot is left untouched so the next run retries it.
- The default sender `onboarding@resend.dev` is Resend's test address. If alerts do not arrive,
  verify your own domain in Resend and set `ALERT_FROM`.
- Pages that need scrolling or interaction before results appear may be under-reported.
