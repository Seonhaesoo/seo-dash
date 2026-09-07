"""SEO 대시보드 웹 — 서치콘솔·GA4 수집값을 한 장으로. LAN 전용 (0.0.0.0:8080).
동기화 버튼(POST /sync), 서버 시작 시 12시간 넘게 지났으면 자동 동기화, key.json 업로드(/settings)."""
import os
import io
import csv
import json
import threading
import datetime as dt
import subprocess

from flask import Flask, render_template, request, redirect, url_for, jsonify

from db import connect, tx, BASE, DB_PATH
import collector

app = Flask(__name__, template_folder=os.path.join(BASE, 'templates'))
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024
SYNC_LOCK = threading.Lock()
SYNC_STATE = {'running': False, 'log': ''}
KEY_PATH = collector.KEY_PATH


# ---------- 동기화 ----------
def start_sync(full=False):
    if SYNC_STATE['running']:
        return False
    def job():
        SYNC_STATE['running'] = True
        try:
            collector.run(full=full)
        finally:
            SYNC_STATE['running'] = False
    threading.Thread(target=job, daemon=True).start()
    return True


def last_sync():
    con = connect()
    row = con.execute('SELECT * FROM sync_log ORDER BY id DESC LIMIT 1').fetchone()
    con.close()
    return row


def maybe_auto_sync():
    if not os.path.exists(KEY_PATH):
        return
    row = last_sync()
    if row and row['status'] == 'ok' and row['finished']:
        age = dt.datetime.now() - dt.datetime.fromisoformat(row['finished'])
        if age < dt.timedelta(hours=12):
            return
    start_sync()


# ---------- 계산 ----------
def site_label(site):
    s = site.replace('sc-domain:', '').replace('https://', '').replace('http://', '').rstrip('/')
    return s


def pct(a, b):
    if not b:
        return None
    return round((a - b) / b * 100)


def summarize_gsc(con, site, end):
    def rng(d0, d1):
        r = con.execute('SELECT COALESCE(SUM(clicks),0) c, COALESCE(SUM(impressions),0) i, AVG(position) p FROM gsc_daily WHERE site=? AND date BETWEEN ? AND ?', (site, d0.isoformat(), d1.isoformat())).fetchone()
        return {'clicks': r['c'], 'impressions': r['i'], 'position': round(r['p'], 1) if r['p'] else None}
    last = con.execute('SELECT MAX(date) d FROM gsc_daily WHERE site=? AND impressions>0', (site,)).fetchone()['d']
    anchor = dt.date.fromisoformat(last) if last else end - dt.timedelta(days=2)
    cur = rng(anchor - dt.timedelta(days=6), anchor)
    prev = rng(anchor - dt.timedelta(days=13), anchor - dt.timedelta(days=7))
    day = rng(anchor, anchor)
    series = con.execute('SELECT date, clicks, impressions FROM gsc_daily WHERE site=? AND date>=? ORDER BY date', (site, (anchor - dt.timedelta(days=29)).isoformat())).fetchall()
    run = con.execute('SELECT MAX(run_date) r FROM gsc_query WHERE site=?', (site,)).fetchone()['r']
    queries = con.execute('SELECT * FROM gsc_query WHERE site=? AND run_date=? AND window=? ORDER BY clicks DESC, impressions DESC LIMIT 12', (site, run, '7d')).fetchall() if run else []
    pages = con.execute('SELECT * FROM gsc_page WHERE site=? AND run_date=? AND window=? ORDER BY clicks DESC, impressions DESC LIMIT 12', (site, run, '7d')).fetchall() if run else []
    # 28일 기준 노출 많은데 순위 낮은 검색어 = 기회
    chances = con.execute('SELECT * FROM gsc_query WHERE site=? AND run_date=? AND window=? AND impressions>=20 AND position>10 ORDER BY impressions DESC LIMIT 8', (site, run, '28d')).fetchall() if run else []
    smr = con.execute('SELECT MAX(run_date) r FROM gsc_sitemap WHERE site=?', (site,)).fetchone()['r']
    sitemaps = con.execute('SELECT * FROM gsc_sitemap WHERE site=? AND run_date=?', (site, smr)).fetchall() if smr else []
    return {'site': site, 'label': site_label(site), 'anchor': anchor.isoformat(), 'day': day, 'cur': cur, 'prev': prev,
            'd_clicks': pct(cur['clicks'], prev['clicks']), 'd_imp': pct(cur['impressions'], prev['impressions']),
            'series': [dict(r) for r in series], 'queries': [dict(r) for r in queries], 'pages': [dict(r) for r in pages], 'chances': [dict(r) for r in chances],
            'sitemaps': [dict(r) for r in sitemaps], 'submitted': sum(r['submitted'] for r in sitemaps), 'indexed': sum(r['indexed'] for r in sitemaps)}


