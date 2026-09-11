"""구글 서치콘솔 + GA4 수집기 — 서비스 계정 키(key.json)로 접근 가능한 속성을 모두 찾아 SQLite에 넣는다.
사용: python collector.py            (마지막 수집일 이후를 채움, 처음이면 16개월)
      python collector.py --full     (전체 다시 받기)
서치콘솔 데이터는 보통 2~3일 늦게 확정되므로 최근 4일은 매번 다시 받는다.
도메인 속성(sc-domain:)이 같은 뿌리의 URL 속성과 겹치면(사주첩처럼 하위 사이트를 묶는 속성) 따로 https 속성이 없는
하위 사이트를 페이지 주소로 나눠 '속성#하위주소' 이름으로도 받는다 — 대시보드는 묶음 카드 대신 이것을 보여 준다."""
import os
import re
import sys
import json
import datetime as dt
from urllib.parse import urlsplit

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


def host_of(url):
    """주소의 호스트 — www 는 떼고 비교"""
    h = (urlsplit(url).netloc if '://' in url else url).lower()
    return h[4:] if h.startswith('www.') else h


def page_filter(host):
    """도메인 속성에서 하위 사이트 하나만 — 페이지 주소가 http(s)://(www.)host/ 로 시작"""
    return [{'filters': [{'dimension': 'page', 'operator': 'includingRegex', 'expression': r'^https?://(www\.)?' + re.escape(host) + '/'}]}]


def split_hosts(con, site, sites, run_date):
    """도메인 속성 site 가 같은 뿌리의 URL 속성과 겹치면, 따로 https 속성이 없는 하위 사이트 목록.
    하위 사이트는 이번 수집에서 이 속성에 잡힌 사이트맵 주소와 페이지 주소에서 찾는다."""
    if not site.startswith('sc-domain:'):
        return []
    root = site.split(':', 1)[1].lower()
    under = lambda h: h == root or h.endswith('.' + root)
    own = [x for x in sites if not x.startswith('sc-domain:') and under(host_of(x))]
    if not own:
        return []
    https_own = {host_of(x) for x in own if x.startswith('https://')}
    seen = {host_of(r['path'] or '') for r in con.execute('SELECT path FROM gsc_sitemap WHERE site=? AND run_date=?', (site, run_date))}
    seen |= {host_of(r['page'] or '') for r in con.execute('SELECT DISTINCT page FROM gsc_page WHERE site=? AND run_date=?', (site, run_date))}
    return sorted(h for h in seen if h and under(h) and h not in https_own)


def collect(s, con, prop, key, end, run_date, full=False, host=None):
    """속성 prop 의 일별 합계·검색어·페이지 상위·사이트맵을 key 이름으로 저장. host 가 있으면 그 하위 사이트 페이지만."""
    extra = {'dimensionFilterGroups': page_filter(host)} if host else {}
    row = con.execute('SELECT MAX(date) d FROM gsc_daily WHERE site=?', (key,)).fetchone()
    if full or not row['d']:
        start = end - dt.timedelta(days=FIRST_RUN_DAYS)
    else:
        start = dt.date.fromisoformat(row['d']) - dt.timedelta(days=RELOAD_DAYS)
    rows = gsc_query(s, prop, {'startDate': start.isoformat(), 'endDate': end.isoformat(), 'dimensions': ['date'], 'rowLimit': 1000, **extra})
    for x in rows:
        con.execute('INSERT OR REPLACE INTO gsc_daily VALUES (?,?,?,?,?,?)', (key, x['keys'][0], int(x['clicks']), int(x['impressions']), x['ctr'], x['position']))
    log(f'  {key}: 일별 {len(rows)}행 ({start}~{end})')
    # 최근 7일 / 28일 검색어·페이지 상위
    for window, days in (('7d', 7), ('28d', 28)):
        ws = (end - dt.timedelta(days=days + 2)).isoformat()   # 확정 지연 감안
        we = (end - dt.timedelta(days=2)).isoformat()
        for dim, table in (('query', 'gsc_query'), ('page', 'gsc_page')):
            rows = gsc_query(s, prop, {'startDate': ws, 'endDate': we, 'dimensions': [dim], 'rowLimit': 500, **extra})
            con.execute(f'DELETE FROM {table} WHERE site=? AND run_date=? AND window=?', (key, run_date, window))
            for x in rows:
                con.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?)', (key, run_date, window, x['keys'][0], int(x['clicks']), int(x['impressions']), x['position']))
    # 사이트맵 (나눠 받을 때는 그 하위 사이트 주소의 사이트맵만)
    r = s.get(f"{GSC}/sites/{requests.utils.quote(prop, safe='')}/sitemaps", timeout=60)
    if r.ok:
        for sm in r.json().get('sitemap', []):
            if host and host_of(sm.get('path') or '') != host:
                continue
            sub = sum(int(c.get('submitted', 0)) for c in sm.get('contents', []))
            idx = sum(int(c.get('indexed', 0)) for c in sm.get('contents', []))
            con.execute('INSERT OR REPLACE INTO gsc_sitemap VALUES (?,?,?,?,?,?,?,?)', (key, run_date, sm.get('path'), sub, idx, sm.get('lastDownloaded'), int(sm.get('errors', 0)), int(sm.get('warnings', 0))))


def sync_gsc(s, con, full=False):
    sites = gsc_sites(s)
    log(f'서치콘솔 속성 {len(sites)}개: ' + ', '.join(sites))
    end = today_kst()
    run_date = end.isoformat()
    for site in sites:
        collect(s, con, site, site, end, run_date, full)
    for site in sites:
        for host in split_hosts(con, site, sites, run_date):
            collect(s, con, site, f'{site}#{host}', end, run_date, full, host)
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
        rows = ga_report(s, pid, {'dateRanges': [{'startDate': '7daysAgo', 'endDate': 'today'}], 'dimensions': [{'name': 'hostName'}], 'metrics': [{'name': 'screenPageViews'}, {'name': 'activeUsers'}, {'name': 'sessions'}], 'limit': 20, 'orderBys': [{'metric': {'metricName': 'screenPageViews'}, 'desc': True}]})
        con.execute('DELETE FROM ga_host WHERE property=? AND run_date=?', (pid, run_date))
        for x in rows:
            m = [int(float(v['value'])) for v in x['metricValues']]
            con.execute('INSERT OR REPLACE INTO ga_host VALUES (?,?,?,?,?,?)', (pid, run_date, x['dimensionValues'][0]['value'], m[0], m[1], m[2]))
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
