#!/usr/bin/env python3
"""
Townhomes (Multi-Unit) weekly progress report.

Runs every Friday in GitHub Actions on `higharc/townhomeupdate`. Queries Linear
for the current state of the Multi-unit Support initiative, renders progress
and allocation charts, commits them to this repo under reports/{date}/, and
posts a rich Slack message via incoming webhook with image URLs pointing back
at raw.githubusercontent.com.

Env vars (provided by the workflow via GitHub secrets):
  LINEAR_API_KEY      — Linear personal access token (read scope)
  SLACK_WEBHOOK_URL   — Incoming webhook URL for #beta-townhomes

Repo-internal config (edit at top of file when needed):
  INITIATIVE_ID                          — Linear initiative UUID
  ENGINEERS / POINTS_PER_ENGINEER_PER_SPRINT / SPRINT_LENGTH_WEEKS
  HISTORY_START                          — first date to plot
  GA_TARGET / MERGE_TO_DEV               — milestone markers
  OUT_OF_SCOPE_PROJECTS                  — project names to drop from scope
  REPO_OWNER / REPO_NAME / DEFAULT_BRANCH — used to build raw URLs
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from scipy.interpolate import PchipInterpolator  # noqa: E402

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
INITIATIVE_ID = "38249c25-a4f8-4306-97a1-7b21361ec609"
ENGINEERS = ["Jesse", "Paul", "Joss", "AMC Bridge engineer 1", "AMC Bridge engineer 2"]
POINTS_PER_ENGINEER_PER_SPRINT = 7
SPRINT_LENGTH_WEEKS = 2
WEEKLY_THROUGHPUT = (
    len(ENGINEERS) * POINTS_PER_ENGINEER_PER_SPRINT / SPRINT_LENGTH_WEEKS
)  # 17.5 pts/week

HISTORY_START = datetime(2025, 10, 6, tzinfo=timezone.utc)
GA_TARGET = datetime(2026, 7, 31, tzinfo=timezone.utc)
MERGE_TO_DEV = datetime(2026, 7, 17, tzinfo=timezone.utc)

OUT_OF_SCOPE_PROJECTS = {
    # Datum/level projects removed from Multi-unit initiative 2026-05-12.
    # Kept here in case the same names ever come back as members.
    "Level and Datum Follow-up for Townhomes Launch",
    "Origin Relative Datums - Followup",
    "Datums - Level Improvements",
    "Origin Relative Datums",
    "Move Facade, Foundation, and Level Datums to the Explorer Menu",
}

AMCB_PROJECTS = {
    "AMCB Townhomes Showroom+Config",
    "AMCB Townhomes UI/UX",
    "AMCB Townhomes LP",
}

REPO_OWNER = "abrahamdrechsler"
REPO_NAME = "townhomeupdates"
DEFAULT_BRANCH = "main"
RAW_URL_BASE = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{DEFAULT_BRANCH}"
)

INITIATIVE_URL = (
    "https://linear.app/higharc/initiative/multi-unit-support-5090ac821ad8"
)

# ----------------------------------------------------------------------------
# Linear GraphQL
# ----------------------------------------------------------------------------
LINEAR_GQL = "https://api.linear.app/graphql"


def linear_query(api_key: str, query: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        LINEAR_GQL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
    return data["data"]


ISSUES_QUERY = """
query InitiativeIssues($id: String!, $after: String) {
  initiative(id: $id) {
    id
    name
    targetDate
    projects(first: 50) {
      nodes {
        id
        name
        team { name }
      }
    }
  }
}
"""

PROJECT_ISSUES_QUERY = """
query ProjectIssues($projectId: String!, $after: String) {
  project(id: $projectId) {
    issues(first: 100, after: $after, includeArchived: false) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        identifier
        title
        url
        estimate
        createdAt
        completedAt
        canceledAt
        state { type name }
        team { name }
      }
    }
  }
}
"""


def fetch_initiative_issues(api_key: str, initiative_id: str) -> tuple[dict, list[dict]]:
    """Returns (initiative_meta, [issues...]). Iterates over projects."""
    data = linear_query(api_key, ISSUES_QUERY, {"id": initiative_id})
    init = data["initiative"]
    projects = init["projects"]["nodes"]
    issues: list[dict] = []
    for proj in projects:
        if proj["name"] in OUT_OF_SCOPE_PROJECTS:
            continue
        cursor = None
        while True:
            page = linear_query(
                api_key,
                PROJECT_ISSUES_QUERY,
                {"projectId": proj["id"], "after": cursor},
            )["project"]["issues"]
            for n in page["nodes"]:
                n["project"] = proj["name"]
                # PF exclusion: drop any issues from PF teams
                team_name = (n.get("team") or {}).get("name") or ""
                if team_name.startswith("PF -"):
                    continue
                issues.append(n)
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return init, issues


# ----------------------------------------------------------------------------
# Snapshot reconstruction
# ----------------------------------------------------------------------------
def iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def state_at(issue: dict, dt: datetime) -> str:
    """One of: unborn, open, completed, canceled."""
    created = iso(issue.get("createdAt"))
    completed = iso(issue.get("completedAt"))
    canceled = iso(issue.get("canceledAt"))
    if created is None or created > dt:
        return "unborn"
    if canceled and canceled <= dt:
        return "canceled"
    if completed and completed <= dt:
        return "completed"
    return "open"


def snapshot_at(issues: list[dict], dt: datetime) -> dict:
    oi = op = ci = cp = ti = tp = 0
    for it in issues:
        s = state_at(it, dt)
        est = it.get("estimate") or 0
        if s == "open":
            oi += 1
            op += est
            ti += 1
            tp += est
        elif s == "completed":
            ci += 1
            cp += est
            ti += 1
            tp += est
    return {
        "total_issues": ti,
        "open_issues": oi,
        "completed_issues": ci,
        "total_points": tp,
        "open_points": op,
        "completed_points": cp,
    }


def weekly_series(issues: list[dict], today: datetime) -> list[tuple[datetime, dict]]:
    series = []
    cur = HISTORY_START
    while cur <= today:
        series.append((cur, snapshot_at(issues, cur)))
        cur += timedelta(days=7)
    if series[-1][0] != today:
        series.append((today, snapshot_at(issues, today)))
    return series


# ----------------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------------
def fmt_x(ax) -> None:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d/%y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")


def smooth(xs_dt: list[datetime], ys: list[float], samples_per_segment: int = 8):
    xs_num = np.array([mdates.date2num(d) for d in xs_dt])
    ys_arr = np.array(ys, dtype=float)
    interp = PchipInterpolator(xs_num, ys_arr)
    xs_fine = np.linspace(
        xs_num[0], xs_num[-1], (len(xs_num) - 1) * samples_per_segment + 1
    )
    return xs_fine, interp(xs_fine)


def render_progress_chart(
    out_path: str,
    series: list[tuple[datetime, dict]],
    today: datetime,
    open_points: float,
    projected_str: str,
) -> None:
    dates = [d for d, _ in series]
    open_iss = [s["open_issues"] for _, s in series]
    open_pts = [s["open_points"] for _, s in series]

    # Linear projection burndown
    proj_dates = [today]
    proj_pts = [open_points]
    n_weeks = int(math.ceil(open_points / WEEKLY_THROUGHPUT))
    for w in range(1, n_weeks + 1):
        proj_dates.append(today + timedelta(weeks=w))
        proj_pts.append(max(0, open_points - w * WEEKLY_THROUGHPUT))
    if proj_pts[-1] > 0:
        proj_dates.append(today + timedelta(weeks=open_points / WEEKLY_THROUGHPUT))
        proj_pts.append(0)

    fig, ax1 = plt.subplots(figsize=(11, 5.5))
    hist_x, hist_iss = smooth(dates, open_iss)
    hist_x_pts, hist_pts_y = smooth(dates, open_pts)

    l1, = ax1.plot_date(
        hist_x, hist_iss, "-", color="#1f77b4", linewidth=2.0,
        label="Open tickets (count)",
    )
    ax1.fill_between(hist_x, hist_iss, 0, color="#1f77b4", alpha=0.10)
    ax1.set_ylabel("Open tickets (count)", color="#1f77b4")
    ax1.tick_params(axis="y", labelcolor="#1f77b4")
    ax1.set_ylim(bottom=0)

    ax2 = ax1.twinx()
    l2, = ax2.plot_date(
        hist_x_pts, hist_pts_y, "-", color="#ff7f0e", linewidth=2.0,
        label="Open points (story-point sum)",
    )
    ax2.fill_between(hist_x_pts, hist_pts_y, 0, color="#ff7f0e", alpha=0.08)
    l3, = ax2.plot(
        proj_dates, proj_pts, "--", color="#2ca02c", linewidth=1.8,
        label=f"Projected burndown @ {WEEKLY_THROUGHPUT} pts/week → {projected_str}",
    )
    ax2.set_ylabel("Open points (story-point sum)", color="#ff7f0e")
    ax2.tick_params(axis="y", labelcolor="#ff7f0e")
    ax2.set_ylim(bottom=0)
    ax1.set_title("Townhomes (Multi-Unit) — Open Work Over Time + Projection")

    merge_line = ax1.axvline(
        MERGE_TO_DEV, color="#b07aa1", linestyle="--", linewidth=1.5,
        label=f"Merge to Dev Milestone ({MERGE_TO_DEV.strftime('%Y-%m-%d')})",
        zorder=1,
    )
    ga_line = ax1.axvline(
        GA_TARGET, color="#999999", linestyle=":", linewidth=1.5,
        label=f"GA Target ({GA_TARGET.strftime('%Y-%m-%d')})",
        zorder=1,
    )
    y_top = max(open_iss + [1]) * 1.05
    ax1.text(
        MERGE_TO_DEV - timedelta(days=4), y_top * 0.65, "Merge to Dev",
        color="#7a4d70", fontsize=9, va="center", ha="right", rotation=90,
    )
    ax1.text(
        GA_TARGET - timedelta(days=4), y_top * 0.65, "GA Target",
        color="#666666", fontsize=9, va="center", ha="right", rotation=90,
    )

    xmin = dates[0]
    xmax = GA_TARGET + timedelta(days=20)
    ax1.set_xlim(xmin, xmax)
    ax2.set_xlim(xmin, xmax)
    ax1.legend(
        handles=[l1, l2, l3, merge_line, ga_line], loc="upper center",
        bbox_to_anchor=(0.5, -0.18), ncol=3, frameon=False, fontsize=9,
    )
    fmt_x(ax1)
    fig.tight_layout()
    plt.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close()


def render_allocation_chart(out_path: str, per_project: dict) -> None:
    proj_sorted = sorted(
        per_project.items(), key=lambda kv: kv[1]["open_points"], reverse=True
    )
    proj_sorted = [
        (k, v) for k, v in proj_sorted if v["open_points"] > 0 or v["open_issues"] > 0
    ]
    labels = [k for k, _ in proj_sorted]
    points = [v["open_points"] for _, v in proj_sorted]
    issues_ct = [v["open_issues"] for _, v in proj_sorted]
    team_colors = {
        "Building Generation": "#1f77b4",
        "Config": "#ff7f0e",
        "Studio": "#2ca02c",
        "AMC Bridge": "#d62728",
        "Showroom": "#9467bd",
        "Product Design": "#8c564b",
        "Quality Assurance": "#e377c2",
    }
    colors = [team_colors.get(v["team"], "#7f7f7f") for _, v in proj_sorted]

    fig, ax = plt.subplots(figsize=(11, 6))
    bars = ax.barh(labels, points, color=colors)
    ax.invert_yaxis()
    for bar, ic, pt in zip(bars, issues_ct, points):
        ax.text(
            bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
            f"{pt} pts / {ic} tickets", va="center", fontsize=9,
        )
    ax.set_xlabel("Open points")
    ax.set_title("Townhomes — Open Work by Project")
    fig.tight_layout()
    plt.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close()


# ----------------------------------------------------------------------------
# Slack
# ----------------------------------------------------------------------------
def post_slack(webhook_url: str, report_date: str, snap: dict, weeks_remaining: float,
               projected_str: str, proj_sorted: list, added_recent: list,
               progress_url: str, allocation_url: str) -> None:
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"Townhomes (Multi-Unit) — Weekly Status — {report_date}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*📊 Status*\n"
            f"• Open: *{snap['open_issues']} tickets* ({snap['open_points']} pts)\n"
            f"• Completed: {snap['completed_issues']} tickets ({snap['completed_points']} pts)\n"
            f"• Total in scope: {snap['total_issues']} tickets ({snap['total_points']} pts)"
        )}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*📈 Last 7 days*\n"
            f"• Tickets added: {snap['issues_added_7d']}\n"
            f"• Tickets completed: {snap['issues_completed_7d']} ({snap['points_completed_7d']} pts)"
        )}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*🎯 Projection*  ({len(ENGINEERS)} eng × {POINTS_PER_ENGINEER_PER_SPRINT} "
            f"pts/sprint ÷ {SPRINT_LENGTH_WEEKS}-wk = {WEEKLY_THROUGHPUT} pts/week)\n"
            f"• Projected completion: *{projected_str}*\n"
            f"• GA target: *{GA_TARGET.strftime('%Y-%m-%d')}*\n"
            f"• Merge to Dev milestone: *{MERGE_TO_DEV.strftime('%Y-%m-%d')}*"
        )}},
        {"type": "image", "title": {"type": "plain_text", "text": "Progress over time + projection"},
         "image_url": progress_url, "alt_text": "Open work over time with projection"},
        {"type": "image", "title": {"type": "plain_text", "text": "Allocation by project"},
         "image_url": allocation_url, "alt_text": "Open work by project"},
    ]
    top_lines = ["*📋 Top 5 by open points*"]
    for k, v in proj_sorted[:5]:
        top_lines.append(f"• {k} ({v['team']}) — {v['open_points']} pts ({v['open_issues']} tickets)")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(top_lines)}})
    if added_recent:
        added_lines = ["*🆕 Tickets added this week*"]
        for i in added_recent[:10]:
            added_lines.append(f"• <{i['url']}|{i['identifier']}> — {i['title']}")
        if len(added_recent) > 10:
            added_lines.append(f"_…and {len(added_recent) - 10} more_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(added_lines)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"Source: <{INITIATIVE_URL}|Multi-unit Support initiative> · Auto-posted by ProjectUpdatePublish"}]})

    payload = {"text": f"Townhomes weekly status — {report_date}", "blocks": blocks}
    resp = requests.post(webhook_url, json=payload, timeout=30)
    print(f"Slack POST → HTTP {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()


# ----------------------------------------------------------------------------
# Git
# ----------------------------------------------------------------------------
def commit_and_push(report_dir: str, report_date: str) -> None:
    """Commit the report files and push to origin."""
    actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    email = os.environ.get("GITHUB_ACTOR_EMAIL", f"{actor}@users.noreply.github.com")
    subprocess.run(["git", "config", "user.name", actor], check=True)
    subprocess.run(["git", "config", "user.email", email], check=True)
    subprocess.run(["git", "add", report_dir], check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode
    if diff == 0:
        print("No changes to commit.")
        return
    subprocess.run(
        ["git", "commit", "-m", f"weekly report — {report_date}"], check=True
    )
    subprocess.run(["git", "push", "origin", "HEAD"], check=True)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    linear_key = os.environ.get("LINEAR_API_KEY")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not linear_key or not slack_url:
        print("ERROR: LINEAR_API_KEY and SLACK_WEBHOOK_URL env vars are required.")
        return 1

    today = datetime.now(timezone.utc)
    today_str = today.strftime("%Y-%m-%d")
    print(f"Running for {today_str}...")

    init, issues = fetch_initiative_issues(linear_key, INITIATIVE_ID)
    print(f"Pulled {len(issues)} issues across {len(init['projects']['nodes'])} projects.")

    # Today's snapshot
    snap = snapshot_at(issues, today)
    week_ago = today - timedelta(days=7)
    snap["issues_added_7d"] = sum(
        1 for i in issues
        if iso(i.get("createdAt")) and week_ago < iso(i["createdAt"]) <= today
        and not (iso(i.get("canceledAt")) and iso(i["canceledAt"]) <= today)
    )
    completed_7d = [
        i for i in issues
        if iso(i.get("completedAt")) and week_ago < iso(i["completedAt"]) <= today
    ]
    snap["issues_completed_7d"] = len(completed_7d)
    snap["points_completed_7d"] = sum((i.get("estimate") or 0) for i in completed_7d)

    weeks_remaining = snap["open_points"] / WEEKLY_THROUGHPUT if WEEKLY_THROUGHPUT else 0
    proj_dt = today + timedelta(days=weeks_remaining * 7)
    days_to_fri = (4 - proj_dt.weekday()) % 7
    proj_dt = proj_dt + timedelta(days=days_to_fri)
    projected_str = proj_dt.strftime("%Y-%m-%d")

    # Per-project allocation
    per_project: dict = {}
    non_canceled = [i for i in issues if (i["state"]["type"] != "canceled")]
    for i in non_canceled:
        name = i["project"]
        if name in AMCB_PROJECTS:
            proj_label = "AMCB Projects"
            team = "AMC Bridge"
        else:
            proj_label = name
            team = (i.get("team") or {}).get("name") or "—"
        pp = per_project.setdefault(proj_label, {
            "open_issues": 0, "open_points": 0,
            "total_issues": 0, "total_points": 0,
            "completed_issues": 0, "team": team, "members": set(),
        })
        pp["members"].add(name)
        pp["total_issues"] += 1
        pp["total_points"] += i.get("estimate") or 0
        if i["state"]["type"] in {"started", "unstarted", "backlog"}:
            pp["open_issues"] += 1
            pp["open_points"] += i.get("estimate") or 0
        elif i["state"]["type"] == "completed":
            pp["completed_issues"] += 1
    proj_sorted = sorted(per_project.items(), key=lambda kv: kv[1]["open_points"], reverse=True)

    # Recent additions list
    added_recent = sorted(
        [i for i in non_canceled if iso(i.get("createdAt")) and iso(i["createdAt"]) >= week_ago],
        key=lambda i: iso(i["createdAt"]), reverse=True,
    )

    # Render charts
    report_dir = os.path.join("reports", today_str)
    os.makedirs(report_dir, exist_ok=True)
    progress_path = os.path.join(report_dir, "progress_combined.png")
    allocation_path = os.path.join(report_dir, "allocation.png")
    series = weekly_series(issues, today)
    render_progress_chart(progress_path, series, today, snap["open_points"], projected_str)
    render_allocation_chart(allocation_path, per_project)
    print(f"Wrote {progress_path} and {allocation_path}")

    # Commit + push (so raw.githubusercontent.com URLs serve the new files)
    commit_and_push(report_dir, today_str)

    # URLs that Slack/Linear/Notion can fetch
    progress_url = f"{RAW_URL_BASE}/{progress_path}"
    allocation_url = f"{RAW_URL_BASE}/{allocation_path}"

    # Post to Slack
    post_slack(slack_url, today_str, snap, weeks_remaining, projected_str,
               proj_sorted, added_recent, progress_url, allocation_url)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
