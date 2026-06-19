#!/usr/bin/env python3
"""
Earnings Calendar Builder
Fetches live data and generates a self-contained HTML file.
Run by GitHub Actions every hour on trading days.
"""

import urllib.request
import xml.etree.ElementTree as ET
import json
import os
import re
import html as html_mod
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

EASTERN = ZoneInfo("America/New_York")

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

# ── 2. Earnings history ───────────────────────────────────────────────────────
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

# Load cached history (accumulates 3+ years over time)
CACHE_FILE = 'data/history_cache.json'
cached_history = {}
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE) as f:
            cached_history = json.load(f)
        print(f"  Loaded cache: {len(cached_history)} tickers")
    except:
        pass

def fetch_history_yf(ticker):
    """yfinance — gives full 3-year history; blocked on GitHub Actions by Yahoo."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        ed = t.get_earnings_dates(limit=20)
        if ed is None or ed.empty:
            return []
        now = datetime.now(timezone.utc)
        past = ed[ed.index < now].dropna(subset=['Reported EPS'])
        rows = []
        for dt, row in past.iterrows():
            rows.append({
                'fiscalQtrEnd':      dt.strftime('%b %Y'),
                'dateReported':      dt.strftime('%-m/%-d/%Y'),
                'eps':               round(float(row['Reported EPS']), 2),
                'consensusForecast': str(round(float(row['EPS Estimate']), 2)) if row['EPS Estimate'] == row['EPS Estimate'] else '',
                'percentageSurprise':str(round(float(row['Surprise(%)']), 2))  if row['Surprise(%)']  == row['Surprise(%)']  else '',
            })
        return rows
    except:
        return []

def fetch_history_nasdaq(ticker):
    """NASDAQ API — always works from GitHub Actions; returns ~4 most recent quarters."""
    url = f'https://api.nasdaq.com/api/company/{ticker.lower()}/earnings-surprise'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read())
        return d.get('data', {}).get('earningsSurpriseTable', {}).get('rows', []) or []
    except:
        return []

def merge_history(fresh, cached):
    """Merge fresh rows with cache, deduplicate by quarter, sort newest first."""
    if not fresh and not cached:
        return []
    by_quarter = {r['fiscalQtrEnd']: r for r in cached}
    for r in fresh:
        by_quarter[r['fiscalQtrEnd']] = r  # fresh overwrites cached
    def sort_key(r):
        try: return datetime.strptime(r['fiscalQtrEnd'], '%b %Y')
        except: return datetime.min
    return sorted(by_quarter.values(), key=sort_key, reverse=True)

def fetch_history(ticker):
    rows = fetch_history_yf(ticker)
    if not rows:
        rows = fetch_history_nasdaq(ticker)
    merged = merge_history(rows, cached_history.get(ticker, []))
    return ticker, merged

print(f"Fetching history for {len(top_tickers)} tickers...")
history = {}
with ThreadPoolExecutor(max_workers=10) as ex:
    for ticker, rows in ex.map(fetch_history, top_tickers, timeout=120):
        if rows:
            history[ticker] = rows
print(f"  Got history for {len(history)} tickers")

# Save updated cache back to repo so history accumulates over time
os.makedirs('data', exist_ok=True)
with open(CACHE_FILE, 'w') as f:
    json.dump(history, f)
print(f"  Cache saved: {len(history)} tickers")

# ── 3. News ───────────────────────────────────────────────────────────────────
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

# ── 4. Stock meta lookup ──────────────────────────────────────────────────────
stock_meta = {}
for date_str, rows in earnings.items():
    for r in rows:
        sym = r.get('symbol', '')
        if sym:
            tl = ('Pre-market'  if r.get('time') == 'time-pre-market'  else
                  'After hours' if r.get('time') == 'time-after-hours' else 'TBD')
            stock_meta[sym] = {
                'name': r.get('name', ''),
                'when': tl,
                'eps':  r.get('epsForecast', ''),
                'q':    r.get('fiscalQuarterEnding', ''),
                'date': date_str,
            }

# ── 5. Serialize & write ──────────────────────────────────────────────────────
built_at = datetime.now(EASTERN).strftime('%b %d, %Y at %-I:%M %p ET')

with open('template.html', 'r') as f:
    template = f.read()

output = (template
    .replace('__EARNINGS_JS__', json.dumps(earnings,   ensure_ascii=False))
    .replace('__HISTORY_JS__',  json.dumps(history,    ensure_ascii=False))
    .replace('__NEWS_JS__',     json.dumps(news,       ensure_ascii=False))
    .replace('__META_JS__',     json.dumps(stock_meta, ensure_ascii=False))
    .replace('__BUILT_AT__',    json.dumps(built_at)))

with open('docs/index.html', 'w') as f:
    f.write(output)

print(f"\nBuild complete: {total_companies} companies, {len(history)} with history, {len(news)} with news")
print(f"Built at: {built_at}")
