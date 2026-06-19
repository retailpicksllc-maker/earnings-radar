# Earnings Calendar

Auto-updating earnings calendar with history and news for every stock reporting in the next 40 trading days.

**Live site:** `https://YOUR_USERNAME.github.io/earnings-calendar/`

## How it works

- **GitHub Actions** runs `build.py` every hour on weekdays
- `build.py` fetches live data from NASDAQ (free, no API key needed)
- Generates `docs/index.html` — a fully self-contained page
- GitHub Pages serves that file at your URL

## Setup (5 minutes)

1. Create a new **public** GitHub repo called `earnings-calendar`
2. Upload all files from this zip
3. Go to **Settings → Pages → Source** → set to `Deploy from branch: main / docs`
4. Done — your URL is `https://YOUR_USERNAME.github.io/earnings-calendar/`

The workflow runs automatically. You can also trigger it manually from the **Actions** tab → **Build Earnings Calendar** → **Run workflow**.

## Data sources
- Earnings calendar: NASDAQ API
- Earnings history (EPS surprise): NASDAQ API  
- Stock news: NASDAQ RSS feeds

No API keys required.
