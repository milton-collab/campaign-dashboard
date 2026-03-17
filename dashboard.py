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
STATUS_ORDER = [
    "to do", "in copy", "in design", "review", "in design qa",
    "in implementation", "in final check", "complete", "scheduled",
]
SAFE_STATUSES = {"in final check", "complete", "scheduled"}
FIRE_ALERT_DAYS = 5


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
    match = re.search(r'C\d+/?M(\d+)', task_name)
    return int(match.group(1)) if match else None


def split_by_month_convention(tasks):
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
# Task processing
# ---------------------------------------------------------------------------

def process_tasks(raw_tasks, now_cot):
    """Extract per-task data from raw ClickUp task objects."""
    processed = []
    for t in raw_tasks:
        assignees = t.get("assignees") or []
        assignee = assignees[0].get("username", "Unassigned") if assignees else "Unassigned"
        due_ms = t.get("due_date")
        due_dt = datetime.fromtimestamp(int(due_ms) / 1000, tz=COT) if due_ms else None
        days_until = (due_dt.date() - now_cot.date()).days if due_dt else None

        processed.append({
            "name": t.get("name", "Untitled"),
            "status": t.get("status", {}).get("status", "unknown").lower(),
            "assignee": assignee,
            "due_date": due_dt,
            "days_until_due": days_until,
        })
    # Sort: overdue first, then by due date ascending, no-date last
    processed.sort(key=lambda x: (x["days_until_due"] is None, x["days_until_due"] or 999))
    return processed


def classify_tasks(tasks, done_statuses, in_progress_statuses):
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
# New features: fire alerts, AM scoreboard, projected completion
# ---------------------------------------------------------------------------

def build_fire_alerts(client_data):
    """Find campaigns due within FIRE_ALERT_DAYS that aren't near-done."""
    alerts = []
    for client_name, data in client_data.items():
        for task in data.get("tasks", []):
            days = task.get("days_until_due")
            if days is not None and days <= FIRE_ALERT_DAYS and task["status"] not in SAFE_STATUSES:
                alerts.append({
                    "client": client_name,
                    "name": task["name"],
                    "status": task["status"],
                    "assignee": task["assignee"],
                    "days": days,
                })
    alerts.sort(key=lambda a: a["days"])
    return alerts


def build_am_scoreboard(client_data, done_statuses):
    """Aggregate campaigns by assignee across all clients."""
    am = {}
    for data in client_data.values():
        for task in data.get("tasks", []):
            name = task["assignee"]
            if name not in am:
                am[name] = {"total": 0, "done": 0}
            am[name]["total"] += 1
            if task["status"] in done_statuses:
                am[name]["done"] += 1

    board = []
    for name, c in am.items():
        pct = round(c["done"] / c["total"] * 100) if c["total"] else 0
        board.append({"name": name, "total": c["total"], "done": c["done"], "pct": pct})
    board.sort(key=lambda x: x["pct"], reverse=True)
    return board


