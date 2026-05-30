"""
Garmin Connect proxy server for life-maxxing PWA.
Deployed on Railway — reads credentials from environment variables.
"""

import json
import os
import threading
import time
from datetime import date, timedelta, datetime

from flask import Flask, jsonify
from flask_cors import CORS
from garminconnect import Garmin

app = Flask(__name__)
CORS(app)

# ── Credentials from env vars (set in Railway dashboard) ──────────────────────
GARMIN_EMAIL    = os.environ.get('GARMIN_EMAIL', '')
GARMIN_PASSWORD = os.environ.get('GARMIN_PASSWORD', '')
DAYS            = int(os.environ.get('GARMIN_DAYS', '14'))

# ── In-memory cache ────────────────────────────────────────────────────────────
_cache = {
    'data':      None,
    'synced_at': None,
    'lock':      threading.Lock(),
}

REFRESH_INTERVAL = 30 * 60  # 30 minutes


# ── Garmin helpers ─────────────────────────────────────────────────────────────

def safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def pull_day(api, date_str):
    day = {'date': date_str}

    try:
        sleep = api.get_sleep_data(date_str)
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
        hrv_data = api.get_hrv_data(date_str)
        day['hrv'] = safe_get(hrv_data, 'hrvSummary', 'lastNight')
    except Exception as e:
        print(f'  [hrv] {date_str}: {e}')
        day['hrv'] = None

    try:
        stats = api.get_stats(date_str)
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
        bb = api.get_body_battery(date_str)
        if bb and isinstance(bb, list):
            day['body_battery_start'] = bb[0].get('bodyBatteryLevel') if bb else None
            day['body_battery_end']   = bb[-1].get('bodyBatteryLevel') if bb else None
        else:
            day['body_battery_start'] = None
            day['body_battery_end']   = None
    except Exception as e:
        print(f'  [body battery] {date_str}: {e}')
        day['body_battery_start'] = None
        day['body_battery_end']   = None

    return day


def run_sync():
    print(f'[garmin] Syncing {DAYS} days as {GARMIN_EMAIL}...')
    api = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    api.login()
    print('[garmin] Logged in.')

    today_date = date.today()
    days_data = []
    for i in range(DAYS - 1, -1, -1):
        d = today_date - timedelta(days=i)
        date_str = d.isoformat()
        print(f'  Pulling {date_str}...')
        days_data.append(pull_day(api, date_str))

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
            'status':    'ok',
            'synced_at': _cache['synced_at'],
            'has_data':  _cache['data'] is not None,
        })


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
        except Exception as e:
            print(f'[garmin] Auto-refresh failed: {e}')


def startup_sync():
    def _go():
        try:
            payload = run_sync()
            with _cache['lock']:
                _cache['data']      = payload
                _cache['synced_at'] = payload['synced_at']
            print('[garmin] Initial sync done.')
        except Exception as e:
            print(f'[garmin] Initial sync failed: {e}')
    threading.Thread(target=_go, daemon=True).start()


# ── Start background threads on import (gunicorn-safe) ────────────────────────

if GARMIN_EMAIL and GARMIN_PASSWORD:
    startup_sync()
    threading.Thread(target=background_loop, daemon=True).start()
else:
    print('[garmin] WARNING: GARMIN_EMAIL / GARMIN_PASSWORD env vars not set.')
