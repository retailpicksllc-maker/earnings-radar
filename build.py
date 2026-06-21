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

# ── Past earnings calendar (cached, up to 3 years) ────────────────────────────
PAST_CACHE_FILE = 'data/past_calendar_cache.json'
past_calendar_cached = {}
if os.path.exists(PAST_CACHE_FILE):
    try:
        with open(PAST_CACHE_FILE) as f:
            past_calendar_cached = json.load(f)
        print(f"  Loaded past cache: {len(past_calendar_cached)} days cached")
    except:
        pass

# Generate past trading days (up to 3 years back)
past_days_needed = []
d = today - timedelta(days=1)
cutoff = today - timedelta(days=365*3)
while d >= cutoff:
    if d.weekday() < 5:
        past_days_needed.append(d.strftime('%Y-%m-%d'))
    d -= timedelta(days=1)

dates_to_fetch = [d for d in past_days_needed if d not in past_calendar_cached]
print(f"Fetching {len(dates_to_fetch)} uncached past trading days...")
if dates_to_fetch:
    with ThreadPoolExecutor(max_workers=20) as ex:
        for date_str, rows in ex.map(fetch_earnings_day, dates_to_fetch, timeout=600):
            past_calendar_cached[date_str] = [r for r in (rows or []) if r]
    os.makedirs('data', exist_ok=True)
    with open(PAST_CACHE_FILE, 'w') as f:
        json.dump(past_calendar_cached, f)
    days_with_data = sum(1 for v in past_calendar_cached.values() if v)
    print(f"  Past cache saved: {days_with_data} days with earnings data")

past_earnings = {d: rows for d, rows in past_calendar_cached.items() if rows}
print(f"  Past earnings: {len(past_earnings)} days with data")

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

# Add top past tickers by market cap (for revenue lookup on past calendar dates)
past_rows_flat = [(parse_mcap(r.get('marketCap', '')), r.get('symbol', ''))
                  for rows in past_earnings.values() for r in rows]
for mc, sym in sorted(past_rows_flat, reverse=True):
    if sym and sym not in seen and mc > 10e9:
        seen.add(sym)
        top_tickers.append(sym)
    if len(top_tickers) >= 300:
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

# ── Revenue actuals via yfinance ─────────────────────────────────────────────
import yfinance as yf

REV_CACHE_FILE = 'data/revenue_cache.json'
revenue_cache = {}
if os.path.exists(REV_CACHE_FILE):
    try:
        with open(REV_CACHE_FILE) as f:
            revenue_cache = json.load(f)
        print(f"  Loaded revenue cache: {len(revenue_cache)} tickers")
    except:
        pass

def _load_fx():
    try:
        r = urllib.request.urlopen('https://open.er-api.com/v6/latest/USD', timeout=8)
        return json.loads(r.read())['rates']
    except:
        return {}

_FX = _load_fx()

def _yf_revenue(ticker):
    """Fetch quarterly revenue via yfinance. Returns {Mon YYYY: $M USD}."""
    try:
        t = yf.Ticker(ticker)
        stmt = t.quarterly_income_stmt
        if stmt is None or stmt.empty:
            return {}
        rev_row = None
        for label in ['Total Revenue', 'Revenue', 'Net Revenue', 'Gross Profit']:
            if label in stmt.index:
                rev_row = stmt.loc[label]
                break
        if rev_row is None:
            return {}
        # FX conversion using financialCurrency
        try:
            fc = (t.fast_info.get('currency') or
                  t.info.get('financialCurrency') or 'USD')
        except:
            fc = 'USD'
        # fast_info.currency is the trading currency (always USD for ADRs)
        # We need financialCurrency for the actual reporting currency
        try:
            fc2 = t.info.get('financialCurrency', fc)
            if fc2: fc = fc2
        except:
            pass
        fx = _FX.get(fc, 1.0) if fc != 'USD' else 1.0
        result = {}
        for dt, val in rev_row.dropna().items():
            if not val or val <= 0: continue
            val_usd = val / fx / 1e6
            if val_usd < 1 or val_usd > 2e6: continue
            try:
                key = dt.strftime('%b %Y') if hasattr(dt, 'strftime') else (
                    datetime.strptime(str(dt)[:7], '%Y-%m').strftime('%b %Y'))
                result[key] = round(val_usd, 1)
            except:
                continue
        return result
    except Exception as e:
        return {}

def rev_is_stale(ticker):
    """True if cache is missing or more than 3 months behind history."""
    if ticker not in revenue_cache or not revenue_cache[ticker]:
        return True
    hist_quarters = history.get(ticker, [])
    if not hist_quarters:
        return False
    try:
        latest_rev  = max(datetime.strptime(k, '%b %Y') for k in revenue_cache[ticker])
        latest_hist = max(datetime.strptime(q['fiscalQtrEnd'], '%b %Y')
                         for q in hist_quarters if q.get('fiscalQtrEnd'))
        return ((latest_hist.year - latest_rev.year) * 12 +
                (latest_hist.month - latest_rev.month)) > 3
    except:
        return False

all_rev_tickers = list(set(top_tickers) | set(history.keys()))
tickers_needing_rev = [t for t in all_rev_tickers if rev_is_stale(t)]
print(f"Fetching revenue for {len(tickers_needing_rev)} tickers via yfinance...")
revenue_data = dict(revenue_cache)