def compute_projected_completion(done, total, now_cot, days_in_month):
    """Velocity-based projected finish date."""
    day = now_cot.day
    if done == 0 or day == 0 or total == 0:
        return {"label": "No velocity yet", "late": False}
    if done >= total:
        return {"label": "Complete", "late": False}

    velocity = done / day
    remaining = total - done
    days_needed = remaining / velocity
    projected = now_cot.date() + timedelta(days=int(days_needed + 0.5))
    month_end = now_cot.date().replace(day=days_in_month)
    diff = (projected - month_end).days

    if diff <= 0:
        return {"label": f"On pace for {projected.strftime('%b %d')}", "late": False}
    return {"label": f"{projected.strftime('%b %d')} ({diff}d late)", "late": True}


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def generate_html(client_data, now_cot, done_statuses):
    template = (SCRIPT_DIR / "template.html").read_text(encoding="utf-8")

    month_year = now_cot.strftime("%B %Y")
    day_of_month = now_cot.day
    days_in_month = calendar.monthrange(now_cot.year, now_cot.month)[1]
    last_updated = now_cot.strftime("%b %d, %Y at %I:%M %p COT")

    total_done = sum(c["done"] for c in client_data.values())
    total_wip = sum(c["in_progress"] for c in client_data.values())
    total_remaining = sum(c["remaining"] for c in client_data.values())
    total_all = sum(c["total"] for c in client_data.values())
    overall_pct = round(total_done / total_all * 100) if total_all else 0
    ring_offset = round(565 * (1 - overall_pct / 100)) if overall_pct else 565

    # --- Fire Alerts ---
    alerts = build_fire_alerts(client_data)
    fire_html = ""
    if alerts:
        rows = ""
        for a in alerts:
            s_color = STATUS_COLORS.get(a["status"], "rgba(255,255,255,0.1)")
            if a["days"] < 0:
                day_label = f'{abs(a["days"])}d overdue'
                day_cls = "fire-overdue"
            elif a["days"] == 0:
                day_label = "Due today"
                day_cls = "fire-overdue"
            else:
                day_label = f'{a["days"]}d left'
                day_cls = "fire-soon"
            rows += f"""<div class="fire-row">
        <span class="fire-client">{a['client']}</span>
        <span class="fire-name">{_esc(a['name'])}</span>
        <span class="fire-pill" style="background:{s_color}">{a['status']}</span>
        <span class="fire-am">@{a['assignee']}</span>
        <span class="fire-days {day_cls}">{day_label}</span>
      </div>"""
        fire_html = f"""
  <div class="fire-section anim" style="animation-delay:0.12s">
    <div class="sec-title"><span class="fire-icon">&#9888;</span> Urgent — Due Within {FIRE_ALERT_DAYS} Days <span class="ln"></span></div>
    <div class="fire-box">{rows}</div>
  </div>"""

    # --- Summary Stats ---
    summary_html = f"""
    <div class="s-stat"><div class="s-num" data-count="{total_done}">{total_done}</div><div class="s-label">Scheduled</div></div>
    <div class="s-stat"><div class="s-num" data-count="{total_wip}">{total_wip}</div><div class="s-label">In Progress</div></div>
    <div class="s-stat"><div class="s-num" data-count="{total_remaining}">{total_remaining}</div><div class="s-label">Remaining</div></div>
    <div class="s-stat"><div class="s-num" data-count="{total_all}">{total_all}</div><div class="s-label">Total</div></div>
    """

    # --- AM Scoreboard ---
    scoreboard = build_am_scoreboard(client_data, done_statuses)
    am_html = ""
    if scoreboard:
        am_cards = ""
        for am in scoreboard:
            am_cards += f"""<div class="am-card">
        <div class="am-name">@{_esc(am['name'])}</div>
        <div class="am-nums"><strong>{am['done']}</strong> <span>/ {am['total']}</span></div>
        <div class="am-bar-track"><div class="am-bar-fill" style="width:{am['pct']}%"></div></div>
        <div class="am-pct" data-count="{am['pct']}">{am['pct']}%</div>
      </div>"""
        am_html = f"""
  <div class="am-section anim" style="animation-delay:0.2s">
    <div class="sec-title">Account Managers <span class="ln"></span></div>
    <div class="am-grid">{am_cards}</div>
  </div>"""

    # --- Client Cards with drill-down + projected ---
    health_cls = {"green": "b-on", "yellow": "b-at", "red": "b-behind"}
    health_lbl = {"green": "On Track", "yellow": "At Risk", "red": "Behind"}

    cards_html = ""
    for i, (client_name, data) in enumerate(client_data.items()):
        health = data["health"]
        done_pct = round(data["done"] / data["total"] * 100) if data["total"] else 0
        wip_pct = round(data["in_progress"] / data["total"] * 100) if data["total"] else 0

        month_label = f"M{data['majority_month']}" if data.get("majority_month") else ""
        month_tag = f'<span class="m-tag">{month_label}</span>' if month_label else ""

        oom_count = data.get("out_of_month_count", 0)
        oom_badge = f'<span class="b-oom">{oom_count} out-of-month</span>' if oom_count > 0 else ""

        proj = data.get("projected", {})
        proj_cls = "proj-late" if proj.get("late") else "proj-ok"
        proj_html = f'<div class="c-proj {proj_cls}">{proj.get("label", "")}</div>'

        # Drill-down task list
        task_rows = ""
        for t in data.get("tasks", []):
            t_name = _esc(t["name"])
            t_status = t["status"]
            t_color = STATUS_COLORS.get(t_status, "rgba(255,255,255,0.08)")
            t_am = t["assignee"]
            d = t["days_until_due"]
            if d is None:
                due_label, due_cls = "No date", ""
            elif d < 0:
                due_label, due_cls = f"{abs(d)}d late", "drill-overdue"
            elif d <= 5:
                due_label, due_cls = f"{d}d", "drill-urgent"
            else:
                due_label, due_cls = f"{d}d", ""
            task_rows += f'<div class="drill-row"><span class="drill-name">{t_name}</span><span class="drill-pill" style="background:{t_color}">{t_status}</span><span class="drill-am">@{t_am}</span><span class="drill-due {due_cls}">{due_label}</span></div>\n'

        drill_html = f"""<details class="drill">
        <summary class="drill-toggle">View all campaigns <span class="drill-cnt">{data['total']}</span></summary>
        <div class="drill-list">{task_rows}</div>
      </details>""" if data["total"] > 0 else ""

        delay = 0.3 + i * 0.1

        cards_html += f"""
    <div class="card anim" style="animation-delay:{delay:.2f}s">
      <div class="c-head">
        <div class="c-name"><span class="c-dot"></span>{client_name}{month_tag}</div>
        <div class="c-badges">{oom_badge}<span class="badge {health_cls[health]}">{health_lbl[health]}</span></div>
      </div>
      <div class="big-num"><span data-count="{data['done']}">{data['done']}</span> <span class="of">/ {data['total']}</span></div>
      <div class="prog-track"><div class="prog-done" style="width:{done_pct}%"></div><div class="prog-wip" style="width:{wip_pct}%"></div></div>
      <div class="c-stats">
        <div><strong>{data['done']}</strong> done</div>
        <div><strong>{data['in_progress']}</strong> in progress</div>
        <div><strong>{data['remaining']}</strong> remaining</div>
      </div>
      {proj_html}
      {drill_html}
    </div>"""

    # --- Detail Table with Projected column ---
    dot_map = {"green": "h-good", "yellow": "h-warn", "red": "h-bad"}
    detail_html = ""
    for client_name, data in client_data.items():
        health = data["health"]
        pct = round(data["done"] / data["total"] * 100) if data["total"] else 0
        dot_cls = dot_map[health]
        month_label = f"M{data['majority_month']}" if data.get("majority_month") else "-"
        proj = data.get("projected", {})
        proj_cls = "proj-late" if proj.get("late") else "proj-ok"

        detail_html += f"""
      <tr>
        <td><span class="c-dot" style="display:inline-block;vertical-align:middle;margin-right:8px"></span>{client_name}</td>
        <td>{month_label}</td>
        <td>{data['done']}</td>
        <td>{data['in_progress']}</td>
        <td>{data['remaining']}</td>
        <td>{data['total']}</td>
        <td class="pct">{pct}%</td>
        <td class="{proj_cls}" style="font-size:0.8rem">{proj.get('label','')}</td>
        <td><span class="h-dot {dot_cls}"></span></td>
      </tr>"""

    # --- Pipeline ---
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

    legend_html = ""
    for status in sorted_statuses:
        s_color = STATUS_COLORS.get(status, "rgba(255,255,255,0.1)")
        legend_html += f'<div class="leg-item"><span class="leg-sw" style="background:{s_color}"></span>{status.title()}</div>'

    # --- Out-of-month ---
    has_oom = any(d.get("out_of_month_count", 0) > 0 for d in client_data.values())
    oom_html = ""
    if has_oom:
        oom_items = ""
        for client_name, data in client_data.items():
            oom_tasks = data.get("out_of_month_tasks", [])
            if not oom_tasks:
                continue
            task_list = ""
            for t in oom_tasks:
                task_list += f'<div class="oom-t"><span>{_esc(t["name"])}</span><span class="oom-ts">{t.get("status","unknown")}</span></div>\n'
            oom_items += f"""
      <details class="oom-cl">
        <summary><span class="c-dot" style="display:inline-block"></span>{client_name}<span class="oom-cnt">{len(oom_tasks)} task{"s" if len(oom_tasks)!=1 else ""}</span></summary>
        <div class="oom-tasks">{task_list}</div>
      </details>"""
        oom_html = f"""
  <div class="oom-section anim" style="animation-delay:0.9s">
    <div class="sec-title">Out-of-Month Campaigns <span class="ln"></span></div>
    <div class="oom-box">
      <div class="oom-desc">These campaigns don't match the current month naming convention. Review needed.</div>
      {oom_items}
    </div>
  </div>"""

    # --- Fill template ---
    html = template
    for k, v in {
        "{{MONTH_YEAR}}": month_year,
        "{{DAY_OF_MONTH}}": str(day_of_month),
        "{{DAYS_IN_MONTH}}": str(days_in_month),
        "{{LAST_UPDATED}}": last_updated,
        "{{OVERALL_PCT}}": str(overall_pct),
        "{{RING_OFFSET}}": str(ring_offset),
        "{{FIRE_ALERTS_SECTION}}": fire_html,
        "{{SUMMARY_STATS}}": summary_html,
        "{{AM_SCOREBOARD_SECTION}}": am_html,
        "{{CLIENT_CARDS}}": cards_html,
        "{{DETAIL_ROWS}}": detail_html,
        "{{PIPELINE_BARS}}": pipeline_html,
        "{{PIPELINE_LEGEND}}": legend_html,
        "{{OUT_OF_MONTH_SECTION}}": oom_html,
    }.items():
        html = html.replace(k, v)

    return html


