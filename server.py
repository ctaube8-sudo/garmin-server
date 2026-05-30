"""
Garmin Connect proxy server for life-maxxing PWA.
Deployed on Render — reads session tokens from GARMIN_TOKENS env var.

Setup: run get_tokens.py locally once, paste output into Render as GARMIN_TOKENS.
"""

import json
import os
import traceback
import threading
import time
from datetime import date, timedelta, datetime

import garth
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Credentials from env vars ──────────────────────────────────────────────────
GARMIN_TOKENS       = os.environ.get('GARMIN_TOKENS', '')
GARMIN_EMAIL        = os.environ.get('GARMIN_EMAIL', '')
GARMIN_PASSWORD     = os.environ.get('GARMIN_PASSWORD', '')
GARMIN_DISPLAY_NAME = os.environ.get('GARMIN_DISPLAY_NAME', '')
DAYS                = int(os.environ.get('GARMIN_DAYS', '14'))

REFRESH_INTERVAL = 30 * 60  # 30 minutes
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
        garth.client.loads(GARMIN_TOKENS)
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


def startup_sync():
    def _go():
        try:
            # Load stale data from disk immediately so has_data is true right away
            cached = load_from_disk()
            if cached:
                with _cache['lock']:
                    _cache['data']      = cached
                    _cache['synced_at'] = cached.get('synced_at')

            # Auth
            display_name = init_garth()
            with _cache['lock']:
                _cache['display_name'] = display_name

            # Fresh sync
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
    threading.Thread(target=_go, daemon=True).start()


# ── Start on import (gunicorn-safe) ───────────────────────────────────────────

startup_sync()
threading.Thread(target=background_loop, daemon=True).start()
