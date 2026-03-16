#!/usr/bin/env python3
"""
Campaign Dashboard Generator
Fetches task data from ClickUp for each GrowthTrigger client,
generates a static HTML dashboard, and posts a Slack summary.
"""

import calendar
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

CLICKUP_V2 = "https://api.clickup.com/api/v2"
COT = timezone(timedelta(hours=-5))
SCRIPT_DIR = Path(__file__).parent
STATUS_COLORS = {
    "to do": "#6b7280",
    "in copy": "#8b5cf6",
    "in design": "#a855f7",
    "in design qa": "#c084fc",
    "in implementation": "#3b82f6",
    "in final check": "#06b6d4",
    "review": "#f59e0b",
    "scheduled": "#22c55e",
    "complete": "#10b981",
}


# ---------------------------------------------------------------------------
# ClickUp API
# ---------------------------------------------------------------------------

def _request_with_retry(method, url, headers, max_retries=3, **kwargs):
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        except requests.RequestException as e:
            print(f"  Request failed (attempt {attempt+1}/{max_retries}): {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        if resp.status_code == 429:
            wait = 2 ** attempt
            print(f"  Rate limited. Waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        return resp
    return None


def fetch_tasks_for_month(list_id, headers, year, month):
    """Fetch all tasks with due dates in the given month."""
    start = datetime(year, month, 1, tzinfo=COT)
    _, last_day = calendar.monthrange(year, month)
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=COT)

    due_date_gt = int(start.timestamp() * 1000)
    due_date_lt = int(end.timestamp() * 1000)

    tasks = []
    page = 0
    while True:
        resp = _request_with_retry(
            "GET",
            f"{CLICKUP_V2}/list/{list_id}/task",
            headers=headers,
            params={
                "page": page,
                "subtasks": "false",
                "include_closed": "true",
                "due_date_gt": due_date_gt,
                "due_date_lt": due_date_lt,
            },
        )
        if resp is None or resp.status_code != 200:
            if resp:
                print(f"  API error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            break

        batch = resp.json().get("tasks", [])
        tasks.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return tasks


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

def classify_tasks(tasks, done_statuses, in_progress_statuses):
    """Classify tasks into done, in_progress, remaining. Return counts and status breakdown."""
    done = 0
    in_progress = 0
    remaining = 0
    status_counts = Counter()

    for task in tasks:
        status = task.get("status", {}).get("status", "").lower()
        status_counts[status] += 1

        if status in done_statuses:
            done += 1
        elif status in in_progress_statuses:
            in_progress += 1
        else:
            remaining += 1

    return {
        "done": done,
        "in_progress": in_progress,
        "remaining": remaining,
        "total": len(tasks),
        "status_counts": dict(status_counts),
    }


def compute_health(done, total, day_of_month, days_in_month):
    """Green/yellow/red based on pace."""
    if total == 0:
        return "green"
    expected_pace = (day_of_month / days_in_month) * total
    if expected_pace == 0:
        return "green"
    ratio = done / expected_pace
    if ratio >= 0.7:
        return "green"
    elif ratio >= 0.4:
        return "yellow"
    return "red"


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(client_data, now_cot):
    """Read template and fill placeholders."""
    template = (SCRIPT_DIR / "template.html").read_text(encoding="utf-8")

    month_year = now_cot.strftime("%B %Y")
    day_of_month = now_cot.day
    days_in_month = calendar.monthrange(now_cot.year, now_cot.month)[1]
    last_updated = now_cot.strftime("%b %d, %Y at %I:%M %p COT")

    # Totals
    total_done = sum(c["done"] for c in client_data.values())
    total_wip = sum(c["in_progress"] for c in client_data.values())
    total_remaining = sum(c["remaining"] for c in client_data.values())
    total_all = sum(c["total"] for c in client_data.values())
    overall_pct = round(total_done / total_all * 100) if total_all else 0

    summary_html = f"""
    <div class="summary-stat"><div class="num">{total_done}</div><div class="label">Scheduled</div></div>
    <div class="summary-stat"><div class="num">{total_wip}</div><div class="label">In Progress</div></div>
    <div class="summary-stat"><div class="num">{total_remaining}</div><div class="label">Remaining</div></div>
    <div class="summary-stat"><div class="num">{total_all}</div><div class="label">Total</div></div>
    <div class="summary-stat"><div class="num">{overall_pct}%</div><div class="label">Complete</div></div>
    """

    # Client cards
    cards_html = ""
    for client_name, data in client_data.items():
        color = data["color"]
        health = data["health"]
        done_pct = round(data["done"] / data["total"] * 100) if data["total"] else 0
        wip_pct = round(data["in_progress"] / data["total"] * 100) if data["total"] else 0

        health_class = f"health-{health}"
        health_label = {"green": "On Track", "yellow": "At Risk", "red": "Behind"}[health]

        cards_html += f"""
    <div class="card">
      <div class="card-header">
        <div class="client-name">
          <span class="color-dot" style="background:{color}"></span>
          {client_name}
        </div>
        <span class="health-badge {health_class}">{health_label}</span>
      </div>
      <div class="big-number">{data['done']} <span>/ {data['total']}</span></div>
      <div class="progress-bar">
        <div class="progress-done" style="width:{done_pct}%;background:{color}"></div>
        <div class="progress-wip" style="width:{wip_pct}%;background:{color}"></div>
      </div>
      <div class="card-stats">
        <div><strong>{data['done']}</strong> done</div>
        <div><strong>{data['in_progress']}</strong> in progress</div>
        <div><strong>{data['remaining']}</strong> remaining</div>
      </div>
    </div>"""

    # Detail table rows
    detail_html = ""
    for client_name, data in client_data.items():
        color = data["color"]
        health = data["health"]
        pct = round(data["done"] / data["total"] * 100) if data["total"] else 0
        dot_color = {"green": "var(--green)", "yellow": "var(--yellow)", "red": "var(--red)"}[health]

        detail_html += f"""
      <tr>
        <td><span class="color-dot" style="background:{color}"></span> {client_name}</td>
        <td>{data['done']}</td>
        <td>{data['in_progress']}</td>
        <td>{data['remaining']}</td>
        <td>{data['total']}</td>
        <td class="pct-cell">{pct}%</td>
        <td><span class="health-dot" style="background:{dot_color}"></span></td>
      </tr>"""

    # Pipeline bars — collect all unique statuses
    all_statuses = set()
    for data in client_data.values():
        all_statuses.update(data["status_counts"].keys())
    # Sort statuses in a logical pipeline order
    status_order = [
        "to do", "in copy", "in design", "review", "in design qa",
        "in implementation", "in final check", "scheduled", "complete",
    ]
    sorted_statuses = [s for s in status_order if s in all_statuses]
    sorted_statuses += sorted(all_statuses - set(status_order))

    pipeline_html = ""
    for client_name, data in client_data.items():
        total = data["total"]
        if total == 0:
            continue
        color = data["color"]
        segments = ""
        for status in sorted_statuses:
            count = data["status_counts"].get(status, 0)
            if count == 0:
                continue
            pct = count / total * 100
            s_color = STATUS_COLORS.get(status, "#6b7280")
            label = str(count) if pct > 6 else ""
            segments += f'<div class="pipeline-segment" style="width:{pct}%;background:{s_color}" title="{status}: {count}">{label}</div>'

        pipeline_html += f"""
    <div class="pipeline-row">
      <div class="pipeline-label"><span class="color-dot" style="background:{color}"></span> {client_name}</div>
      <div class="pipeline-bar">{segments}</div>
    </div>"""

    # Legend
    legend_html = ""
    for status in sorted_statuses:
        s_color = STATUS_COLORS.get(status, "#6b7280")
        legend_html += f'<div class="legend-item"><span class="legend-swatch" style="background:{s_color}"></span>{status.title()}</div>'

    # Fill template
    html = template
    html = html.replace("{{MONTH_YEAR}}", month_year)
    html = html.replace("{{DAY_OF_MONTH}}", str(day_of_month))
    html = html.replace("{{DAYS_IN_MONTH}}", str(days_in_month))
    html = html.replace("{{LAST_UPDATED}}", last_updated)
    html = html.replace("{{SUMMARY_STATS}}", summary_html)
    html = html.replace("{{CLIENT_CARDS}}", cards_html)
    html = html.replace("{{DETAIL_ROWS}}", detail_html)
    html = html.replace("{{PIPELINE_BARS}}", pipeline_html)
    html = html.replace("{{PIPELINE_LEGEND}}", legend_html)

    return html


# ---------------------------------------------------------------------------
# Slack notification
# ---------------------------------------------------------------------------

def post_slack_summary(client_data, now_cot, dashboard_url):
    """Post a summary to Slack with per-client one-liners."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("[SLACK] No SLACK_BOT_TOKEN set, skipping notification")
        return

    # Load config for channel
    config = json.loads((SCRIPT_DIR / "config.json").read_text())
    channel = config.get("slack_channel")
    if not channel:
        print("[SLACK] No slack_channel configured, skipping notification")
        return

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=token)
    month_year = now_cot.strftime("%B %Y")

    total_done = sum(c["done"] for c in client_data.values())
    total_all = sum(c["total"] for c in client_data.values())

    lines = [f"*Campaign Dashboard — {month_year}*"]
    lines.append(f"Overall: *{total_done}/{total_all}* campaigns scheduled\n")

    health_emoji = {"green": ":large_green_circle:", "yellow": ":large_yellow_circle:", "red": ":red_circle:"}
    for name, data in client_data.items():
        emoji = health_emoji[data["health"]]
        lines.append(f"{emoji} *{name}*: {data['done']}/{data['total']} scheduled, {data['in_progress']} in progress")

    lines.append(f"\n<{dashboard_url}|View full dashboard>")

    try:
        client.chat_postMessage(channel=channel, text="\n".join(lines))
        print(f"[SLACK] Posted summary to {channel}")
    except SlackApiError as e:
        print(f"[SLACK] Error: {e.response['error']}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api_token = os.environ.get("CLICKUP_API_TOKEN")
    if not api_token:
        print("[ERROR] CLICKUP_API_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    config = json.loads((SCRIPT_DIR / "config.json").read_text())
    headers = {"Authorization": api_token}

    now_cot = datetime.now(COT)
    year = now_cot.year
    month = now_cot.month
    day_of_month = now_cot.day
    days_in_month = calendar.monthrange(year, month)[1]

    done_statuses = [s.lower() for s in config.get("done_statuses", ["scheduled"])]
    in_progress_statuses = [s.lower() for s in config.get("in_progress_statuses", [])]

    print(f"[START] Campaign Dashboard — {now_cot.strftime('%B %Y')}")
    print(f"[TIME] {now_cot.strftime('%Y-%m-%d %H:%M:%S')} COT")

    client_data = {}

    for client_name, client_config in config["clients"].items():
        list_id = client_config["list_id"]
        color = client_config["color"]

        print(f"\n[FETCH] {client_name} (list {list_id})...")
        tasks = fetch_tasks_for_month(list_id, headers, year, month)
        print(f"  Found {len(tasks)} tasks with due dates in {now_cot.strftime('%B')}")

        result = classify_tasks(tasks, done_statuses, in_progress_statuses)
        health = compute_health(result["done"], result["total"], day_of_month, days_in_month)

        client_data[client_name] = {
            **result,
            "color": color,
            "health": health,
        }

        print(f"  Done: {result['done']} | In Progress: {result['in_progress']} | Remaining: {result['remaining']} | Health: {health}")

    # Generate HTML
    html = generate_html(client_data, now_cot)
    output_path = SCRIPT_DIR / "docs" / "index.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"\n[HTML] Written to {output_path}")

    # Post Slack notification
    dashboard_url = "https://milton-collab.github.io/campaign-dashboard/"
    post_slack_summary(client_data, now_cot, dashboard_url)

    print("[DONE] Dashboard updated.")


if __name__ == "__main__":
    main()
