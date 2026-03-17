#!/usr/bin/env python3
"""
Campaign Dashboard Generator
Fetches task data from ClickUp for each GrowthTrigger client,
generates a static HTML dashboard, and posts a Slack summary.
"""

import calendar
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

CLICKUP_V2 = "https://api.clickup.com/api/v2"
COT = timezone(timedelta(hours=-5))
SCRIPT_DIR = Path(__file__).parent
# Gray→Blue gradient pipeline — progresses from muted to full blue at "scheduled"
STATUS_COLORS = {
    "to do": "rgba(255,255,255,0.08)",
    "in copy": "rgba(255,255,255,0.15)",
    "in design": "rgba(0,47,255,0.18)",
    "review": "rgba(0,47,255,0.28)",
    "in design qa": "rgba(0,47,255,0.38)",
    "in implementation": "rgba(0,47,255,0.50)",
    "in final check": "rgba(0,47,255,0.62)",
    "complete": "rgba(0,47,255,0.76)",
    "scheduled": "#002fff",
}
# Pipeline order: gray → blue gradient, "scheduled" is the final destination
STATUS_ORDER = [
    "to do", "in copy", "in design", "review", "in design qa",
    "in implementation", "in final check", "complete", "scheduled",
]


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
# Month convention detection
# ---------------------------------------------------------------------------

def extract_month_number(task_name: str) -> Optional[int]:
    """Extract month number from 'C1/M14 — ...' or 'C3M7 - ...' patterns."""
    match = re.search(r'C\d+/?M(\d+)', task_name)
    return int(match.group(1)) if match else None


def split_by_month_convention(tasks):
    """Separate tasks into majority-month and out-of-month groups."""
    month_numbers = []
    for task in tasks:
        m = extract_month_number(task.get("name", ""))
        if m is not None:
            month_numbers.append(m)

    if not month_numbers:
        return tasks, [], None

    majority_month = Counter(month_numbers).most_common(1)[0][0]

    current = []
    out_of_month = []
    for task in tasks:
        m = extract_month_number(task.get("name", ""))
        if m is None or m == majority_month:
            current.append(task)
        else:
            out_of_month.append(task)

    return current, out_of_month, majority_month


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

