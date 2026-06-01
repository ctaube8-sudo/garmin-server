"""
Garmin Connect proxy server for life-maxxing PWA.
Deployed on Render — reads session tokens from GARMIN_TOKENS env var.

Setup: run get_tokens.py locally once, paste output into Render as GARMIN_TOKENS.
"""

import base64
import json
import os
import re
import traceback
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta, datetime

import garth
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Credentials from env vars ──────────────────────────────────────────────────
GARMIN_TOKENS       = ''.join(os.environ.get('GARMIN_TOKENS', '').split())  # strip ALL whitespace incl. embedded newlines
GARMIN_EMAIL        = os.environ.get('GARMIN_EMAIL', '')
GARMIN_PASSWORD     = os.environ.get('GARMIN_PASSWORD', '')
GARMIN_DISPLAY_NAME = os.environ.get('GARMIN_DISPLAY_NAME', '')
DAYS                = int(os.environ.get('GARMIN_DAYS', '14'))

FATSECRET_CLIENT_ID     = os.environ.get('FATSECRET_CLIENT_ID', '')
FATSECRET_CLIENT_SECRET = os.environ.get('FATSECRET_CLIENT_SECRET', '')

REFRESH_INTERVAL = 60 * 60  # 1 hour — Garmin API rate limits on frequent calls
CACHE_FILE       = '/tmp/garmin_cache.json'

# ── In-memory cache (pre-populated from disk at startup) ──────────────────────
def _init_cache():
    c = {'data': None, 'synced_at': None, 'display_name': None,
         'last_error': None, 'lock': threading.Lock()}
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                d = json.load(f)
            c['data']      = d
            c['synced_at'] = d.get('synced_at')
            print(f'[garmin] Pre-loaded disk cache (synced_at: {c["synced_at"]}).')
    except Exception as e:
        print(f'[garmin] Pre-load failed: {e}')
    return c

_cache = _init_cache()


# ── Disk persistence ───────────────────────────────────────────────────────────

def save_to_disk(payload):
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(payload, f)
        print('[garmin] Saved data to disk.')
    except Exception as e:
        print(f'[garmin] Disk save failed: {e}')


def load_from_disk():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                payload = json.load(f)
            print(f'[garmin] Loaded cached data from disk (synced_at: {payload.get("synced_at")}).')
            return payload
    except Exception as e:
        print(f'[garmin] Disk load failed: {e}')
    return None


# ── Auth ───────────────────────────────────────────────────────────────────────

def init_garth():
    """Load tokens from env var (preferred) or fall back to password auth."""
    if GARMIN_TOKENS:
        # Fix base64 padding — Render may strip trailing '=' characters
        token = GARMIN_TOKENS
        token += '=' * (-len(token) % 4)
        garth.client.loads(token)
        print('[garmin] Loaded session from GARMIN_TOKENS.')
    elif GARMIN_EMAIL and GARMIN_PASSWORD:
        print(f'[garmin] Logging in as {GARMIN_EMAIL}...')
        garth.login(GARMIN_EMAIL, GARMIN_PASSWORD)
        print('[garmin] Logged in.')
    else:
        raise RuntimeError('Set GARMIN_TOKENS (or GARMIN_EMAIL + GARMIN_PASSWORD) env vars.')

    if GARMIN_DISPLAY_NAME:
        print(f'[garmin] Using display name from env: {GARMIN_DISPLAY_NAME}')
        return GARMIN_DISPLAY_NAME

    profile = garth.connectapi('/userprofile-service/socialProfile')
    display_name = profile.get('displayName') or profile.get('userName')
    print(f'[garmin] Display name: {display_name}')
    return display_name


# ── Garmin API helpers ─────────────────────────────────────────────────────────

def safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def pull_day(display_name, date_str):
    day = {'date': date_str}

    try:
        sleep = garth.connectapi(
            f'/wellness-service/wellness/dailySleepData/{display_name}',
            params={'date': date_str}
        )
        summary = safe_get(sleep, 'dailySleepDTO') or {}
        day['sleep_hours']     = round((summary.get('sleepTimeSeconds') or 0) / 3600, 2)
        day['sleep_score']     = (summary.get('sleepScores', {}).get('overall', {}).get('value')
                                  or summary.get('sleepScore'))
        day['deep_sleep_min']  = round((summary.get('deepSleepSeconds')  or 0) / 60)
        day['rem_sleep_min']   = round((summary.get('remSleepSeconds')   or 0) / 60)
        day['light_sleep_min'] = round((summary.get('lightSleepSeconds') or 0) / 60)
        day['awake_min']       = round((summary.get('awakeSleepSeconds') or 0) / 60)
    except Exception as e:
        print(f'  [sleep] {date_str}: {e}')
        day.update({'sleep_hours': None, 'sleep_score': None, 'deep_sleep_min': None,
                    'rem_sleep_min': None, 'light_sleep_min': None, 'awake_min': None})

    try:
        hrv_data = garth.connectapi(f'/hrv-service/hrv/{date_str}')
        day['hrv'] = safe_get(hrv_data, 'hrvSummary', 'lastNight')
    except Exception as e:
        print(f'  [hrv] {date_str}: {e}')
        day['hrv'] = None

    try:
        stats = garth.connectapi(
            f'/usersummary-service/usersummary/daily/{display_name}',
            params={'calendarDate': date_str}
        )
        day['steps']           = stats.get('totalSteps')
        day['resting_hr']      = stats.get('restingHeartRate')
        day['avg_stress']      = stats.get('averageStressLevel')
        day['active_calories'] = stats.get('activeKilocalories')
        day['total_calories']  = stats.get('totalKilocalories')
    except Exception as e:
        print(f'  [stats] {date_str}: {e}')
        day.update({'steps': None, 'resting_hr': None, 'avg_stress': None,
                    'active_calories': None, 'total_calories': None})

    try:
        bb = garth.connectapi(
            '/wellness-service/wellness/bodyBattery/graphs',
            params={'startDate': date_str, 'endDate': date_str}
        )
        if bb and isinstance(bb, list) and bb[0].get('bodyBatteryValuesArray'):
            vals = bb[0]['bodyBatteryValuesArray']
            day['body_battery_start'] = vals[0][1]  if vals else None
            day['body_battery_end']   = vals[-1][1] if vals else None
        else:
            day['body_battery_start'] = None
            day['body_battery_end']   = None
    except Exception as e:
        print(f'  [body battery] {date_str}: {e}')
        day['body_battery_start'] = None
        day['body_battery_end']   = None

    return day


def run_sync():
    with _cache['lock']:
        display_name = _cache['display_name']

    if not display_name:
        raise RuntimeError('Not authenticated yet.')

    print(f'[garmin] Syncing {DAYS} days for {display_name}...')
    today_date = date.today()
    days_data = []
    for i in range(DAYS - 1, -1, -1):
        d = today_date - timedelta(days=i)
        date_str = d.isoformat()
        print(f'  Pulling {date_str}...')
        days_data.append(pull_day(display_name, date_str))

    payload = {
        'synced_at': datetime.now().isoformat(timespec='seconds'),
        'days': days_data,
    }
    print(f'[garmin] Sync complete — {len(days_data)} days.')
    return payload


# ── FatSecret food search ──────────────────────────────────────────────────────

_fs_token = {'access_token': None, 'expires_at': 0, 'lock': threading.Lock()}


