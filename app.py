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


# ---------- 읽기 쉽게 ----------
SOURCE_KO = {
    '(direct)': '직접 방문 (주소창·북마크·메신저 링크)',
    '(not set)': '알 수 없음',
    '(data not available)': '알 수 없음 (동의 안 된 방문)',
    'google': '구글 검색',
    'bing': '빙 검색',
    'naver': '네이버',
    'daum': '다음',
    'chatgpt.com': 'ChatGPT',
    'l.threads.com': '쓰레드',
    'threads.net': '쓰레드',
    'l.instagram.com': '인스타그램',
    'instagram.com': '인스타그램',
    'm.facebook.com': '페이스북 (모바일)',
    'facebook.com': '페이스북',
    'l.facebook.com': '페이스북',
    't.co': 'X (트위터)',
    'sajucheop.com': '사주첩에서 넘어옴',
    'donpyo.com': '돈표에서 넘어옴',
    'bodyzip.com': '바디집에서 넘어옴',
    'saengil.sajucheop.com': '생일 사전에서 넘어옴',
    'dream.sajucheop.com': '꿈해몽에서 넘어옴',
    'tarot.sajucheop.com': '타로에서 넘어옴',
    'daytip.kr': 'daytip.kr에서 넘어옴',
}
SEARCH_SOURCES = ('google', 'bing', 'naver', 'daum', 'yahoo', 'search.naver')
SNS_SOURCES = ('threads', 'instagram', 'facebook', 't.co', 'kakao', 'band.us', 'youtube')

HOST_KO = {
    'sajucheop.com': '사주첩', 'www.sajucheop.com': '사주첩',
    'saengil.sajucheop.com': '생일 사전', 'dream.sajucheop.com': '꿈해몽', 'tarot.sajucheop.com': '타로',
    'donpyo.com': '돈표', 'www.donpyo.com': '돈표',
    'bodyzip.com': '바디집', 'www.bodyzip.com': '바디집',
    'daytip.kr': 'daytip.kr', 'www.daytip.kr': 'daytip.kr',
    'seonhaesoo.github.io': 'github.io (도메인 연결 전)',
}
PATH_KO = [
    ('/embed', '위젯'), ('/checkup', '건강검진 해석'), ('/bp/', '혈압'), ('/glucose', '공복혈당'),
    ('/ldl', 'LDL'), ('/hdl', 'HDL'), ('/cholesterol', '콜레스테롤'), ('/triglyceride', '중성지방'),
    ('/liver', '간수치'), ('/uric', '요산'), ('/bmi', 'BMI'), ('/bmr', '기초대사량'), ('/bodyfat', '체지방률'),
    ('/food', '음식 칼로리'), ('/exercise', '운동 칼로리'), ('/steps', '걸음 수'), ('/water', '물·단백질'),
    ('/due-date', '출산예정일'), ('/ovulation', '배란일'), ('/pregnancy', '임신 주차'), ('/baby', '아기'),
    ('/pet', '반려동물'), ('/guide', '서재'), ('/sleep', '수면'), ('/diet', '다이어트 기간'),
    ('/alcohol', '혈중알코올'), ('/caffeine', '카페인'), ('/quit-smoking', '금연'), ('/child-height', '아이 키'),
    ('/today', '오늘 담기'), ('/weight', '체중 기록'), ('/kcal-need', '권장 칼로리'),
    ('/salary', '연봉 실수령'), ('/hourly', '시급'), ('/monthly', '월급'), ('/loan', '대출'),
    ('/unemployment', '실업급여'), ('/yearend', '연말정산'), ('/dsr', 'DSR'), ('/couple', '맞벌이'),
    ('/subscription', '청약 가점'), ('/parental-leave', '육아휴직'), ('/electric', '전기요금'),
    ('/annual', '연차수당'), ('/freelance', '프리랜서 3.3%'), ('/nhis', '건강보험료'),
    ('/eitc', '근로장려금'), ('/pension', '국민연금'), ('/capgain', '양도소득세'), ('/carcost', '자동차 유지비'),
    ('/d/', '꿈 상징'), ('/taemong', '태몽'), ('/gilmong', '길몽'),
    ('/2027', '2027 신년운세'), ('/ilju', '일주'), ('/gunghap', '궁합'), ('/today/ddi', '오늘의 띠별 운세'),
    ('/draw', '타로 뽑기'), ('/major', '메이저 카드'), ('/yesno', '예·아니오'),
    ('/age', '만나이'), ('/school', '학년'), ('/cal', '달력'), ('/ddi', '띠'), ('/zodiac', '별자리'),
    ('/en', '영문'), ('/method', '계산 기준'), ('/about', '소개'),
]


