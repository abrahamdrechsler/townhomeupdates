# townhomeupdate

Weekly status report automation for the **Townhomes (Multi-Unit)** Linear initiative.

Every Friday morning (13:00 UTC ≈ 9 AM EDT / 8 AM EST), a GitHub Actions workflow:

1. Queries the Linear API for current ticket data in the `Multi-unit Support` initiative
2. Renders two charts (`progress_combined.png`, `allocation.png`) and commits them under `reports/YYYY-MM-DD/`
3. POSTs a rich Block Kit message to Slack `#beta-townhomes` via incoming webhook, with image URLs pointing back to `raw.githubusercontent.com/higharc/townhomeupdate/main/reports/...`

No laptop required. No Cowork session. Just GitHub.

## Files

- `process.py` — does everything: fetches Linear data, renders charts, commits, posts to Slack
- `requirements.txt` — Python deps (requests, matplotlib, scipy, numpy)
- `.github/workflows/weekly.yml` — cron + workflow definition
- `reports/YYYY-MM-DD/` — auto-generated chart archive (one folder per weekly run)

## Setup (one-time)

### 1. Create the repo
At `https://github.com/organizations/higharc/repositories/new`:
- Name: `townhomeupdate`
- Visibility: **public** (so `raw.githubusercontent.com` URLs are publicly fetchable — needed for Slack/Linear/Notion image rendering)
- Initialize with a README (creates the `main` branch)

### 2. Add secrets
`Settings → Secrets and variables → Actions → New repository secret`

- `LINEAR_API_KEY` — personal API key from Linear (`Settings → My account → Security & access → Personal API keys`). Looks like `lin_api_xxx…`. Needs read access to the Multi-unit Support initiative.
- `SLACK_WEBHOOK_URL` — incoming webhook URL from the `ProjectUpdatePublish` Slack app, pointing at `#beta-townhomes` (or your test channel for first runs).

### 3. Push the code
From the existing local project folder:

```bash
cd "/Users/abrahamdrechsler/Documents/Claude/Projects/Townhomes Progress Tracker/github-actions-setup"
git init
git remote add origin https://github.com/higharc/townhomeupdate.git
git checkout -b main
git add .
git commit -m "initial: weekly townhomes status automation"
git push -u origin main
```

### 4. First run (manual)
Go to `Actions` tab → "Weekly Townhomes Status" → `Run workflow` → click `Run workflow` button. This triggers an immediate run so you can verify the pipeline end-to-end without waiting until Friday.

Watch the run log. On success you'll see:
- A new commit with files under `reports/YYYY-MM-DD/`
- A Slack post in your target channel with both charts rendered inline

If anything fails, the workflow logs show the exact step + error.

### 5. Real schedule kicks in
From the next Friday onward, the workflow runs automatically. No human action needed.

## Configuration knobs

Inside `process.py` (top of file):
- `INITIATIVE_ID` — Linear initiative UUID
- `ENGINEERS`, `POINTS_PER_ENGINEER_PER_SPRINT`, `SPRINT_LENGTH_WEEKS` — velocity math
- `HISTORY_START` — first date to plot on the historical chart
- `GA_TARGET`, `MERGE_TO_DEV` — milestone vertical lines
- `OUT_OF_SCOPE_PROJECTS` — project names that should NOT count toward the initiative even if attached
- `AMCB_PROJECTS` — projects lumped together as a single "AMCB Projects" row in the allocation chart
- `REPO_OWNER`, `REPO_NAME`, `DEFAULT_BRANCH` — used to build the public chart URLs

To change the day/time, edit `.github/workflows/weekly.yml` and update the cron expression. Cron is UTC; remember to translate from your local time.

## Notes

- **GitHub Actions schedule isn't precise** — runs can be delayed up to ~15 min during high-load periods. Acceptable for a weekly status post.
- **`raw.githubusercontent.com` URLs** require the repo to be public. If you need this to be private, you'll have to host charts elsewhere (S3, Cloudinary, etc.) and the workflow needs to be modified to upload there instead.
- **The repo doubles as your weekly archive** — every report is in `reports/YYYY-MM-DD/`. Old runs are immutable.
- **Linear PF tickets** (team prefix `PF -`) are excluded from the report at fetch time — they're unscoped feature requests and distort engineering velocity.
- **Datum/level projects** that were removed from the Multi-unit initiative on 2026-05-12 are listed in `OUT_OF_SCOPE_PROJECTS` as a safety net in case Linear ever surfaces them again.