def get_fatsecret_token():
    with _fs_token['lock']:
        if _fs_token['access_token'] and time.time() < _fs_token['expires_at'] - 60:
            return _fs_token['access_token']

    creds = base64.b64encode(f"{FATSECRET_CLIENT_ID}:{FATSECRET_CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        'https://oauth.fatsecret.com/connect/token',
        data=b'grant_type=client_credentials&scope=basic',
        headers={
            'Authorization': f'Basic {creds}',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        tok = json.loads(r.read())

    with _fs_token['lock']:
        _fs_token['access_token'] = tok['access_token']
        _fs_token['expires_at'] = time.time() + tok.get('expires_in', 86400)

    return tok['access_token']


def parse_fs_desc(desc):
    """Parse FatSecret description: 'Per 100g - Calories: 165kcal | Fat: 3.57g | Carbs: 0g | Protein: 31g'"""
    per_m = re.match(r'^Per\s+(.+?)\s*-', desc)
    per = per_m.group(1).strip() if per_m else ''

    def ex(label):
        m = re.search(rf'{label}:\s*([\d.]+)', desc, re.IGNORECASE)
        return round(float(m.group(1))) if m else 0

    return {'per': per, 'cal': ex('Calories'), 'protein': ex('Protein'),
            'carbs': ex('Carbs'), 'fat': ex('Fat')}


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route('/status')
def status():
    with _cache['lock']:
        return jsonify({
            'status':       'ok',
            'synced_at':    _cache['synced_at'],
            'has_data':     _cache['data'] is not None,
            'display_name': _cache['display_name'],
            'last_error':   _cache['last_error'],
        })


@app.route('/debug')
def debug():
    error = None
    display_name = None
    try:
        garth.client.loads(GARMIN_TOKENS)
        display_name = GARMIN_DISPLAY_NAME or 'not set'
        if not GARMIN_DISPLAY_NAME:
            profile = garth.connectapi('/userprofile-service/socialProfile')
            display_name = profile.get('displayName') or profile.get('userName')
    except Exception as e:
        error = traceback.format_exc()
    return jsonify({'display_name': display_name, 'error': error})


@app.route('/data')
def data():
    with _cache['lock']:
        payload = _cache['data']
    if payload is None:
        return jsonify({'error': 'Not synced yet'}), 503
    return jsonify(payload)


@app.route('/sync', methods=['POST'])
def sync():
    try:
        payload = run_sync()
        with _cache['lock']:
            _cache['data']      = payload
            _cache['synced_at'] = payload['synced_at']
        save_to_disk(payload)
        return jsonify(payload)
    except Exception as e:
        print(f'[garmin] Sync error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/today')
def today_endpoint():
    with _cache['lock']:
        payload = _cache['data']
    if payload is None:
        return jsonify({'error': 'Not synced yet'}), 503
    today_str = date.today().isoformat()
    day = next((d for d in payload['days'] if d['date'] == today_str), None)
    if day is None and payload['days']:
        day = payload['days'][-1]
    return jsonify({**day, 'synced_at': payload['synced_at']})


@app.route('/today-sync', methods=['POST'])
def today_sync():
    """Light sync — only pulls today + yesterday (~10 API calls vs 60 for full sync)."""
    with _cache['lock']:
        display_name = _cache['display_name']
        payload = _cache['data']

    if not display_name:
        return jsonify({'error': 'Not authenticated yet'}), 503

    try:
        today_str     = date.today().isoformat()
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()

        today_day     = pull_day(display_name, today_str)
        yesterday_day = pull_day(display_name, yesterday_str)

        with _cache['lock']:
            if _cache['data'] and _cache['data'].get('days'):
                days = _cache['data']['days']
                # Replace or append today + yesterday
                for new_day in [yesterday_day, today_day]:
                    idx = next((i for i, d in enumerate(days) if d['date'] == new_day['date']), None)
                    if idx is not None:
                        days[idx] = new_day
                    else:
                        days.append(new_day)
                days.sort(key=lambda d: d['date'])
                _cache['data']['synced_at'] = datetime.now().isoformat(timespec='seconds')
                payload = _cache['data']
            else:
                return jsonify({'error': 'No base data yet — run full sync first'}), 503

        save_to_disk(payload)
        print(f'[garmin] Today-sync complete.')
        return jsonify(payload)
    except Exception as e:
        print(f'[garmin] Today-sync error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/food/search')
def food_search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Missing q parameter'}), 400
    if not FATSECRET_CLIENT_ID or not FATSECRET_CLIENT_SECRET:
        return jsonify({'error': 'FatSecret credentials not configured'}), 500
    try:
        token = get_fatsecret_token()
        url = (
            'https://platform.fatsecret.com/rest/server.api'
            f'?method=foods.search'
            f'&search_expression={urllib.parse.quote(query, safe="")}'
            f'&format=json&max_results=8'
        )
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        raw = data.get('foods', {}).get('food', [])
        if isinstance(raw, dict):
            raw = [raw]  # single result comes back as dict, not list

        results = []
        for f in raw:
            macros = parse_fs_desc(f.get('food_description', ''))
            results.append({
                'id':    f.get('food_id'),
                'name':  f.get('food_name', ''),
                'brand': f.get('brand_name', ''),
                'type':  f.get('food_type', ''),
                **macros,
            })

        return jsonify({'results': results})
    except Exception as e:
        print(f'[fatsecret] Search error: {e}')
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/stock/<ticker>')
def stock_price(ticker):
    ticker = ticker.upper().strip()

    # Try Yahoo Finance (query1 then query2)
    for host in ['query1', 'query2']:
        url = f'https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d'
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json',
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            price = data['chart']['result'][0]['meta']['regularMarketPrice']
            return jsonify({'ticker': ticker, 'price': float(price), 'source': host})
        except Exception as e:
            print(f'[stock] {host} failed for {ticker}: {e}')

    # Fallback: Stooq (no API key needed)
    try:
        stooq_url = f'https://stooq.com/q/l/?s={ticker}.us&f=sd2t2ohlcv&h&e=json'
        req = urllib.request.Request(stooq_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        symbols = data.get('symbols', [])
        if symbols and symbols[0].get('Close') not in (None, 'N/D'):
            return jsonify({'ticker': ticker, 'price': float(symbols[0]['Close']), 'source': 'stooq'})
    except Exception as e:
        print(f'[stock] stooq failed for {ticker}: {e}')

    return jsonify({'error': f'Could not fetch price for {ticker}'}), 500


# ── Background refresh ─────────────────────────────────────────────────────────

def background_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
        print('[garmin] Auto-refresh triggered.')
        try:
            payload = run_sync()
            with _cache['lock']:
                _cache['data']      = payload
                _cache['synced_at'] = payload['synced_at']
            save_to_disk(payload)
        except Exception as e:
            print(f'[garmin] Auto-refresh failed: {e}')


def do_startup():
    """Runs synchronously — blocks until first sync completes."""
    try:
        display_name = init_garth()
        with _cache['lock']:
            _cache['display_name'] = display_name
        payload = run_sync()
        with _cache['lock']:
            _cache['data']      = payload
            _cache['synced_at'] = payload['synced_at']
            _cache['last_error'] = None
        save_to_disk(payload)
        print('[garmin] Initial sync done.')
    except Exception as e:
        print(f'[garmin] Startup failed: {e}')
        traceback.print_exc()
        with _cache['lock']:
            _cache['last_error'] = str(e)


# ── Start ─────────────────────────────────────────────────────────────────────

# Run startup sync in background so Flask binds to PORT immediately.
# /status returns has_data: false while sync is in progress (expected).
threading.Thread(target=do_startup, daemon=True).start()
threading.Thread(target=background_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
