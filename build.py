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

# ── 1. Earnings calendar (Finnhub) ───────────────────────────────────────────
FINNHUB_KEY = os.environ.get('FINNHUB_API_KEY', '')

def finnhub_get(path):
    url = f'https://finnhub.io/api/v1{path}&token={FINNHUB_KEY}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def map_finnhub_row(r):
    hour = r.get('hour', '')
    time_val = 'time-pre-market' if hour == 'bmo' else ('time-after-hours' if hour == 'amc' else 'time-not-supplied')
    q, yr = r.get('quarter', ''), r.get('year', '')
    fqe = f'Q{q}/{str(yr)[2:]}' if q and yr else ''
    return {
        'symbol': r.get('symbol', ''),
        'time': time_val,
        'fiscalQuarterEnding': fqe,
        'eps': r.get('epsEstimate'),
        'epsActual': r.get('epsActual'),
        'revenueEstimate': r.get('revenueEstimate'),
        'revenueActual': r.get('revenueActual'),
        'marketCap': '',
        'name': r.get('symbol', ''),
    }

def fetch_finnhub_range(from_d, to_d):
    try:
        data = finnhub_get(f'/calendar/earnings?from={from_d}&to={to_d}')
        rows = data.get('earningsCalendar', [])
        return [map_finnhub_row(r) for r in rows if r.get('symbol')]
    except Exception as e:
        print(f"  ERR Finnhub earnings {from_d}-{to_d}: {e}")
        return []

today = datetime.now(timezone.utc)
today_str = today.strftime('%Y-%m-%d')

# ── Upcoming earnings: NASDAQ API (per-day, next 14 trading days) ────────────
def fetch_nasdaq_day(date_str):
    url = f'https://api.nasdaq.com/api/calendar/earnings?date={date_str}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        rows = (data.get('data') or {}).get('rows') or []
        out = []
        for row in rows:
            sym = row.get('symbol', '').strip()
            if not sym:
                continue
            out.append({
                'symbol': sym,
                'time': row.get('time', 'time-not-supplied'),
                'fiscalQuarterEnding': row.get('fiscalQuarterEnding', ''),
                'eps': row.get('epsForecast'),
                'epsActual': None,
                'revenueEstimate': None,
                'revenueActual': None,
                'marketCap': row.get('marketCap', ''),
                'name': row.get('name', sym),
            })
        return out
    except Exception as e:
        print(f"  ERR NASDAQ {date_str}: {e}")
        return []

# Build list of next 14 trading days
td_list = []
d = today
while len(td_list) < 14:
    if d.weekday() < 5:
        td_list.append(d.strftime('%Y-%m-%d'))
    d += timedelta(days=1)

print(f"Fetching upcoming earnings from NASDAQ ({td_list[0]} to {td_list[-1]})...")
earnings = {}
with ThreadPoolExecutor(max_workers=5) as ex:
    results = list(ex.map(fetch_nasdaq_day, td_list))
# Load existing mktcap cache
mktcap_cache_path = 'data/marketcap_cache.json'
try:
    with open(mktcap_cache_path) as _f: mktcap_cache = json.load(_f)
except: mktcap_cache = {}

for date_str, rows in zip(td_list, results):
    if rows:
        earnings[date_str] = rows
        # Populate mktcap cache from NASDAQ data
        for r in rows:
            sym = r.get('symbol','')
            mc = r.get('marketCap','')
            if sym and mc:
                mktcap_cache[sym] = mc

# Fill in further-out dates (Aug+) from Finnhub where NASDAQ is sparse
far_td_list = []
d2 = today + timedelta(days=14)
while len(far_td_list) < 26:
    if d2.weekday() < 5:
        far_td_list.append(d2.strftime('%Y-%m-%d'))
    d2 += timedelta(days=1)
if far_td_list:
    try:
        fh_data = finnhub_get(f'/calendar/earnings?from={far_td_list[0]}&to={far_td_list[-1]}')
        for r in fh_data.get('earningsCalendar', []):
            sym = r.get('symbol', '')
            dt = r.get('date', '')
            if not sym or not dt:
                continue
            hour = r.get('hour', '')
            time_val = 'time-pre-market' if hour == 'bmo' else ('time-after-hours' if hour == 'amc' else 'time-not-supplied')
            earnings.setdefault(dt, []).append({
                'symbol': sym, 'time': time_val,
                'fiscalQuarterEnding': f"Q{r.get('quarter','')}/{str(r.get('year',''))[2:]}",
                'eps': r.get('epsEstimate'), 'epsActual': None,
                'revenueEstimate': None, 'revenueActual': None,
                'marketCap': '', 'name': sym,
            })
    except Exception as e:
        print(f"  ERR Finnhub far-out: {e}")