# ---------------------------------------------------------------------------
# Slack notification
# ---------------------------------------------------------------------------

def post_slack_summary(client_data, now_cot, dashboard_url):
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

    alerts = build_fire_alerts(client_data)
    if alerts:
        lines.append(f":rotating_light: *{len(alerts)} campaigns due within {FIRE_ALERT_DAYS} days and not ready*\n")

    health_emoji = {"green": ":large_green_circle:", "yellow": ":large_yellow_circle:", "red": ":red_circle:"}
    for name, data in client_data.items():
        emoji = health_emoji[data["health"]]
        month_label = f" (M{data['majority_month']})" if data.get("majority_month") else ""
        proj = data.get("projected", {}).get("label", "")
        lines.append(f"{emoji} *{name}*{month_label}: {data['done']}/{data['total']} scheduled | {proj}")

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
        raw_tasks = fetch_tasks_for_month(list_id, headers, year, month)
        print(f"  Found {len(raw_tasks)} tasks with due dates in {now_cot.strftime('%B')}")

        current_tasks, oom_tasks, majority_month = split_by_month_convention(raw_tasks)
        if majority_month is not None:
            print(f"  Majority month: M{majority_month} ({len(current_tasks)} tasks)")
            if oom_tasks:
                print(f"  Out-of-month: {len(oom_tasks)} tasks")
        else:
            print(f"  No month convention detected, counting all tasks")

        result = classify_tasks(current_tasks, done_statuses, in_progress_statuses)
        health = compute_health(result["done"], result["total"], day_of_month, days_in_month)
        processed = process_tasks(current_tasks, now_cot)
        projected = compute_projected_completion(result["done"], result["total"], now_cot, days_in_month)

        client_data[client_name] = {
            **result,
            "color": color,
            "health": health,
            "majority_month": majority_month,
            "out_of_month_count": len(oom_tasks),
            "out_of_month_tasks": [
                {"name": t.get("name", "Untitled"), "status": t.get("status", {}).get("status", "unknown")}
                for t in oom_tasks
            ],
            "tasks": processed,
            "projected": projected,
        }

        print(f"  Done: {result['done']} | In Progress: {result['in_progress']} | Remaining: {result['remaining']} | Health: {health} | {projected['label']}")

    html = generate_html(client_data, now_cot, done_statuses)
    output_path = SCRIPT_DIR / "docs" / "index.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"\n[HTML] Written to {output_path}")

    dashboard_url = "https://milton-collab.github.io/campaign-dashboard/"
    post_slack_summary(client_data, now_cot, dashboard_url)

    print("[DONE] Dashboard updated.")


if __name__ == "__main__":
    main()
