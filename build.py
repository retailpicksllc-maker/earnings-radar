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

# ── Calendar data: worker API is the SINGLE source ───────────────────────────
WORKER_BASE = "https://captivating-creation-production-3d49.up.railway.app"
MIN_CAP_MUSD = 500  # $500M floor (worker applies this server-side)

def fetch_worker_calendar(frm, to, min_cap=MIN_CAP_MUSD):
    url = f"{WORKER_BASE}/v1/calendar?from={frm}&to={to}&min_cap={min_cap}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        if isinstance(data, list):
            return data
        return data.get('data') or data.get('rows') or data.get('events') or []
    except Exception as e:
        print(f"  ERR worker calendar {frm}..{to}: {e}")
        return []

def _worker_fqe(e):
    fy = e.get('fiscal_year')
    fq = e.get('fiscal_quarter', '') or ''
    return f"{fq}/{str(fy)[2:]}" if (fy and fq) else fq

def _rev_musd(v):
    """Worker revenue is raw USD; convert to $millions for the UI formatter."""
    try:
        return round(float(v) / 1e6, 1) if v is not None else None
    except Exception:
        return None

def map_worker_row(e):
    rt = (e.get('report_time') or 'unknown').lower()
    # If the coarse report_time is unknown, derive session from the richer
    # expected_time field ("before open" / "after close" / "16:15 ET").
    if rt not in ('bmo', 'amc'):
        et = (e.get('expected_time') or '').lower()
        if 'before' in et or 'pre' in et:
            rt = 'bmo'
        elif 'after' in et or 'post' in et:
            rt = 'amc'
        else:
            _m = re.search(r'(\d{1,2}):(\d{2})', et)
            if _m:
                rt = 'bmo' if int(_m.group(1)) < 12 else 'amc'
    time_val = ('time-pre-market' if rt == 'bmo'
                else 'time-after-hours' if rt == 'amc'
                else 'time-not-supplied')
    cap = e.get('market_cap_musd') or 0
    try:
        cap_str = f"${int(float(cap) * 1_000_000):,}" if cap else ''
    except Exception:
        cap_str = ''
    return {
        'symbol': e.get('ticker', ''),
        'time': time_val,
        'fiscalQuarterEnding': _worker_fqe(e),
        'eps': e.get('eps_est'),
        'epsActual': e.get('eps_act'),
        # Worker sends revenue as raw USD; the UI formatter expects $millions.
        'revenueEstimate': _rev_musd(e.get('rev_est')),
        'revenueActual': _rev_musd(e.get('rev_act')),
        'marketCap': cap_str,
        'name': e.get('ticker', ''),
        'status': e.get('status', ''),
        # Expected report time (worker learned clock time), styled by confidence
        'expectedTime': e.get('expected_time') or '',
        'expectedTimeConf': (e.get('expected_time_confidence') or 'unknown'),
        'verified': e.get('verified', True),
        'divergence': bool(e.get('divergence_flag')),
    }

_win_from = (today - timedelta(days=45)).strftime('%Y-%m-%d')
_win_to   = (today + timedelta(days=60)).strftime('%Y-%m-%d')
print(f"Fetching calendar from worker ({_win_from} .. {_win_to}, min_cap={MIN_CAP_MUSD}M)...")
_events = fetch_worker_calendar(_win_from, _win_to)
print(f"  Worker returned {len(_events)} events")

earnings = {}
past_earnings = {}
for _e in _events:
    _d = _e.get('report_date', '')
    if not _d or not _e.get('ticker'):
        continue
    _row = map_worker_row(_e)
    (earnings if _d >= today_str else past_earnings).setdefault(_d, []).append(_row)

def _mc_sort(r):
    try:
        return float(str(r.get('marketCap', '')).replace('$', '').replace(',', ''))
    except Exception:
        return 0.0
for _cal in (earnings, past_earnings):
    for _d in _cal:
        _cal[_d].sort(key=_mc_sort, reverse=True)

# Stubs so the downstream (history / news / prices / serialize) stays unchanged.
mktcap_cache = {}
mktcap_cache_path = 'data/marketcap_cache.json'