def source_ko(src):
    """유입 이름을 사람이 읽는 말로."""
    s = (src or '').strip()
    if s in SOURCE_KO:
        return SOURCE_KO[s]
    low = s.lower()
    for k, v in SOURCE_KO.items():
        if k in low:
            return v
    return s or '알 수 없음'


def source_kind(src):
    low = (src or '').lower()
    if low.startswith('(direct)'):
        return 'direct'
    if any(k in low for k in SEARCH_SOURCES):
        return 'search'
    if any(k in low for k in SNS_SOURCES):
        return 'sns'
    if low.startswith('(not set)') or 'not available' in low:
        return 'unknown'
    return 'link'


def host_ko(h):
    h = (h or '').strip()
    return HOST_KO.get(h, h or '알 수 없음')


def path_ko(p):
    """경로 앞부분으로 무슨 페이지인지 한마디."""
    p = p or '/'
    if p in ('/', ''):
        return '홈'
    for prefix, label in PATH_KO:
        if p.startswith(prefix):
            return label
    return ''


def ga_headline(a):
    """GA 카드 한 줄 해석."""
    y, cur = a['yesterday']['users'], a['cur']['users']
    if cur == 0:
        return '아직 방문 기록이 없습니다. 태그를 붙인 직후라면 하루쯤 기다려 보세요.'
    bits = [f"어제 {y:,}명, 지난 7일 {cur:,}명"]
    if a['d_users'] is not None:
        bits.append('이전 7일보다 ' + (f"{a['d_users']}% 늘었습니다" if a['d_users'] > 0 else (f"{abs(a['d_users'])}% 줄었습니다" if a['d_users'] < 0 else '비슷합니다')))
    kinds = {}
    for s0 in a['sources']:
        kinds[source_kind(s0['source'])] = kinds.get(source_kind(s0['source']), 0) + s0['sessions']
    total = sum(kinds.values()) or 1
    search, sns, direct = kinds.get('search', 0), kinds.get('sns', 0), kinds.get('direct', 0)
    if search / total >= 0.3:
        bits.append(f"검색으로 온 사람이 {search}세션으로 가장 큰 몫입니다")
    elif sns and sns >= search:
        bits.append(f"SNS에서 {sns}세션이 들어왔고 검색은 {search}세션입니다")
    elif direct / total >= 0.6:
        bits.append(f"대부분({direct}세션)이 직접 방문이라 아직 검색으로 발견되는 단계는 아닙니다")
    return '. '.join(bits) + '.'


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
    hosts = con.execute('SELECT * FROM ga_host WHERE property=? AND run_date=? ORDER BY pageviews DESC LIMIT 8', (pid, run)).fetchall() if run else []
    hosts = [dict(h) | {'label': host_ko(h['host'])} for h in hosts]
    top_host = hosts[0]['host'] if hosts else ''
    out = {'property': pid, 'name': name, 'yesterday': yesterday, 'cur': cur, 'prev': prev, 'd_users': pct(cur['users'], prev['users']),
           'series': [dict(r) for r in series], 'hosts': hosts, 'mixed': len(hosts) > 1,
           'pages': [dict(p) | {'label': path_ko(p['page']), 'url': ('https://' + top_host + p['page']) if top_host else ''} for p in pages],
           'sources': [dict(r) | {'label': source_ko(r['source']), 'kind': source_kind(r['source'])} for r in sources]}
    out['headline'] = ga_headline(out)
    return out


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
