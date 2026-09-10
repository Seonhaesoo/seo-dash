"""색인 알림 워커 — 사이트맵에서 새 주소를 찾아 IndexNow(네이버·빙)에 알리고, 새 주소가 나온 사이트맵은 구글 서치콘솔에 다시 제출한다.
주 1회 사이트마다 무작위 10쪽을 URL 검사로 재서 색인 표본(색인됨·발견 안 됨 등)을 쌓는다.
사용: python indexer.py               새 주소 알림(처음이면 전체) + 7일 지난 표본
      python indexer.py --dry         보내지 않고 무엇을 보낼지만
      python indexer.py --sample      표본만 지금
      python indexer.py --site 바디집  한 사이트만
열쇠 파일(https://호스트/<INDEXNOW_KEY>.txt)이 안 보이면 그 사이트는 알림을 건너뛴다.
IndexNow 는 한 번에 1만 개까지라, 엔진마다 한 번 실행에 1만 개씩 보내고 나머지는 다음 실행으로 넘긴다(짧은 주소 = 허브부터).
구글에는 '색인 요청'을 자동으로 보내는 공식 방법이 없어(Indexing API는 채용공고·라이브 방송 전용) 사이트맵 재제출만 한다."""
import os
import re
import sys
import json
import random
import datetime as dt

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

from db import tx, BASE

KEY_PATH = os.environ.get('SEO_DASH_KEY', os.path.join(BASE, 'data', 'key.json'))
INDEXNOW_KEY = 'd49d364be4edd2e8b512d402529d63f8'   # 공개 값 — 사이트마다 루트에 같은 이름의 .txt 로 올려 둠
ENGINES = [('naver', '네이버', 'https://searchadvisor.naver.com/indexnow'), ('bing', '빙', 'https://www.bing.com/indexnow')]
BATCH = 10000
SAMPLE_N = 10
SAMPLE_EVERY_DAYS = 7
UA = {'User-Agent': 'seo-dash-indexer/1.0 (+https://github.com/Seonhaesoo/seo-dash)'}
# (이름, 호스트, 켜짐) — 생일 사전은 https 가 켜진 뒤에 True 로 바꾼다(지금 보내면 http 주소라 곧 다시 보내야 함)
SITES = [
    ('사주첩', 'sajucheop.com', True),
    ('꿈해몽', 'dream.sajucheop.com', True),
    ('타로', 'tarot.sajucheop.com', True),
    ('생일 사전', 'saengil.sajucheop.com', False),
    ('돈표', 'donpyo.com', True),
    ('바디집', 'bodyzip.com', True),
]
GSC = 'https://www.googleapis.com/webmasters/v3'
INSPECT = 'https://searchconsole.googleapis.com/v1/urlInspection/index:inspect'
STATE = {'Submitted and indexed': 'indexed', 'Indexed, not submitted in sitemap': 'indexed',
         'Crawled - currently not indexed': 'crawled', 'Discovered - currently not indexed': 'discovered',
         'URL is unknown to Google': 'unknown'}