def summarize_ga(con, pid, end):
    name = con.execute('SELECT name FROM ga_daily WHERE property=? LIMIT 1', (pid,)).fetchone()['name']
    def rng(d0, d1):
        r = con.execute('SELECT COALESCE(SUM(users),0) u, COALESCE(SUM(pageviews),0) p, COALESCE(SUM(sessions),0) s FROM ga_daily WHERE property=? AND date BETWEEN ? AND ?', (pid, d0.isoformat(), d1.isoformat())).fetchone()
        return {'users': r['u'], 'pageviews': r['p'], 'sessions': r['s']}
    y = end - dt.timedelta(days=1)
    yesterday = rng(y, y)
    cur = rng(end - dt.timedelta(days=7), y)
    prev = rng(end - dt.timedelta(days=14), end - dt.timedelta(days=8))
    series = con.execute('SELECT date, users, pageviews FROM ga_daily WHERE property=? AND date>=? AND date<? ORDER BY date', (pid, (end - dt.timedelta(days=30)).isoformat(), end.isoformat())).fetchall()
    run = con.execute('SELECT MAX(run_date) r FROM ga_page WHERE property=?', (pid,)).fetchone()['r']
    pages = con.execute('SELECT * FROM ga_page WHERE property=? AND run_date=? ORDER BY pageviews DESC LIMIT 12', (pid, run)).fetchall() if run else []
    sources = con.execute('SELECT * FROM ga_source WHERE property=? AND run_date=? ORDER BY sessions DESC LIMIT 8', (pid, run)).fetchall() if run else []
    return {'property': pid, 'name': name, 'yesterday': yesterday, 'cur': cur, 'prev': prev, 'd_users': pct(cur['users'], prev['users']),
            'series': [dict(r) for r in series], 'pages': [dict(r) for r in pages], 'sources': [dict(r) for r in sources]}


def headline(gsc, ga):
    """한 줄 요약 — 숫자를 말로."""
    parts = []
    for g in gsc:
        c, i = g['cur']['clicks'], g['cur']['impressions']
        if i == 0:
            parts.append(f"{g['label']}: 아직 검색 노출이 잡히지 않음 (색인 대기)")
        elif c == 0:
            parts.append(f"{g['label']}: 노출 {i:,}회지만 클릭 0 — 순위가 낮은 단계")
        else:
            trend = '' if g['d_clicks'] is None else (f" ({g['d_clicks']:+d}%)" if g['d_clicks'] else ' (보합)')
            parts.append(f"{g['label']}: 주간 클릭 {c:,}·노출 {i:,}{trend}")
    return parts


