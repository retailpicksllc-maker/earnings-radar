#!/usr/bin/env python3
"""
Earnings Calendar Builder
Fetches live data and generates a self-contained HTML file.
Run by GitHub Actions every hour on trading days.
"""

import urllib.request
import xml.etree.ElementTree as ET
import json
import re
import html as html_mod
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
from concurrent.futures import ThreadPoolExecutor

print("Starting build...")

# ── 1. Earnings calendar (next 40 trading days) ───────────────────────────────
def fetch_earnings_day(date_str):
    url = f'https://api.nasdaq.com/api/calendar/earnings?date={date_str}'
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read()).get('data', {}).get('rows', [])
        return date_str, rows
    except Exception as e:
        print(f"  ERR earnings {date_str}: {e}")
        return date_str, []

today = datetime.now(timezone.utc)
# Skip weekends
trading_days = []
d = today
while len(trading_days) < 40:
    if d.weekday() < 5:
        trading_days.append(d.strftime('%Y-%m-%d'))
    d += timedelta(days=1)

print(f"Fetching earnings for {len(trading_days)} trading days...")
earnings = {}
with ThreadPoolExecutor(max_workers=10) as ex:
    for date_str, rows in ex.map(fetch_earnings_day, trading_days, timeout=120):
        confirmed = [r for r in (rows or []) if r.get('time') in ('time-pre-market', 'time-after-hours')]
        if confirmed:
            earnings[date_str] = confirmed

total_companies = sum(len(v) for v in earnings.values())
print(f"  Got {total_companies} companies across {len(earnings)} days")

# ── 2. Earnings history (top 200 by market cap) ───────────────────────────────
def parse_mcap(s):
    if not s: return 0
    try: return float(s.replace('$', '').replace(',', ''))
    except: return 0

all_rows_flat = [(parse_mcap(r.get('marketCap', '')), r.get('symbol', ''))
                 for rows in earnings.values() for r in rows]
seen = set()
top_tickers = []
for mc, sym in sorted(all_rows_flat, reverse=True):
    if sym and sym not in seen and mc > 1e9:
        seen.add(sym)
        top_tickers.append(sym)
    if len(top_tickers) >= 200:
        break

def fetch_history(ticker):
    url = f'https://api.nasdaq.com/api/company/{ticker.lower()}/earnings-surprise'
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read())
        rows = d.get('data', {}).get('earningsSurpriseTable', {}).get('rows', [])
        return ticker, rows
    except:
        return ticker, []

print(f"Fetching history for {len(top_tickers)} tickers...")
history = {}
with ThreadPoolExecutor(max_workers=30) as ex:
    for ticker, rows in ex.map(fetch_history, top_tickers, timeout=90):
        if rows:
            history[ticker] = rows
print(f"  Got history for {len(history)} tickers")

# ── 3. News (top 200 tickers) ─────────────────────────────────────────────────
def strip_html(t):
    t = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', t or '', flags=re.DOTALL)
    return re.sub(r'<[^>]+>', '', t).strip()

def parse_rss_date(s):
    for fmt in ['%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M %z']:
        try: return datetime.strptime((s or '').strip(), fmt)
        except: pass
    return datetime.now(timezone.utc)

def fetch_news(ticker):
    url = f'https://www.nasdaq.com/feed/rssoutbound?symbol={ticker}'
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
        })
        with urllib.request.urlopen(req, timeout=6) as r:
            root = ET.fromstring(r.read())
        items = []
        for item in root.findall('.//item')[:10]:
            title = strip_html(item.findtext('title', ''))
            if not title: continue
            dt = parse_rss_date(item.findtext('pubDate', ''))
            items.append({
                'title': title,
                'link':  item.findtext('link', ''),
                'desc':  strip_html(item.findtext('description', ''))[:180],
                'time':  dt.astimezone(EASTERN).strftime('%-I:%M %p ET'),
                'date':  dt.astimezone(EASTERN).strftime('%b %d'),
                'ts':    int(dt.timestamp()),
            })
        return ticker, items
    except:
        return ticker, []

news_tickers = list(history.keys())
print(f"Fetching news for {len(news_tickers)} tickers...")
news = {}
with ThreadPoolExecutor(max_workers=30) as ex:
    for ticker, items in ex.map(fetch_news, news_tickers, timeout=90):
        if items:
            news[ticker] = items
print(f"  Got news for {len(news)} tickers")

# ── 4. Build stock meta lookup ────────────────────────────────────────────────
stock_meta = {}
for date_str, rows in earnings.items():
    for r in rows:
        sym = r.get('symbol', '')
        if sym:
            tl = ('Pre-market'   if r.get('time') == 'time-pre-market'  else
                  'After hours'  if r.get('time') == 'time-after-hours' else 'TBD')
            stock_meta[sym] = {
                'name': r.get('name', ''),
                'when': tl,
                'eps':  r.get('epsForecast', ''),
                'q':    r.get('fiscalQuarterEnding', ''),
                'date': date_str,
            }

# ── 5. Serialize ──────────────────────────────────────────────────────────────
built_at = datetime.now(EASTERN).strftime('%b %d, %Y at %-I:%M %p ET')

earnings_js = json.dumps(earnings, ensure_ascii=False)
history_js  = json.dumps(history,  ensure_ascii=False)
news_js     = json.dumps(news,     ensure_ascii=False)
meta_js     = json.dumps(stock_meta, ensure_ascii=False)
built_js    = json.dumps(built_at)

# ── 6. Read HTML template and inject data ─────────────────────────────────────
with open('template.html', 'r') as f:
    template = f.read()

output = template.replace('__EARNINGS_JS__', earnings_js) \
                 .replace('__HISTORY_JS__',  history_js)  \
                 .replace('__NEWS_JS__',     news_js)      \
                 .replace('__META_JS__',     meta_js)      \
                 .replace('__BUILT_AT__',    built_js)

with open('docs/index.html', 'w') as f:
    f.write(output)

print(f"\nBuild complete: {total_companies} companies, {len(history)} with history, {len(news)} with news")
print(f"Built at: {built_at}")
