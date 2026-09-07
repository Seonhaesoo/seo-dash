"""구글 서치콘솔 + GA4 수집기 — 서비스 계정 키(key.json)로 접근 가능한 속성을 모두 찾아 SQLite에 넣는다.
사용: python collector.py            (마지막 수집일 이후를 채움, 처음이면 16개월)
      python collector.py --full     (전체 다시 받기)
서치콘솔 데이터는 보통 2~3일 늦게 확정되므로 최근 4일은 매번 다시 받는다."""
import os
import sys
import json
import datetime as dt

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

from db import tx, BASE

KEY_PATH = os.environ.get('SEO_DASH_KEY', os.path.join(BASE, 'data', 'key.json'))
SCOPES = ['https://www.googleapis.com/auth/webmasters.readonly', 'https://www.googleapis.com/auth/analytics.readonly']
GSC = 'https://www.googleapis.com/webmasters/v3'
GA_DATA = 'https://analyticsdata.googleapis.com/v1beta'
GA_ADMIN = 'https://analyticsadmin.googleapis.com/v1beta'
FIRST_RUN_DAYS = 480      # 서치콘솔은 최대 16개월
GA_FIRST_RUN_DAYS = 400
RELOAD_DAYS = 4


def today_kst():
    return (dt.datetime.utcnow() + dt.timedelta(hours=9)).date()


def session():
    if not os.path.exists(KEY_PATH):
        raise SystemExit('key.json 이 없습니다: ' + KEY_PATH)
    creds = service_account.Credentials.from_service_account_file(KEY_PATH, scopes=SCOPES)
    return AuthorizedSession(creds)


def log(msg):
    print(dt.datetime.now().strftime('%H:%M:%S'), msg, flush=True)


# ---------- 서치콘솔 ----------
def gsc_sites(s):
    r = s.get(GSC + '/sites', timeout=60)
    r.raise_for_status()
    out = []
    for e in r.json().get('siteEntry', []):
        if e.get('permissionLevel') in ('siteUnverifiedUser',):
            continue
        out.append(e['siteUrl'])
    return sorted(out)


def gsc_query(s, site, body):
    url = f"{GSC}/sites/{requests.utils.quote(site, safe='')}/searchAnalytics/query"
    r = s.post(url, json=body, timeout=120)
    if r.status_code == 403:
        log(f'  권한 없음: {site}')
        return []
    r.raise_for_status()
    return r.json().get('rows', [])


def sync_gsc(s, con, full=False):
    sites = gsc_sites(s)
    log(f'서치콘솔 속성 {len(sites)}개: ' + ', '.join(sites))
    end = today_kst()
    run_date = end.isoformat()
    for site in sites:
        row = con.execute('SELECT MAX(date) d FROM gsc_daily WHERE site=?', (site,)).fetchone()
        if full or not row['d']:
            start = end - dt.timedelta(days=FIRST_RUN_DAYS)
        else:
            start = dt.date.fromisoformat(row['d']) - dt.timedelta(days=RELOAD_DAYS)
        rows = gsc_query(s, site, {'startDate': start.isoformat(), 'endDate': end.isoformat(), 'dimensions': ['date'], 'rowLimit': 1000})
        for x in rows:
            con.execute('INSERT OR REPLACE INTO gsc_daily VALUES (?,?,?,?,?,?)', (site, x['keys'][0], int(x['clicks']), int(x['impressions']), x['ctr'], x['position']))
        log(f'  {site}: 일별 {len(rows)}행 ({start}~{end})')
        # 최근 7일 / 28일 검색어·페이지 상위
        for window, days in (('7d', 7), ('28d', 28)):
            ws = (end - dt.timedelta(days=days + 2)).isoformat()   # 확정 지연 감안
            we = (end - dt.timedelta(days=2)).isoformat()
            for dim, table in (('query', 'gsc_query'), ('page', 'gsc_page')):
                rows = gsc_query(s, site, {'startDate': ws, 'endDate': we, 'dimensions': [dim], 'rowLimit': 500})
                con.execute(f'DELETE FROM {table} WHERE site=? AND run_date=? AND window=?', (site, run_date, window))
                for x in rows:
                    con.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?)', (site, run_date, window, x['keys'][0], int(x['clicks']), int(x['impressions']), x['position']))
        # 사이트맵
        r = s.get(f"{GSC}/sites/{requests.utils.quote(site, safe='')}/sitemaps", timeout=60)
        if r.ok:
            for sm in r.json().get('sitemap', []):
                sub = sum(int(c.get('submitted', 0)) for c in sm.get('contents', []))
                idx = sum(int(c.get('indexed', 0)) for c in sm.get('contents', []))
                con.execute('INSERT OR REPLACE INTO gsc_sitemap VALUES (?,?,?,?,?,?,?,?)', (site, run_date, sm.get('path'), sub, idx, sm.get('lastDownloaded'), int(sm.get('errors', 0)), int(sm.get('warnings', 0))))
    con.commit()
    return sites