# SEC EDGAR CIK map (used as fallback for annual-only filers)
_cik_map = {}
try:
    _req = urllib.request.Request('https://www.sec.gov/files/company_tickers.json',
                                  headers={'User-Agent': 'retail.picksllc@gmail.com'})
    _cik_map = {v['ticker']: str(v['cik_str']).zfill(10)
                for v in json.loads(urllib.request.urlopen(_req, timeout=15).read()).values()}
except: pass

def _sec_annual_fallback(ticker):
    """For tickers with no yfinance revenue: try SEC annual (20-F/10-K) ÷ 4, expand to 4 quarters."""
    cik = _cik_map.get(ticker)
    if not cik: return {}
    try:
        url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
        req = urllib.request.Request(url, headers={'User-Agent': 'retail.picksllc@gmail.com'})
        facts = json.loads(urllib.request.urlopen(req, timeout=20).read())
        annual = {}
        for taxonomy in ['us-gaap', 'ifrs-full']:
            tax = facts.get('facts', {}).get(taxonomy, {})
            for field in ['Revenue', 'Revenues', 'RentalIncome',
                          'RevenueFromContractWithCustomerExcludingAssessedTax',
                          'SalesRevenueNet', 'NoninterestIncome']:
                if field not in tax: continue
                for cur, entries in tax[field].get('units', {}).items():
                    fx = _FX.get(cur, 1.0) if cur != 'USD' else 1.0
                    for e in entries:
                        if e.get('form') not in ('10-K', '20-F', '40-F'): continue
                        val = e.get('val', 0)
                        if val <= 0: continue
                        val_usd = val / fx / 1e6
                        if val_usd < 1 or val_usd > 2e6: continue
                        try:
                            s = datetime.strptime(e.get('start', e['end']), '%Y-%m-%d')
                            en = datetime.strptime(e['end'], '%Y-%m-%d')
                            if 330 <= (en - s).days <= 400:
                                k = en.strftime('%b %Y')
                                if k not in annual or val_usd > annual[k]:
                                    annual[k] = round(val_usd / 4, 1)
                        except: continue
                if annual: break
            if annual: break
        # Expand annual entries to all 4 quarters of that year
        result = {}
        for key, val in annual.items():
            end = datetime.strptime(key, '%b %Y')
            for offset in [0, -3, -6, -9]:
                mo = ((end.month - 1 + offset) % 12) + 1
                yr = end.year + ((end.month - 1 + offset) // 12)
                k = datetime(yr, mo, 1).strftime('%b %Y')
                if k not in result:
                    result[k] = val
        return result
    except: return {}

def _fetch_one(ticker):
    qtrs = _yf_revenue(ticker)
    if not qtrs:
        qtrs = _sec_annual_fallback(ticker)
    return ticker, qtrs

with ThreadPoolExecutor(max_workers=8) as ex:
    for ticker, qtrs in ex.map(_fetch_one, tickers_needing_rev, timeout=300):
        if qtrs:
            revenue_data[ticker] = qtrs

# Merge revenue into history — nearest-quarter match with fallback
# 1. Exact match  2. ±2 months (handles fiscal offset)  3. Most recent prior value (≤18 months)
def _nearest_rev(rev_dict, fqe):
    if not rev_dict or not fqe:
        return None
    if fqe in rev_dict:
        return rev_dict[fqe]
    try:
        target = datetime.strptime(fqe, '%b %Y')
        best_close_val, best_close_diff = None, 999
        best_prior_val, best_prior_diff = None, 999
        for k, v in rev_dict.items():
            try:
                kdt = datetime.strptime(k, '%b %Y')
                diff   = abs((kdt.year - target.year) * 12 + (kdt.month - target.month))
                signed = (target.year - kdt.year) * 12 + (target.month - kdt.month)
                if diff <= 2 and diff < best_close_diff:
                    best_close_diff, best_close_val = diff, v
                if 0 < signed <= 18 and signed < best_prior_diff:
                    best_prior_diff, best_prior_val = signed, v
            except:
                continue
        return best_close_val if best_close_val is not None else best_prior_val
    except:
        return None

for ticker, quarters in history.items():
    rev = revenue_data.get(ticker, {})
    for q in quarters:
        q['revActual'] = _nearest_rev(rev, q.get('fiscalQtrEnd', ''))

os.makedirs('data', exist_ok=True)
with open(REV_CACHE_FILE, 'w') as f:
    json.dump(revenue_data, f)
print(f"  Revenue cache saved: {len(revenue_data)} tickers")


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
    .replace('__PAST_EARNINGS_JS__', json.dumps(past_earnings, ensure_ascii=False))
    .replace('__EARNINGS_JS__', json.dumps(earnings,   ensure_ascii=False))
    .replace('__HISTORY_JS__',  json.dumps(history,    ensure_ascii=False))
    .replace('__NEWS_JS__',     json.dumps(news,       ensure_ascii=False))
    .replace('__META_JS__',     json.dumps(stock_meta, ensure_ascii=False))
    .replace('__BUILT_AT__',    json.dumps(built_at)))

with open('docs/index.html', 'w') as f:
    f.write(output)

print(f"\nBuild complete: {total_companies} companies, {len(history)} with history, {len(news)} with news")
print(f"Built at: {built_at}")