@app.route('/')
def index():
    con = connect()
    end = collector.today_kst()
    sites = [r['site'] for r in con.execute('SELECT DISTINCT site FROM gsc_daily ORDER BY site')]
    props = [r['property'] for r in con.execute('SELECT DISTINCT property FROM ga_daily ORDER BY name')]
    gsc = [summarize_gsc(con, s, end) for s in sites]
    ga = [summarize_ga(con, p, end) for p in props]
    naver = [dict(r) for r in con.execute('SELECT site, MAX(date) d, SUM(clicks) c, SUM(impressions) i FROM naver_daily WHERE date>=? GROUP BY site', ((end - dt.timedelta(days=7)).isoformat(),))]
    con.close()
    return render_template('index.html', gsc=gsc, ga=ga, naver=naver, heads=headline(gsc, ga), last=last_sync(), running=SYNC_STATE['running'],
                           has_key=os.path.exists(KEY_PATH), today=end.isoformat(), host=request.host)


@app.route('/sync', methods=['POST'])
def sync():
    if not os.path.exists(KEY_PATH):
        return redirect(url_for('settings'))
    start_sync(full=request.form.get('full') == '1')
    return redirect(url_for('index'))


@app.route('/sync/status')
def sync_status():
    row = last_sync()
    return jsonify({'running': SYNC_STATE['running'], 'last': dict(row) if row else None})


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    msg = ''
    if request.method == 'POST':
        f = request.files.get('key')
        if f and f.filename:
            data = f.read()
            try:
                j = json.loads(data)
                assert j.get('type') == 'service_account' and j.get('client_email')
            except Exception:
                msg = '서비스 계정 키(JSON) 파일이 아닙니다.'
            else:
                os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
                with open(KEY_PATH, 'wb') as out:
                    out.write(data)
                os.chmod(KEY_PATH, 0o600)
                msg = f"키 저장 완료 — 서비스 계정 {j['client_email']} 을(를) 서치콘솔·GA4에 사용자로 추가했는지 확인한 뒤 동기화하세요."
        nv = request.files.get('naver')
        if nv and nv.filename:
            msg = import_naver(nv, request.form.get('naver_site', ''))
    email = None
    if os.path.exists(KEY_PATH):
        try:
            email = json.load(open(KEY_PATH))['client_email']
        except Exception:
            email = '(키 파일을 읽을 수 없음)'
    con = connect()
    logs = con.execute('SELECT * FROM sync_log ORDER BY id DESC LIMIT 15').fetchall()
    con.close()
    return render_template('settings.html', msg=msg, email=email, key_path=KEY_PATH, db_path=DB_PATH, logs=logs, running=SYNC_STATE['running'])


def import_naver(f, site):
    """네이버 서치어드바이저 '검색 노출/클릭' CSV 또는 '일별 방문' CSV — 열 이름으로 날짜·클릭·노출·방문 찾기."""
    if not site:
        return '네이버 CSV는 사이트 주소를 함께 적어야 합니다.'
    raw = f.read()
    for enc in ('utf-8-sig', 'cp949', 'utf-8'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return 'CSV 인코딩을 읽을 수 없습니다.'
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return '빈 파일입니다.'
    head = [h.strip().lower() for h in rows[0]]
    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if n in h:
                    return i
        return None
    ci, cc, cimp, cv = col('날짜', 'date', '일자'), col('클릭'), col('노출'), col('방문', '유입')
    if ci is None:
        return 'CSV에서 날짜 열을 찾지 못했습니다: ' + ', '.join(rows[0])
    n = 0
    with tx() as con:
        for r in rows[1:]:
            if len(r) <= ci or not r[ci].strip():
                continue
            d = r[ci].strip().replace('.', '-').replace('/', '-')[:10]
            def num(i):
                try:
                    return int(float(r[i].replace(',', ''))) if i is not None and i < len(r) and r[i].strip() else 0
                except ValueError:
                    return 0
            con.execute('INSERT OR REPLACE INTO naver_daily VALUES (?,?,?,?,?)', (site, d, num(cc), num(cimp), num(cv)))
            n += 1
    return f'네이버 {n}일치 저장 완료.'


@app.route('/health')
def health():
    return 'ok'


if __name__ == '__main__':
    threading.Timer(5, maybe_auto_sync).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8080')), threaded=True)