total_companies = sum(len(v) for v in earnings.values())
print(f"  Upcoming: {total_companies} across {len(earnings)} days | "
      f"Past: {sum(len(v) for v in past_earnings.values())} across {len(past_earnings)} days")

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
    if len(top_tickers) >= 250:   # cap Priority 1 so history/news stay lean
        break
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
FMP_EST_CACHE_FILE   = 'data/fmp_est_cache.json'
FMP_INC_CACHE_FILE  = 'data/fmp_income_cache.json'
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
fmp_income_cache = {}
try:
    with open(FMP_INC_CACHE_FILE) as _f: fmp_income_cache = json.load(_f)
    print(f"  Loaded FMP income cache: {len(fmp_income_cache)} tickers")
except: pass

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

def _sec_quarterly(ticker):
    """Fetch quarterly revenue from SEC EDGAR 10-Q filings — completely free."""
    cik = _cik_map.get(ticker)
    if not cik: return {}
    try:
        url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
        req = urllib.request.Request(url, headers={'User-Agent': 'retail.picksllc@gmail.com'})
        facts = json.loads(urllib.request.urlopen(req, timeout=20).read())
        result = {}
        for taxonomy in ['us-gaap', 'ifrs-full']:
            tax = facts.get('facts', {}).get(taxonomy, {})
            for field in ['Revenues', 'Revenue',
                          'RevenueFromContractWithCustomerExcludingAssessedTax',
                          'SalesRevenueNet', 'NoninterestIncome',
                          'RealEstateRevenueNet', 'RevenueFromContractWithCustomerIncludingAssessedTax']:
                if field not in tax: continue
                for cur, entries in tax[field].get('units', {}).items():
                    fx = _FX.get(cur, 1.0) if cur != 'USD' else 1.0
                    for e in entries:
                        if e.get('form') not in ('10-Q', '10-K', '20-F'): continue
                        val = e.get('val', 0)
                        if not val or val <= 0: continue
                        val_usd = val / fx / 1e6
                        if val_usd < 0.01 or val_usd > 5e6: continue
                        try:
                            start_s = e.get('start', '')
                            end_s = e['end']
                            if not start_s: continue
                            s = datetime.strptime(start_s, '%Y-%m-%d')
                            en = datetime.strptime(end_s, '%Y-%m-%d')
                            days = (en - s).days
                            if 60 <= days <= 105:  # quarterly ~90 days
                                k = en.strftime('%b %Y')
                                if k not in result:
                                    result[k] = round(val_usd, 1)
                        except: continue
                if result: break
            if result: break
        return result
    except: return {}

def _fmp_income(ticker):
    """Kept for backward compat — now just calls SEC quarterly."""
    rev = _sec_quarterly(ticker)
    return rev, {}

def _sec_annual_fallback(ticker):
    return _sec_quarterly(ticker)


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

# Upcoming symbols for cache-bypass logic
upcoming_syms = set(r.get('symbol','') for rows in earnings.values() for r in rows if r.get('symbol'))

# FMP income: fetch rev actuals + eps actuals for tickers not cached
fmp_income_data = dict(fmp_income_cache)
fmp_inc_fetch = [t for t in all_rev_tickers if t not in fmp_income_data or
                 t in upcoming_syms]
# Also fetch for top_tickers not yet in income cache
for sym in top_tickers:
    if sym not in fmp_income_data and sym not in fmp_inc_fetch:
        fmp_inc_fetch.append(sym)
fmp_inc_fetch = fmp_inc_fetch[:600]  # cap per build
print(f"Fetching FMP income statements for {len(fmp_inc_fetch)} tickers...")

def _fetch_one(ticker):
    rev, eps = _fmp_income(ticker)
    return ticker, rev, eps

with ThreadPoolExecutor(max_workers=5) as ex:
    for ticker, rev, eps in ex.map(_fetch_one, fmp_inc_fetch, timeout=300):
        entry = {'rev': rev, 'eps': eps}
        fmp_income_data[ticker] = entry
        if rev:
            revenue_data[ticker] = rev