total_companies = sum(len(v) for v in earnings.values())
print(f"  Got {total_companies} companies across {len(earnings)} days")

# ── Past earnings calendar (Finnhub, cached in 90-day chunks) ────────────────
PAST_CACHE_FILE = 'data/past_calendar_cache.json'
past_calendar_cached = {}
if os.path.exists(PAST_CACHE_FILE):
    try:
        with open(PAST_CACHE_FILE) as f:
            past_calendar_cached = json.load(f)
        print(f"  Loaded past cache: {len(past_calendar_cached)} days cached")
    except:
        pass

# Build 90-day date ranges going back up to 1 year
# Always re-fetch the current 90-day window (catches recent reports)
cutoff = today - timedelta(days=365)
ranges = []
chunk_end = today - timedelta(days=1)
while chunk_end >= cutoff:
    chunk_start = max(chunk_end - timedelta(days=89), cutoff)
    ranges.append((chunk_start.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
    chunk_end = chunk_start - timedelta(days=1)

# Always refresh the most recent range; skip older ones if cached
recent_range = ranges[0] if ranges else None
ranges_to_fetch = []
for fr, to in ranges:
    cache_key = f'{fr}_{to}'
    if cache_key not in past_calendar_cached.get('_chunks', {}) or (fr, to) == recent_range:
        ranges_to_fetch.append((fr, to))

# Always re-fetch today and yesterday individually for fresh epsActual
hot_dates = []
for offset in [0, 1]:
    d = (today - timedelta(days=offset)).strftime('%Y-%m-%d')
    hot_dates.append(d)

print(f"Fetching {len(ranges_to_fetch)} past date ranges from Finnhub...")
chunks_done = past_calendar_cached.get('_chunks', {})
for fr, to in ranges_to_fetch:
    rows = fetch_finnhub_range(fr, to)
    confirmed = [r for r in rows if r['time'] in ('time-pre-market', 'time-after-hours')]
    # Store by date
    for row in confirmed:
        dt = row.get('date', '')
        if dt:
            past_calendar_cached.setdefault(dt, [])
            # Upsert by symbol — always update so epsActual/revenueActual refresh
            existing = [r for r in past_calendar_cached[dt] if r['symbol'] != row['symbol']]
            existing.append(row)
            past_calendar_cached[dt] = existing
    chunks_done[f'{fr}_{to}'] = True

# Fetch today/yesterday individually — always fresh, no cache skip
for hot_d in hot_dates:
    rows = fetch_finnhub_range(hot_d, hot_d)
    # Include all tickers (confirmed or not) for hot dates — they've already reported
    for row in rows:
        dt = row.get('date', hot_d)
        if dt and row.get('symbol'):
            past_calendar_cached.setdefault(dt, [])
            existing = [r for r in past_calendar_cached[dt] if r['symbol'] != row['symbol']]
            existing.append(row)
            past_calendar_cached[dt] = existing

past_calendar_cached['_chunks'] = chunks_done
os.makedirs('data', exist_ok=True)
with open(PAST_CACHE_FILE, 'w') as f:
    json.dump(past_calendar_cached, f)
print(f"  Past cache saved")

past_earnings = {d: rows for d, rows in past_calendar_cached.items() if d != '_chunks' and rows}
print(f"  Past earnings: {len(past_earnings)} days with data")

# ── 2. Earnings history ───────────────────────────────────────────────────────
def parse_mcap(s):
    if not s: return 0
    try: return float(s.replace('$', '').replace(',', ''))
    except: return 0

# top_tickers: for history fetch — keep lean (≤400)
# Priority 1: recent past reporters (last 14 days) with mc > 1B — always include
recent_14d = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
seen = set()
top_tickers = []
past_rows_flat = [(parse_mcap(r.get('marketCap', '')), r.get('symbol', ''), iso)
                  for iso, rows in past_earnings.items() for r in rows]
for mc, sym, iso in sorted(past_rows_flat, reverse=True):
    if sym and sym not in seen and mc > 1e9 and iso >= recent_14d:
        seen.add(sym)
        top_tickers.append(sym)
# Priority 2: top upcoming tickers by mcap
all_rows_flat = [(parse_mcap(r.get('marketCap', '')), r.get('symbol', ''))
                 for rows in earnings.values() for r in rows]
for mc, sym in sorted(all_rows_flat, reverse=True):
    if sym and sym not in seen and mc > 1e9:
        seen.add(sym)
        top_tickers.append(sym)
    if len(top_tickers) >= 300:
        break
# Priority 3: historical past by mcap up to 400 total
for mc, sym, iso in sorted(past_rows_flat, reverse=True):
    if sym and sym not in seen and mc > 10e9:
        seen.add(sym)
        top_tickers.append(sym)
    if len(top_tickers) >= 400:
        break

# rev_tickers: for revenue fetch — all recent calendar tickers (last 28 days + upcoming)
recent_cutoff = (datetime.now() - timedelta(days=28)).strftime('%Y-%m-%d')
rev_tickers = list({r.get('symbol','') for rows in earnings.values() for r in rows if r.get('symbol')})
for iso, rows in past_earnings.items():
    if iso >= recent_cutoff:
        for r in rows:
            sym = r.get('symbol','')
            if sym and sym not in rev_tickers:
                rev_tickers.append(sym)
# Also include top historical tickers by mcap
seen_rev = set(rev_tickers)
for mc, sym, _iso in sorted(past_rows_flat, reverse=True):
    if sym and sym not in seen_rev and mc > 5e9:
        seen_rev.add(sym)
        rev_tickers.append(sym)
    if len(rev_tickers) >= 800:
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
    for ticker, rows in ex.map(fetch_history, top_tickers, timeout=300):
        if rows:
            history[ticker] = rows
print(f"  Got history for {len(history)} tickers")

# Backfill from entire cache — any ticker ever stored is included (preserves manual injections)
for sym, rows in cached_history.items():
    if sym and sym not in history and rows:
        history[sym] = rows
print(f"  After cache backfill: {len(history)} tickers")

# Save updated cache back to repo so history accumulates over time
os.makedirs('data', exist_ok=True)
with open(CACHE_FILE, 'w') as f:
    json.dump(history, f)
print(f"  Cache saved: {len(history)} tickers")

# ── Revenue actuals (Finnhub) ─────────────────────────────────────────────────
REV_CACHE_FILE = 'data/revenue_cache.json'
REV_EST_CACHE_FILE = 'data/rev_est_cache.json'
EPS_EST_CACHE_FILE = 'data/eps_est_cache.json'
FMP_EST_CACHE_FILE  = 'data/fmp_est_cache.json'
FMP_API_KEY = os.environ.get('FMP_API_KEY', '')
revenue_cache = {}
rev_est_cache = {}
eps_est_cache = {}
if os.path.exists(REV_CACHE_FILE):
    try:
        with open(REV_CACHE_FILE) as f:
            revenue_cache = json.load(f)
        print(f"  Loaded revenue cache: {len(revenue_cache)} tickers")
    except:
        pass
if os.path.exists(REV_EST_CACHE_FILE):
    try:
        with open(REV_EST_CACHE_FILE) as f:
            rev_est_cache = json.load(f)
        print(f"  Loaded rev estimate cache: {len(rev_est_cache)} tickers")
    except:
        pass
if os.path.exists(EPS_EST_CACHE_FILE):
    try:
        with open(EPS_EST_CACHE_FILE) as f:
            eps_est_cache = json.load(f)
        print(f"  Loaded EPS estimate cache: {len(eps_est_cache)} tickers")
    except:
        pass
fmp_est_cache = {}
if os.path.exists(FMP_EST_CACHE_FILE):
    try:
        with open(FMP_EST_CACHE_FILE) as f:
            fmp_est_cache = json.load(f)
        print(f"  Loaded FMP est cache: {len(fmp_est_cache)} tickers")
    except:
        pass

def _load_fx():
    try:
        r = urllib.request.urlopen('https://open.er-api.com/v6/latest/USD', timeout=8)
        return json.loads(r.read())['rates']
    except:
        return {}

_FX = _load_fx()

def _finnhub_revenue(ticker):
    """Revenue financials require Finnhub paid tier."""
    return {}
    try:
        data = finnhub_get(f'/stock/financials?symbol={ticker}&statement=income&freq=quarterly')
        qtrs = (data.get('financials') or {}).get('quarterly') or []
        result = {}
        for q in qtrs:
            date = q.get('date', '')
            rev = q.get('revenue') or q.get('totalRevenue')
            if not date or not rev or rev <= 0:
                continue
            val_m = round(float(rev) / 1e6, 1)
            if not (0.1 < val_m < 2e6):
                continue
            try:
                key = datetime.strptime(date[:7], '%Y-%m').strftime('%b %Y')
                result[key] = val_m
            except:
                pass
        return result
    except:
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

all_rev_tickers = list(set(rev_tickers) | set(history.keys()))
tickers_needing_rev = [t for t in all_rev_tickers if rev_is_stale(t)]
print(f"Fetching revenue for {len(tickers_needing_rev)} tickers via Finnhub...")
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


def _fmp_estimates(ticker):
    """Fetch EPS + revenue estimates from FMP /v3/analyst-estimates."""
    if not FMP_API_KEY:
        return {}, {}
    try:
        url = f'https://financialmodelingprep.com/api/v3/analyst-estimates/{ticker}?period=quarter&limit=8&apikey={FMP_API_KEY}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
        if not rows or not isinstance(rows, list):
            return {}, {}
        eps_out = {}
        rev_out = {}
        for row in rows:
            date = row.get('date', '')  # e.g. "2025-03-31"
            if not date:
                continue
            # FMP uses full ISO date; convert to "Mar 2025" style key
            try:
                from datetime import datetime as _dt
                d = _dt.strptime(date, '%Y-%m-%d')
                qk = d.strftime('%b %Y')
            except:
                qk = date
            eps = row.get('estimatedEpsAvg')
            rev = row.get('estimatedRevenueAvg')
            if eps is not None:
                try: eps_out[qk] = float(eps)
                except: pass
            if rev is not None:
                try: rev_out[qk] = float(rev)
                except: pass
        return eps_out, rev_out
    except Exception as e:
        return {}, {}

def _finnhub_rev_estimate_monthly(ticker):
    _, rev = _fmp_estimates(ticker)
    return rev

def _finnhub_eps_estimate(ticker):
    eps, _ = _fmp_estimates(ticker)
    return eps

def _finnhub_rev_estimate(ticker):
    _, rev = _fmp_estimates(ticker)
    return rev

def _fetch_one(ticker):
    qtrs = _sec_annual_fallback(ticker)
    return ticker, qtrs

rev_est_data = dict(rev_est_cache)
with ThreadPoolExecutor(max_workers=8) as ex:
    for ticker, qtrs in ex.map(_fetch_one, tickers_needing_rev, timeout=300):
        if qtrs:
            revenue_data[ticker] = qtrs

# Upcoming tickers need fresh estimate attempts even if previously cached empty
upcoming_syms = set(r.get('symbol','') for rows in earnings.values() for r in rows if r.get('symbol'))

# Fetch revenue estimates — always retry upcoming tickers with empty cache
est_tickers = [t for t in rev_tickers if t not in rev_est_data or (t in upcoming_syms and not rev_est_data.get(t))]
print(f"Fetching revenue estimates for {len(est_tickers)} tickers...")
with ThreadPoolExecutor(max_workers=8) as ex:
    for ticker, est in ex.map(lambda t: (t, _finnhub_rev_estimate_monthly(t)), est_tickers, timeout=300):
        if est:
            rev_est_data[ticker] = est
print(f"  Revenue estimates collected: {len(rev_est_data)} tickers")

# Fetch EPS estimates — always retry upcoming tickers with empty cache
eps_est_data = dict(eps_est_cache)
eps_est_fetch = [t for t in rev_tickers if t not in eps_est_data or (t in upcoming_syms and not eps_est_data.get(t))]
print(f"Fetching EPS estimates for {len(eps_est_fetch)} tickers...")
with ThreadPoolExecutor(max_workers=8) as ex:
    for ticker, est in ex.map(lambda t: (t, _finnhub_eps_estimate(t)), eps_est_fetch, timeout=300):
        if est:
            eps_est_data[ticker] = est
print(f"  EPS estimates collected: {len(eps_est_data)} tickers")

# Fetch Finnhub per-quarter revenue estimates (keyed by report ISO date)
fmp_est_data = dict(fmp_est_cache)
if FINNHUB_KEY:
    fmp_fetch = [t for t in rev_tickers if t not in fmp_est_data]
    print(f"Fetching Finnhub revenue estimates for {len(fmp_fetch)} tickers...")
    with ThreadPoolExecutor(max_workers=4) as ex:
        for ticker, est in ex.map(lambda t: (t, _finnhub_rev_estimate(t)), fmp_fetch, timeout=300):
            if est:
                fmp_est_data[ticker] = est
    print(f"  Finnhub estimates collected: {len(fmp_est_data)} tickers")
else:
    fmp_est_data = dict(fmp_est_cache)
    print("  FINNHUB_KEY not set — skipping Finnhub revenue estimates")

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
    fmp = fmp_est_data.get(ticker, {})
    for q in quarters:
        q['revActual'] = _nearest_rev(rev, q.get('fiscalQtrEnd', ''))
        # Match Finnhub rev estimate via fiscalQtrEnd "Jan 2026" -> nearest YYYY-MM-DD period
        q['revEstimate'] = None
        fqe = q.get('fiscalQtrEnd', '')
        if fqe and fmp:
            try:
                fqe_dt = datetime.strptime(fqe, '%b %Y')
                best_val, best_diff = None, 999
                for period_iso, val in fmp.items():
                    try:
                        p_dt = datetime.strptime(period_iso[:7], '%Y-%m')
                        diff = abs((p_dt.year - fqe_dt.year) * 12 + (p_dt.month - fqe_dt.month))
                        if diff <= 2 and diff < best_diff:
                            best_diff, best_val = diff, val
                    except: pass
                if best_val is not None:
                    q['revEstimate'] = best_val
            except:
                pass

os.makedirs('data', exist_ok=True)
with open(REV_CACHE_FILE, 'w') as f:
    json.dump(revenue_data, f)
print(f"  Revenue cache saved: {len(revenue_data)} tickers")
with open(REV_EST_CACHE_FILE, 'w') as f:
    json.dump(rev_est_data, f)
print(f"  Rev estimate cache saved: {len(rev_est_data)} tickers")
with open(EPS_EST_CACHE_FILE, 'w') as f:
    json.dump(eps_est_data, f)
print(f"  EPS estimate cache saved: {len(eps_est_data)} tickers")
with open(FMP_EST_CACHE_FILE, 'w') as f:
    json.dump(fmp_est_data, f)
print(f"  Finnhub estimate cache saved: {len(fmp_est_data)} tickers")


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
            eps_fc = r.get('epsForecast', '')
            if not eps_fc and sym in eps_est_data:
                est = eps_est_data[sym]
                v = est.get('0q') or (list(est.values())[0] if est else None)
                if v is not None:
                    eps_fc = str(round(float(v), 2))
            stock_meta[sym] = {
                'name': r.get('name', ''),
                'when': tl,
                'eps':  eps_fc,
                'q':    r.get('fiscalQuarterEnding', ''),
                'date': date_str,
            }


prices = {}  # prices removed from page

# Save mktcap cache
try:
    with open(mktcap_cache_path, 'w') as _f: json.dump(mktcap_cache, _f)
    print(f"Saved mktcap_cache: {len(mktcap_cache)} tickers")
except Exception as e:
    print(f"WARN mktcap cache save: {e}")

# ── 5. Serialize & write ──────────────────────────────────────────────────────
built_at = datetime.now(EASTERN).strftime('%b %d, %Y at %-I:%M %p ET')

with open('template.html', 'r') as f:
    template = f.read()

output = (template
    .replace('__PAST_EARNINGS_JS__', json.dumps(past_earnings, ensure_ascii=False))
    .replace('__EARNINGS_JS__', json.dumps(earnings,   ensure_ascii=False))
    .replace('__HISTORY_JS__',  json.dumps(history,    ensure_ascii=False))
    .replace('__REVENUE_JS__',  json.dumps(revenue_data, ensure_ascii=False))
    .replace('__REV_EST_JS__', json.dumps(rev_est_data,  ensure_ascii=False))
    .replace('__EPS_EST_JS__', json.dumps(eps_est_data,  ensure_ascii=False))
    .replace('__NEWS_JS__',     json.dumps(news,       ensure_ascii=False))
    .replace('__META_JS__',     json.dumps(stock_meta, ensure_ascii=False))
    .replace('__MKTCAP_JS__',    json.dumps(mktcap_cache, ensure_ascii=False))
    .replace('__BUILT_AT__',    json.dumps(built_at)))

with open('docs/index.html', 'w') as f:
    f.write(output)

print(f"\nBuild complete: {total_companies} companies, {len(history)} with history, {len(news)} with news")
print(f"Built at: {built_at}")