# ---------- GA4 ----------
def ga_properties(s):
    r = s.get(GA_ADMIN + '/accountSummaries', params={'pageSize': 200}, timeout=60)
    if not r.ok:
        log('GA4 속성 목록 실패: ' + r.text[:200])
        return []
    out = []
    for acc in r.json().get('accountSummaries', []):
        for p in acc.get('propertySummaries', []):
            out.append((p['property'].split('/')[-1], p.get('displayName', '')))
    return out


def ga_report(s, pid, body):
    r = s.post(f'{GA_DATA}/properties/{pid}:runReport', json=body, timeout=120)
    if not r.ok:
        log(f'  GA4 {pid} 실패: {r.text[:200]}')
        return []
    return r.json().get('rows', [])


def sync_ga(s, con, full=False):
    props = ga_properties(s)
    log(f'GA4 속성 {len(props)}개: ' + ', '.join(f'{n}({p})' for p, n in props))
    end = today_kst()
    run_date = end.isoformat()
    for pid, name in props:
        row = con.execute('SELECT MAX(date) d FROM ga_daily WHERE property=?', (pid,)).fetchone()
        if full or not row['d']:
            start = end - dt.timedelta(days=GA_FIRST_RUN_DAYS)
        else:
            start = dt.date.fromisoformat(row['d']) - dt.timedelta(days=2)
        rows = ga_report(s, pid, {'dateRanges': [{'startDate': start.isoformat(), 'endDate': 'today'}], 'dimensions': [{'name': 'date'}], 'metrics': [{'name': 'activeUsers'}, {'name': 'screenPageViews'}, {'name': 'sessions'}], 'limit': 1000})
        for x in rows:
            d = x['dimensionValues'][0]['value']
            d = f'{d[:4]}-{d[4:6]}-{d[6:]}'
            m = [int(float(v['value'])) for v in x['metricValues']]
            con.execute('INSERT OR REPLACE INTO ga_daily VALUES (?,?,?,?,?,?)', (pid, name, d, m[0], m[1], m[2]))
        log(f'  {name}: 일별 {len(rows)}행')
        rows = ga_report(s, pid, {'dateRanges': [{'startDate': '7daysAgo', 'endDate': 'today'}], 'dimensions': [{'name': 'pagePath'}], 'metrics': [{'name': 'screenPageViews'}, {'name': 'activeUsers'}], 'limit': 300, 'orderBys': [{'metric': {'metricName': 'screenPageViews'}, 'desc': True}]})
        con.execute('DELETE FROM ga_page WHERE property=? AND run_date=?', (pid, run_date))
        for x in rows:
            con.execute('INSERT OR REPLACE INTO ga_page VALUES (?,?,?,?,?)', (pid, run_date, x['dimensionValues'][0]['value'], int(float(x['metricValues'][0]['value'])), int(float(x['metricValues'][1]['value']))))
        rows = ga_report(s, pid, {'dateRanges': [{'startDate': '7daysAgo', 'endDate': 'today'}], 'dimensions': [{'name': 'sessionSource'}], 'metrics': [{'name': 'sessions'}], 'limit': 30, 'orderBys': [{'metric': {'metricName': 'sessions'}, 'desc': True}]})
        con.execute('DELETE FROM ga_source WHERE property=? AND run_date=?', (pid, run_date))
        for x in rows:
            con.execute('INSERT OR REPLACE INTO ga_source VALUES (?,?,?,?)', (pid, run_date, x['dimensionValues'][0]['value'], int(float(x['metricValues'][0]['value']))))
    con.commit()
    return props


def run(full=False):
    started = dt.datetime.now().isoformat(timespec='seconds')
    if not os.path.exists(KEY_PATH):
        log('key.json 이 없어 건너뜀: ' + KEY_PATH)
        return False
    with tx() as con:
        con.execute("UPDATE sync_log SET status='error', finished=?, message='중단됨 (재시작)' WHERE status='running'", (started,))
        cur = con.execute('INSERT INTO sync_log (started, status, message) VALUES (?,?,?)', (started, 'running', ''))
        log_id = cur.lastrowid
        con.commit()
    status, message = 'ok', ''
    try:
        s = session()
        with tx() as con:
            sites = sync_gsc(s, con, full)
            props = sync_ga(s, con, full)
        message = f'서치콘솔 {len(sites)}개 · GA4 {len(props)}개'
    except BaseException as e:   # noqa — SystemExit 포함
        status, message = 'error', f'{type(e).__name__}: {e}'[:500]
        log('오류: ' + message)
    with tx() as con:
        con.execute('UPDATE sync_log SET finished=?, status=?, message=? WHERE id=?', (dt.datetime.now().isoformat(timespec='seconds'), status, message, log_id))
    log(f'동기화 {status} — {message}')
    return status == 'ok'


if __name__ == '__main__':
    ok = run(full='--full' in sys.argv)
    sys.exit(0 if ok else 1)