# Backfill revenue from existing cache for tickers not just fetched
for ticker, entry in fmp_income_data.items():
    if ticker not in revenue_data and entry.get('rev'):
        revenue_data[ticker] = entry['rev']

# Backfill EPS history from FMP income for tickers not covered by Finnhub
for ticker, entry in fmp_income_data.items():
    if ticker not in history and entry.get('eps'):
        eps_by_qtr = entry['eps']  # {qk: eps_val}
        quarters = []
        for qk, eps_val in sorted(eps_by_qtr.items(),
                                  key=lambda x: datetime.strptime(x[0], '%b %Y') if len(x[0])==8 else datetime.min,
                                  reverse=True):
            quarters.append({'fiscalQtrEnd': qk, 'eps': eps_val,
                             'consensusForecast': '', 'percentageSurprise': '',
                             'dateReported': '', 'revActual': eps_by_qtr.get(qk),
                             'revEstimate': None})
        if quarters:
            history[ticker] = quarters

# Save FMP income cache
try:
    with open(FMP_INC_CACHE_FILE, 'w') as _f: json.dump(fmp_income_data, _f)
    print(f"  FMP income cache saved: {len(fmp_income_data)} tickers")
except Exception as e:
    print(f"WARN FMP income cache save: {e}")

rev_est_data = dict(rev_est_cache)
with ThreadPoolExecutor(max_workers=8) as ex:
    for ticker, qtrs in ex.map(lambda t: (t, {}), [], timeout=10):
        pass  # revenue now from FMP income above



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
# Best-effort: never let a slow news feed fail the whole build.
try:
    with ThreadPoolExecutor(max_workers=30) as ex:
        for ticker, items in ex.map(fetch_news, news_tickers, timeout=150):
            if items:
                news[ticker] = items
except Exception as e:
    print(f"  news fetch stopped early ({type(e).__name__}); continuing with {len(news)} collected")
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

# ── 4b. Live price snapshot from worker /v1/quotes (entire board in ONE call) ─
# Client polls /v1/quotes every ~5s for live ticks; this bakes an initial snapshot
# so prices render immediately (and whenever CORS blocks the browser poll).
price_data = {}
_cal_syms = set()
for _cal in (earnings, past_earnings):
    for _rows in _cal.values():
        for _r in _rows:
            if _r.get('symbol'):
                _cal_syms.add(_r['symbol'])
try:
    _qreq = urllib.request.Request(f"{WORKER_BASE}/v1/quotes",
                                   headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(_qreq, timeout=60) as _qr:
        _qdata = json.loads(_qr.read())
    _quotes = _qdata if isinstance(_qdata, list) else (_qdata.get('data') or _qdata.get('quotes') or [])
    for _q in _quotes:
        _sym = _q.get('ticker', '')
        if _sym and _sym in _cal_syms and _q.get('last') is not None:
            try:
                price_data[_sym] = {
                    'c':  round(float(_q['last']), 2),
                    'dp': round(float(_q.get('change_pct') or 0), 2),
                    'pc': round(float(_q.get('prev_close') or 0), 2),
                }
            except Exception:
                pass
    print(f"  Prices from worker /v1/quotes: {len(price_data)} of {len(_cal_syms)} calendar tickers ({len(_quotes)} on board)")
except Exception as e:
    print(f"  WARN worker quotes fetch: {e}")

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
    .replace('__PRICES_JS__',     json.dumps(price_data, ensure_ascii=False))
    .replace('__MKTCAP_JS__',    json.dumps(mktcap_cache, ensure_ascii=False))
    .replace('__FH_KEY_JS__',   json.dumps(FINNHUB_KEY, ensure_ascii=False))
    .replace('__BUILT_AT__',    json.dumps(built_at)))

with open('docs/index.html', 'w') as f:
    f.write(output)

print(f"\nBuild complete: {total_companies} companies, {len(history)} with history, {len(news)} with news")
print(f"Built at: {built_at}")