def now():
    return (dt.datetime.utcnow() + dt.timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')


def log(msg):
    print(dt.datetime.now().strftime('%H:%M:%S'), msg, flush=True)


def write_log(con, site, kind, engine, count, status, message=''):
    con.execute('INSERT INTO idx_log (ts, site, kind, engine, count, status, message) VALUES (?,?,?,?,?,?,?)',
                (now(), site, kind, engine, count, status, (message or '')[:300]))


def google():
    if not os.path.exists(KEY_PATH):
        return None
    creds = service_account.Credentials.from_service_account_file(KEY_PATH, scopes=['https://www.googleapis.com/auth/webmasters'])
    return AuthorizedSession(creds)


def gsc_props(s):
    if not s:
        return []
    r = s.get(GSC + '/sites', timeout=60)
    if r.status_code != 200:
        return []
    return [e['siteUrl'] for e in r.json().get('siteEntry', []) if e.get('permissionLevel') != 'siteUnverifiedUser']


def gsc_property(props, host):
    """호스트를 덮는 서치콘솔 속성 — 도메인 속성 > https 접두어 > http 접두어"""
    root = '.'.join(host.split('.')[-2:])
    for cand in (f'sc-domain:{root}', f'https://{host}/', f'http://{host}/'):
        if cand in props:
            return cand
    return None


def reachable(url):
    try:
        return requests.head(url, headers=UA, timeout=20, allow_redirects=True).status_code < 500
    except requests.RequestException:
        return False


def key_ok(scheme, host):
    try:
        r = requests.get(f'{scheme}://{host}/{INDEXNOW_KEY}.txt', headers=UA, timeout=30)
        return r.status_code == 200 and r.text.strip() == INDEXNOW_KEY
    except requests.RequestException:
        return False


def fetch_locs(url, top=None, depth=0):
    """사이트맵(또는 사이트맵 목록)의 주소들 → [(주소, 맨 위 사이트맵)] — 구글 재제출은 맨 위 사이트맵으로 한다"""
    top = top or url
    try:
        r = requests.get(url, headers=UA, timeout=60)
        if r.status_code != 200:
            return []
        text = r.text
    except requests.RequestException:
        return []
    locs = re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', text)
    if '<sitemapindex' in text and depth < 2:
        out = []
        for sm in locs:
            out += fetch_locs(sm, top, depth + 1)
        return out
    return [(u, top) for u in locs]


def sitemaps_for(s, prop, host, scheme):
    """robots.txt 의 Sitemap: 줄 + 서치콘솔에 제출된 이 호스트의 사이트맵"""
    maps = []
    try:
        r = requests.get(f'{scheme}://{host}/robots.txt', headers=UA, timeout=30)
        if r.status_code == 200:
            maps += re.findall(r'(?im)^\s*sitemap:\s*(\S+)', r.text)
    except requests.RequestException:
        pass
    if s and prop:
        r = s.get(f"{GSC}/sites/{requests.utils.quote(prop, safe='')}/sitemaps", timeout=60)
        if r.status_code == 200:
            maps += [m['path'] for m in r.json().get('sitemap', []) if f'//{host}/' in m['path']]
    if not maps:
        maps = [f'{scheme}://{host}/sitemap.xml']
    return list(dict.fromkeys(maps))


def indexnow(endpoint, scheme, host, urls):
    body = {'host': host, 'key': INDEXNOW_KEY, 'keyLocation': f'{scheme}://{host}/{INDEXNOW_KEY}.txt', 'urlList': urls}
    try:
        r = requests.post(endpoint, json=body, headers={'Content-Type': 'application/json; charset=utf-8', **UA}, timeout=120)
        return r.status_code, r.text[:200]
    except requests.RequestException as e:
        return 0, str(e)[:200]


def resubmit(s, prop, sitemap):
    url = f"{GSC}/sites/{requests.utils.quote(prop, safe='')}/sitemaps/{requests.utils.quote(sitemap, safe='')}"
    r = s.put(url, timeout=60)
    return r.status_code, r.text[:200]


def process(con, s, name, host, scheme, prop, dry):
    maps = sitemaps_for(s, prop, host, scheme)
    pairs = []
    for m in maps:
        pairs += fetch_locs(m)
    pairs = [(u, m) for u, m in pairs if re.match(rf'^https?://{re.escape(host)}/', u)]
    if not pairs:
        log(f'{name}: 사이트맵에서 주소를 못 읽음')
        write_log(con, name, 'skip', '', 0, 'error', '사이트맵 비어 있음: ' + ', '.join(maps))
        return
    count = lambda: con.execute('SELECT COUNT(*) n FROM idx_url WHERE site=?', (name,)).fetchone()['n']
    before, ts = count(), now()
    con.executemany('INSERT OR IGNORE INTO idx_url (site, url, sitemap, first_seen) VALUES (?,?,?,?)', [(name, u, m, ts) for u, m in pairs])
    new = count() - before
    log(f'{name}: 사이트맵 {len(maps)}개 · 주소 {len(pairs):,} · 새 주소 {new:,} · {scheme}')
    if not key_ok(scheme, host):
        log(f'{name}: 열쇠 파일이 안 보여 IndexNow 건너뜀')
        write_log(con, name, 'skip', '', 0, 'error', f'{scheme}://{host}/{INDEXNOW_KEY}.txt 가 안 보임')
    else:
        for eng, label, endpoint in ENGINES:
            col = f'{eng}_at'
            urls = [r['url'] for r in con.execute(f'SELECT url FROM idx_url WHERE site=? AND {col} IS NULL ORDER BY first_seen, length(url), url LIMIT ?', (name, BATCH))]
            if not urls:
                continue
            if dry:
                log(f'  → {label}: {len(urls):,}개 보낼 예정 (보내지 않음)')
                continue
            code, msg = indexnow(endpoint, scheme, host, urls)
            ok = code in (200, 202)
            log(f'  → {label}: {len(urls):,}개 · HTTP {code}{"" if ok else " " + msg}')
            write_log(con, name, 'indexnow', label, len(urls), 'ok' if ok else f'HTTP {code}', msg)
            if ok:
                con.executemany(f'UPDATE idx_url SET {col}=? WHERE site=? AND url=?', [(ts, name, u) for u in urls])
    if new and s and prop and not dry:
        for m in [r['sitemap'] for r in con.execute('SELECT DISTINCT sitemap FROM idx_url WHERE site=? AND first_seen=?', (name, ts))]:
            code, msg = resubmit(s, prop, m)
            ok = code in (200, 204)
            log(f'  → 구글 사이트맵 다시 제출 {m} · HTTP {code}')
            write_log(con, name, 'sitemap', '구글', 1, 'ok' if ok else f'HTTP {code}', m if ok else msg)


def sample_due(con, name):
    d = con.execute('SELECT MAX(date) d FROM idx_sample WHERE site=?', (name,)).fetchone()['d']
    return not d or (dt.date.fromisoformat(now()[:10]) - dt.date.fromisoformat(d)).days >= SAMPLE_EVERY_DAYS


def sample(con, s, name, prop):
    urls = [r['url'] for r in con.execute('SELECT url FROM idx_url WHERE site=?', (name,))]
    if not urls:
        return None
    c = {'indexed': 0, 'crawled': 0, 'discovered': 0, 'unknown': 0, 'other': 0}
    pick = random.sample(urls, min(SAMPLE_N, len(urls)))
    for u in pick:
        r = s.post(INSPECT, json={'inspectionUrl': u, 'siteUrl': prop}, timeout=90)
        st = r.json().get('inspectionResult', {}).get('indexStatusResult', {}).get('coverageState', '') if r.status_code == 200 else ''
        c[STATE.get(st, 'other')] += 1
    con.execute('INSERT OR REPLACE INTO idx_sample (site, date, total, indexed, crawled, discovered, unknown, other) VALUES (?,?,?,?,?,?,?,?)',
                (name, now()[:10], len(pick), c['indexed'], c['crawled'], c['discovered'], c['unknown'], c['other']))
    return c


def run(dry=False, only=None, sample_only=False):
    s = google()
    props = gsc_props(s)
    if not s:
        log('구글 키가 없어 IndexNow 만 보냅니다')
    with tx() as con:
        for name, host, on in SITES:
            if only and only != name:
                continue
            if not on:
                log(f'{name}: 꺼 둠 — 건너뜀')
                continue
            try:
                scheme = 'https' if reachable(f'https://{host}/') else 'http'
                prop = gsc_property(props, host)
                if not sample_only:
                    process(con, s, name, host, scheme, prop, dry)
                if s and prop and not dry and (sample_only or sample_due(con, name)):
                    c = sample(con, s, name, prop)
                    if c:
                        log(f'{name}: 색인 표본 {c}')
                        write_log(con, name, 'sample', '구글', SAMPLE_N, 'ok', json.dumps(c, ensure_ascii=False))
                elif not prop:
                    log(f'{name}: 서치콘솔 속성이 없어 표본·사이트맵 재제출은 건너뜀')
            except Exception as e:  # 한 사이트가 실패해도 나머지는 계속
                log(f'{name}: 오류 {e}')
                write_log(con, name, 'error', '', 0, 'error', str(e))
            if dry:
                con.rollback()          # 미리보기는 아무것도 저장하지 않는다(저장하면 다음 실행에서 '새 주소'가 0이 되어 구글 재제출이 빠짐)
            else:
                con.commit()


if __name__ == '__main__':
    args = sys.argv[1:]
    only = args[args.index('--site') + 1] if '--site' in args and args.index('--site') + 1 < len(args) else None
    run(dry='--dry' in args, only=only, sample_only='--sample' in args)
