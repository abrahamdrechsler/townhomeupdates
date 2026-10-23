#!/usr/bin/env python3
"""Weekly ticket-count report for the two Wall Planes GA projects."""
from __future__ import annotations

import argparse
import html
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from process import RAW_URL_BASE, commit_and_push, fmt_x, iso, linear_query, plt, requests

# Stable IDs keep the report working when project names change in Linear.
PROJECT_IDS = (
    "e648f625-5907-47ff-a900-d9a2f58156df",
    "930a1529-cec2-41ad-a48b-215f44d66eca",
)
CHANNEL_ID = "C0ALM094YSG"  # #project-wall-planes
HISTORY_START = datetime(2026, 2, 25, tzinfo=timezone.utc)
COLORS = ("#2876b9", "#d47b28")

PROJECT_QUERY = """
query WallPlanesProject($id: String!, $after: String) {
  project(id: $id) {
    id name url
    issues(first: 100, after: $after, includeArchived: true) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id identifier title url createdAt completedAt canceledAt
        state { type }
      }
    }
  }
}
"""


def fetch_projects(api_key: str) -> list[dict]:
    """Fetch all pages, including unestimated and archived tickets from every team."""
    projects = []
    for project_id in PROJECT_IDS:
        cursor = None
        issues = {}
        while True:
            project = linear_query(api_key, PROJECT_QUERY, {
                "id": project_id, "after": cursor,
            })["project"]
            if project is None:
                raise RuntimeError(f"Cannot access configured project {project_id}")
            page = project["issues"]
            issues.update((issue["id"], issue) for issue in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            next_cursor = page["pageInfo"]["endCursor"]
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError(f"Pagination stalled for project {project_id}")
            cursor = next_cursor
        projects.append({
            "id": project["id"], "name": project["name"], "url": project["url"],
            "issues": list(issues.values()),
        })
    return projects


def counts(issues: list[dict]) -> dict:
    """Use current workflow states, so reopened and canceled issues count correctly."""
    completed = sum(i["state"]["type"] == "completed" for i in issues)
    canceled = sum(i["state"]["type"] == "canceled" for i in issues)
    return {"open": len(issues) - completed - canceled,
            "completed": completed, "total": len(issues) - canceled}


def recent(issues: list[dict], field: str, today: datetime) -> list[dict]:
    """Tickets created/completed in the last seven days, excluding canceled work."""
    week_ago = today - timedelta(days=7)
    return sorted([
        i for i in issues
        if i["state"]["type"] != "canceled"
        and (field != "completedAt" or i["state"]["type"] == "completed")
        and (when := iso(i.get(field))) is not None
        and week_ago < when <= today
    ], key=lambda i: i[field], reverse=True)


def open_at(issues: list[dict], date: datetime) -> int:
    """Approximate historical open work from dates within today's project scope."""
    return sum(
        (created := iso(i.get("createdAt"))) is not None and created <= date
        and ((done := iso(i.get("completedAt"))) is None or done > date)
        and ((canceled := iso(i.get("canceledAt"))) is None or canceled > date)
        for i in issues
    )


def build_report(projects: list[dict], today: datetime) -> dict:
    """Keep both projects in the report, even before Enterprise has tickets."""
    rows = []
    for project in projects:
        issues = project["issues"]
        rows.append({
            "id": project["id"], "name": project["name"], "url": project["url"],
            **counts(issues),
            "created_7d": len(recent(issues, "createdAt", today)),
            "completed_7d": len(recent(issues, "completedAt", today)),
        })
    keys = ("open", "completed", "total", "created_7d", "completed_7d")
    return {"date": today.date().isoformat(), "generated_at": today.isoformat(), "projects": rows,
            "totals": {key: sum(row[key] for row in rows) for key in keys}}


def render_charts(projects: list[dict], report: dict, today: datetime, directory: Path) -> None:
    """Render separate project trends and a current open-ticket comparison."""
    dates = []
    date = min(HISTORY_START, today)
    while date < today:
        dates.append(date)
        date += timedelta(days=7)
    dates.append(today)
    fig, ax = plt.subplots(figsize=(12, 6))
    for project, row, color in zip(projects, report["projects"], COLORS):
        values = [open_at(project["issues"], date) for date in dates[:-1]] + [row["open"]]
        ax.plot(dates, values, label=project["name"], color=color, linewidth=2)
        ax.scatter([today], [row["open"]], color=color, zorder=3)
    ax.set_title("Wall Planes — Open Tickets by Project", loc="left", fontweight="bold")
    ax.set_ylabel("Open tickets")
    ax.set_ylim(bottom=0, top=max(1, ax.get_ylim()[1]))
    ax.grid(axis="y", alpha=0.2)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.20), frameon=False)
    fmt_x(ax)
    fig.text(0.02, 0.01, "History is reconstructed from issue dates in the current project scope; transfers and reopenings may change past values.", fontsize=8, color="#666666")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(directory / "progress_combined.png", dpi=140, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 4))
    rows = report["projects"]
    bars = ax.barh([row["name"] for row in rows], [row["open"] for row in rows], color=COLORS)
    ax.invert_yaxis()
    maximum = max((row["open"] for row in rows), default=0)
    ax.set_xlim(0, max(5, maximum * 1.3))
    for bar, row in zip(bars, rows):
        ax.text(bar.get_width() + max(0.1, maximum * 0.02), bar.get_y() + bar.get_height() / 2,
                f"{row['open']} open / {row['total']} total", va="center")
    ax.set_title("Wall Planes — Current Open Work", loc="left", fontweight="bold")
    ax.set_xlabel("Open tickets")
    ax.grid(axis="x", alpha=0.2)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(directory / "allocation.png", dpi=140, bbox_inches="tight")
    plt.close(fig)


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def slack_payload(projects: list[dict], report: dict, image_base: str) -> dict:
    """Build a bounded Block Kit message with a separate section for each project."""
    totals = report["totals"]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Wall Planes — Weekly Status — {report['date']}"}},
        section(f"*📊 All tracked projects*\n• Open: *{totals['open']} tickets*\n"
                f"• Completed: {totals['completed']} tickets\n• Total in scope: {totals['total']} tickets\n"
                f"• Last 7 days: {totals['created_7d']} created · {totals['completed_7d']} completed"),
    ]
    today = datetime.fromisoformat(report["generated_at"])
    for project, row in zip(projects, report["projects"]):
        blocks.append({"type": "divider"})
        blocks.append(section(
            f"*<{row['url']}|{html.escape(row['name'], quote=False)}>*\n"
            f"• *{row['open']} open* · {row['completed']} completed · {row['total']} total\n"
            f"• Last 7 days: *{row['created_7d']} created* · *{row['completed_7d']} completed*"
            + ("\n_No tickets in scope yet._" if row["total"] == 0 else "")
        ))
        for field, heading in (("createdAt", "New tickets this week"), ("completedAt", "Completed this week")):
            issues = recent(project["issues"], field, today)
            if issues:
                lines = [f"*{heading}*"]
                shown = 0
                for issue in issues[:5]:
                    # Slack only decodes &, < and > entities; leave quotes literal.
                    title = html.escape(issue["title"][:200], quote=False)
                    line = f"• <{issue['url']}|{issue['identifier']}> — {title}"
                    if len("\n".join(lines + [line])) > 2800:
                        break
                    lines.append(line)
                    shown += 1
                if len(issues) > shown:
                    lines.append(f"_…and {len(issues) - shown} more_")
                blocks.append(section("\n".join(lines)))
    for filename, title in (("progress_combined.png", "Open tickets over time by project"), ("allocation.png", "Current open work by project")):
        blocks.append({"type": "image", "title": {"type": "plain_text", "text": title},
                       "image_url": f"{image_base}/{filename}", "alt_text": title})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
        "Ticket counts include all teams and archived issues; canceled tickets are excluded. "
        "Created means issue creation, not transfer into a project. Historical curves use current project scope. "
        "Story points and completion forecasts are omitted because estimates are inconsistent."}]})
    return {"text": f"Wall Planes weekly status — {report['date']}: {totals['open']} open tickets", "blocks": blocks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Commit charts and post to the Wall Planes webhook")
    parser.add_argument("--input", type=Path, help="Use a local JSON fixture instead of querying Linear (preview only)")
    parser.add_argument("--output", type=Path, help="Preview output directory")
    args = parser.parse_args()
    if args.publish and (args.input or args.output):
        parser.error("--input and --output are preview-only")
    webhook = os.environ.get("WALL_PLANES_SLACK_WEBHOOK_URL")
    if args.publish and not webhook:
        parser.error("WALL_PLANES_SLACK_WEBHOOK_URL is required for publishing")
    if not args.input and not os.environ.get("LINEAR_API_KEY"):
        parser.error("LINEAR_API_KEY is required")
    today = datetime.now(timezone.utc)
    projects = json.loads(args.input.read_text()) if args.input else fetch_projects(os.environ["LINEAR_API_KEY"])
    report = build_report(projects, today)
    directory = args.output or Path("reports") / "wall-planes" / report["date"]
    directory.mkdir(parents=True, exist_ok=True)
    render_charts(projects, report, today, directory)
    (directory / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    payload = slack_payload(projects, report, f"{RAW_URL_BASE}/reports/wall-planes/{report['date']}")
    if args.publish:
        commit_and_push(str(directory), f"wall planes {report['date']}")
        try:
            response = requests.post(webhook, json=payload, timeout=30)
        except requests.RequestException:
            raise RuntimeError("Wall Planes Slack delivery failed; check connectivity before retrying") from None
        # Do not print a requests exception containing the secret webhook URL.
        if response.status_code != 200 or response.text.strip() != "ok":
            raise RuntimeError(f"Slack rejected Wall Planes report: HTTP {response.status_code}")
        print(f"Published Wall Planes report to {CHANNEL_ID}")
    else:
        print("Preview only: no commit, push, or Slack post.")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
