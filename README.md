# Campaign Dashboard

Daily visual tracker for GrowthTrigger campaign progress across all clients.

- **Data source:** ClickUp API (task statuses per client list)
- **Output:** Static HTML dashboard at `docs/index.html`, served via GitHub Pages
- **Schedule:** Daily at 11 PM COT via GitHub Actions
- **Notification:** Slack summary with link

## Setup

1. Add secrets: `CLICKUP_API_TOKEN`, `SLACK_BOT_TOKEN`
2. Enable GitHub Pages: Settings → Pages → Branch: main, Folder: /docs
3. Dashboard URL: `https://milton-collab.github.io/campaign-dashboard/`

## Local testing

```bash
CLICKUP_API_TOKEN=your_token python3 dashboard.py
open docs/index.html
```