def _html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def generate_html(client_data, now_cot):
    """Read template and fill placeholders."""
    template = (SCRIPT_DIR / "template.html").read_text(encoding="utf-8")

    month_year = now_cot.strftime("%B %Y")
    day_of_month = now_cot.day
    days_in_month = calendar.monthrange(now_cot.year, now_cot.month)[1]
    last_updated = now_cot.strftime("%b %d, %Y at %I:%M %p COT")

    # Totals (only current-month tasks)
    total_done = sum(c["done"] for c in client_data.values())
    total_wip = sum(c["in_progress"] for c in client_data.values())
    total_remaining = sum(c["remaining"] for c in client_data.values())
    total_all = sum(c["total"] for c in client_data.values())
    overall_pct = round(total_done / total_all * 100) if total_all else 0

    # Ring offset: circumference = 2*pi*90 = 565.48, offset = 565 * (1 - pct/100)
    ring_offset = round(565 * (1 - overall_pct / 100)) if overall_pct else 565

    summary_html = f"""
    <div class="s-stat"><div class="s-num" data-count="{total_done}">{total_done}</div><div class="s-label">Scheduled</div></div>
    <div class="s-stat"><div class="s-num" data-count="{total_wip}">{total_wip}</div><div class="s-label">In Progress</div></div>
    <div class="s-stat"><div class="s-num" data-count="{total_remaining}">{total_remaining}</div><div class="s-label">Remaining</div></div>
    <div class="s-stat"><div class="s-num" data-count="{total_all}">{total_all}</div><div class="s-label">Total</div></div>
    """

    # Client cards — blue/white/black only
    health_class_map = {"green": "b-on", "yellow": "b-at", "red": "b-behind"}
    health_label_map = {"green": "On Track", "yellow": "At Risk", "red": "Behind"}

    cards_html = ""
    for i, (client_name, data) in enumerate(client_data.items()):
        health = data["health"]
        done_pct = round(data["done"] / data["total"] * 100) if data["total"] else 0
        wip_pct = round(data["in_progress"] / data["total"] * 100) if data["total"] else 0

        health_class = health_class_map[health]
        health_label = health_label_map[health]

        month_label = f"M{data['majority_month']}" if data.get("majority_month") else ""
        month_tag = f'<span class="m-tag">{month_label}</span>' if month_label else ""

        oom_count = data.get("out_of_month_count", 0)
        oom_badge = f'<span class="b-oom">{oom_count} out-of-month</span>' if oom_count > 0 else ""

        delay = 0.25 + i * 0.1

        cards_html += f"""
    <div class="card anim" style="animation-delay:{delay:.2f}s">
      <div class="c-head">
        <div class="c-name">
          <span class="c-dot"></span>
          {client_name}
          {month_tag}
        </div>
        <div class="c-badges">
          {oom_badge}
          <span class="badge {health_class}">{health_label}</span>
        </div>
      </div>
      <div class="big-num"><span data-count="{data['done']}">{data['done']}</span> <span class="of">/ {data['total']}</span></div>
      <div class="prog-track">
        <div class="prog-done" style="width:{done_pct}%"></div>
        <div class="prog-wip" style="width:{wip_pct}%"></div>
      </div>
      <div class="c-stats">
        <div><strong>{data['done']}</strong> done</div>
        <div><strong>{data['in_progress']}</strong> in progress</div>
        <div><strong>{data['remaining']}</strong> remaining</div>
      </div>
    </div>"""

    # Detail table rows
    dot_map = {"green": "h-good", "yellow": "h-warn", "red": "h-bad"}
    detail_html = ""
    for client_name, data in client_data.items():
        health = data["health"]
        pct = round(data["done"] / data["total"] * 100) if data["total"] else 0
        dot_cls = dot_map[health]
        month_label = f"M{data['majority_month']}" if data.get("majority_month") else "-"

        detail_html += f"""
      <tr>
        <td><span class="c-dot" style="display:inline-block;vertical-align:middle;margin-right:8px"></span>{client_name}</td>
        <td>{month_label}</td>
        <td>{data['done']}</td>
        <td>{data['in_progress']}</td>
        <td>{data['remaining']}</td>
        <td>{data['total']}</td>
        <td class="pct">{pct}%</td>
        <td><span class="h-dot {dot_cls}"></span></td>
      </tr>"""

    # Pipeline bars — blue intensity scale
    all_statuses = set()
    for data in client_data.values():
        all_statuses.update(data["status_counts"].keys())
    sorted_statuses = [s for s in STATUS_ORDER if s in all_statuses]
    sorted_statuses += sorted(all_statuses - set(STATUS_ORDER))

    pipeline_html = ""
    for client_name, data in client_data.items():
        total = data["total"]
        if total == 0:
            continue
        segments = ""
        for status in sorted_statuses:
            count = data["status_counts"].get(status, 0)
            if count == 0:
                continue
            pct = count / total * 100
            s_color = STATUS_COLORS.get(status, "rgba(255,255,255,0.1)")
            label = str(count) if pct > 6 else ""
            segments += f'<div class="pipe-seg" style="width:{pct}%;background:{s_color}" title="{status}: {count}">{label}</div>'

        pipeline_html += f"""
    <div class="pipe-row">
      <div class="pipe-lbl"><span class="c-dot" style="display:inline-block"></span> {client_name}</div>
      <div class="pipe-bar">{segments}</div>
    </div>"""

    # Legend
    legend_html = ""
    for status in sorted_statuses:
        s_color = STATUS_COLORS.get(status, "#6b7280")
        legend_html += f'<div class="leg-item"><span class="leg-sw" style="background:{s_color}"></span>{status.title()}</div>'

    # Out-of-month section
    has_oom = any(d.get("out_of_month_count", 0) > 0 for d in client_data.values())
    oom_html = ""
    if has_oom:
        oom_items = ""
        for client_name, data in client_data.items():
            oom_tasks = data.get("out_of_month_tasks", [])
            if not oom_tasks:
                continue
            majority = data.get("majority_month", "?")
            task_list = ""
            for t in oom_tasks:
                name = _html_escape(t["name"])
                status = t.get("status", "unknown")
                task_list += f'<div class="oom-t"><span>{name}</span><span class="oom-ts">{status}</span></div>\n'

            oom_items += f"""
      <details class="oom-cl">
        <summary>
          <span class="c-dot" style="display:inline-block"></span>
          {client_name}
          <span class="oom-cnt">{len(oom_tasks)} task{"s" if len(oom_tasks) != 1 else ""}</span>
        </summary>
        <div class="oom-tasks">{task_list}</div>
      </details>"""

        oom_html = f"""
  <div class="oom-section anim" style="animation-delay:0.9s">
    <div class="sec-title">Out-of-Month Campaigns <span class="ln"></span></div>
    <div class="oom-box">
      <div class="oom-desc">These campaigns don't match the current month naming convention. They may be leftover from a previous month or mislabeled. Review needed.</div>
      {oom_items}
    </div>
  </div>"""

    # Fill template
    html = template
    html = html.replace("{{MONTH_YEAR}}", month_year)
    html = html.replace("{{DAY_OF_MONTH}}", str(day_of_month))
    html = html.replace("{{DAYS_IN_MONTH}}", str(days_in_month))
    html = html.replace("{{LAST_UPDATED}}", last_updated)
    html = html.replace("{{OVERALL_PCT}}", str(overall_pct))
    html = html.replace("{{RING_OFFSET}}", str(ring_offset))
    html = html.replace("{{SUMMARY_STATS}}", summary_html)
    html = html.replace("{{CLIENT_CARDS}}", cards_html)
    html = html.replace("{{DETAIL_ROWS}}", detail_html)
    html = html.replace("{{PIPELINE_BARS}}", pipeline_html)
    html = html.replace("{{PIPELINE_LEGEND}}", legend_html)
    html = html.replace("{{OUT_OF_MONTH_SECTION}}", oom_html)

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
        month_label = f" (M{data['majority_month']})" if data.get("majority_month") else ""
        oom = data.get("out_of_month_count", 0)
        oom_note = f" | {oom} out-of-month" if oom > 0 else ""
        lines.append(f"{emoji} *{name}*{month_label}: {data['done']}/{data['total']} scheduled, {data['in_progress']} in progress{oom_note}")

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

        # Split by naming convention
        current_tasks, oom_tasks, majority_month = split_by_month_convention(tasks)
        if majority_month is not None:
            print(f"  Majority month: M{majority_month} ({len(current_tasks)} tasks)")
            if oom_tasks:
                print(f"  Out-of-month: {len(oom_tasks)} tasks")
        else:
            print(f"  No month convention detected, counting all tasks")

        # Classify only current-month tasks
        result = classify_tasks(current_tasks, done_statuses, in_progress_statuses)
        health = compute_health(result["done"], result["total"], day_of_month, days_in_month)

        client_data[client_name] = {
            **result,
            "color": color,
            "health": health,
            "majority_month": majority_month,
            "out_of_month_count": len(oom_tasks),
            "out_of_month_tasks": [
                {
                    "name": t.get("name", "Untitled"),
                    "status": t.get("status", {}).get("status", "unknown"),
                }
                for t in oom_tasks
            ],
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
