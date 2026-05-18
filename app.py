"""
MJT 모바일 현황 대시보드 — Flask
Google Sheets 데이터를 모바일 브라우저로 조회 + 오늘 메뉴 등록
"""
import os, json, base64, time, threading, secrets, io
from datetime import datetime, date, timedelta
from collections import defaultdict
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, send_file

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))

# 식당 메뉴 등록 비밀번호
MENU_PW = os.environ.get('MENU_PW', '3838')

# ── 결재 설정 ────────────────────────────────────────────────────
# render.com 환경변수에 APPROVE_GRP_MJ_PW / APPROVE_CEO_MJ_PW /
# APPROVE_GRP_SCS_PW / APPROVE_DIR_SCS_PW 로 각 결재자 비밀번호 설정
APPROVAL_CFG = {
    'MJ': {
        'grp': {'name': '이광희', 'title': '제조그룹장', 'stamp': '이광희',
                'pw': os.environ.get('APPROVE_GRP_MJ_PW', '')},
        'ceo': {'name': '김신욱', 'title': '대표이사',   'stamp': '김신욱',
                'pw': os.environ.get('APPROVE_CEO_MJ_PW', '')},
    },
    'SCS': {
        'grp': {'name': '이현승', 'title': '제조팀장',   'stamp': '이현승',
                'pw': os.environ.get('APPROVE_GRP_SCS_PW', '')},
        'ceo': {'name': '김멋진', 'title': '이사',       'stamp': '김멋진',
                'pw': os.environ.get('APPROVE_DIR_SCS_PW', '')},
    },
}

# ── 상수 ────────────────────────────────────────────────────────
SHEET_NAME   = 'MJT_식수관리'
SCOPES       = ['https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive']
ATT_CAT_OT   = ['잔업', '특근']
ATT_CAT_LEAVE= ['연차','오전반차','오후반차','탄력오전','탄력오후','계절휴가','하계휴가','기휴']
OT_DANGER_H  = 12   # 주 52H 한도 OT
OT_MAX64_H   = 24   # 주 64H 한도 OT
CACHE_TTL    = 300  # 5분 캐시

# ── 인증 ────────────────────────────────────────────────────────
def _get_creds():
    env_val = os.environ.get('GOOGLE_CREDS_JSON')
    if env_val:
        info = json.loads(base64.b64decode(env_val).decode('utf-8'))
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    cred_file = os.path.join(os.path.dirname(__file__), '..',
                             'youtubehotfinder-471114-52ceb99f60d8.json')
    return Credentials.from_service_account_file(cred_file, scopes=SCOPES)

def _open_sh():
    gc = gspread.authorize(_get_creds())
    try:
        gc.session.timeout = (15, 60)
    except Exception:
        pass
    return gc.open(SHEET_NAME)

# ── 캐시 ────────────────────────────────────────────────────────
_cache: dict = {}
_lock = threading.Lock()

def _cached(key, fn, ttl=CACHE_TTL):
    with _lock:
        e = _cache.get(key)
        if e and time.time() - e['t'] < ttl:
            return e['d']
    d = fn()
    with _lock:
        _cache[key] = {'d': d, 't': time.time()}
    return d

def _clear_cache():
    with _lock:
        _cache.clear()

# ── 데이터 함수 ──────────────────────────────────────────────────
def get_employees():
    def _f():
        sh = _open_sh()
        rows = sh.worksheet('사원마스터').get_all_records()
        return [r for r in rows if str(r.get('사용여부', 'Y')).strip() == 'Y']
    return _cached('emps', _f)

def get_att_records(year, month):
    def _f():
        sh = _open_sh()
        rows = sh.worksheet('근태기록').get_all_records()
        return [r for r in rows
                if str(r.get('연도', '')).strip() == str(year)
                and str(r.get('월', '')).strip() == str(month)]
    return _cached(f'att_{year}_{month}', _f)

def get_today_menu(ds: str) -> str:
    def _f():
        try:
            sh = _open_sh()
            rows = sh.worksheet('오늘점심메뉴').get_all_values()[1:]
            for r in rows:
                if len(r) >= 2 and r[0].strip() == ds:
                    return r[1].strip()
        except Exception:
            pass
        return ''
    return _cached(f'menu_{ds}', _f, ttl=60)

def save_today_menu(ds: str, menu: str):
    sh = _open_sh()
    ws = sh.worksheet('오늘점심메뉴')
    rows = ws.get_all_values()
    now_s = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for i, r in enumerate(rows):
        if i == 0:
            continue
        if r and r[0].strip() == ds:
            ws.update(range_name=f'A{i+1}:D{i+1}',
                      values=[[ds, menu, '식당', now_s]])
            with _lock:
                _cache.pop(f'menu_{ds}', None)
            return
    ws.append_row([ds, menu, '식당', now_s], value_input_option='USER_ENTERED')
    with _lock:
        _cache.pop(f'menu_{ds}', None)

def get_meal_today(ds: str) -> dict:
    # 중식신청:   [날짜, 시간, 공장, 사원번호, 성명, 부서, 상태]
    # 중식실식수: [날짜, 시간, 유형, 사원번호, 성명, 부서, 직급, 메모, 공장]
    # 저녁도시락: [날짜, 시간, 사원번호, 성명, 부서, 직급, 성별, 사유]
    # 특근식사:   [날짜, 시간, 사원번호, 성명, 부서, 직급, mode, 메뉴, ...]
    # 외부손님:   [방문날짜, 등록날짜, 시간, 담당자id, 담당자성명, 부서, 회사명, 손님성명, 사유]
    def _f():
        sh = _open_sh()
        out = {}
        def _get(name, date_col=0):
            try:
                rows = sh.worksheet(name).get_all_values()[1:]
                return [r for r in rows if len(r) > date_col and r[date_col].strip() == ds]
            except Exception:
                return []
        out['중식신청']   = _get('중식신청')
        out['중식실식수'] = _get('중식실식수')
        out['저녁도시락'] = _get('저녁도시락')
        out['특근식사']   = _get('특근식사')
        out['외부손님']   = _get('외부손님', date_col=0)
        return out
    return _cached(f'meal_{ds}', _f, ttl=120)  # 2분 캐시

# ── 52H 위험도 계산 ──────────────────────────────────────────────
def calc_ot_status(emps, att_rows, year, month):
    fw = date(year, month, 1).weekday()
    def wn(d):
        return max(1, min(6, (d + fw - 1) // 7 + 1))

    emp_map = {str(e.get('사원번호', '')).strip(): e for e in emps}
    emp_wk: dict = {}

    for r in att_rows:
        eid   = str(r.get('사원번호', '')).strip()
        atype = str(r.get('근태유형', '')).strip()
        try:   v = float(r.get('값', 0) or 0)
        except: v = 0.0
        try:   day = int(str(r.get('일자', '')).split('-')[-1])
        except: continue
        w = wn(day)
        if eid not in emp_wk:
            emp_wk[eid] = {'ot': {i: 0.0 for i in range(1, 7)},
                           'lv': {i: 0.0 for i in range(1, 7)},
                           'jn': 0.0, 'sp': 0.0}
        if atype == '잔업':
            emp_wk[eid]['ot'][w] += v; emp_wk[eid]['jn'] += v
        elif atype == '특근':
            emp_wk[eid]['ot'][w] += v; emp_wk[eid]['sp'] += v
        elif atype in ATT_CAT_LEAVE:
            emp_wk[eid]['lv'][w] += v * 8

    rows = []
    for eid, wk in emp_wk.items():
        e = emp_map.get(eid, {})
        avail52  = {w: OT_DANGER_H + wk['lv'][w] for w in range(1, 7)}
        avail64  = {w: OT_MAX64_H  + wk['lv'][w] for w in range(1, 7)}
        worst52  = max(wk['ot'][w] - avail52[w] for w in range(1, 7))
        worst64  = max(wk['ot'][w] - avail64[w] for w in range(1, 7))
        worst_wk = max(wk['ot'].values())
        total_ot = wk['jn'] + wk['sp']
        if   worst64 >= 0:    status = 'max64'
        elif worst52 >= 0:    status = 'danger'
        elif worst52 >= -4:   status = 'warn'
        else:                 status = 'safe'
        rows.append({
            'eid': eid,
            'name': e.get('성명', eid),
            'dept': e.get('부서명', '?'),
            'total_ot': total_ot,
            'jn':  wk['jn'],
            'sp':  wk['sp'],
            'worst_wk': worst_wk,
            'status': status,
        })
    status_order = {'max64': 0, 'danger': 1, 'warn': 2, 'safe': 3}
    rows.sort(key=lambda r: (status_order[r['status']], -r['total_ot']))
    return rows

# ── 공휴일 DB ────────────────────────────────────────────────────
def _build_holiday_db():
    db = {}
    fixed = {
        '01-01': '신정',      '03-01': '삼일절',     '05-01': '근로자의날',
        '05-05': '어린이날',  '06-06': '현충일',     '08-15': '광복절',
        '10-03': '개천절',    '10-09': '한글날',     '12-25': '성탄절',
    }
    for yr in range(2025, 2051):
        for mmdd, name in fixed.items():
            db[f'{yr}-{mmdd}'] = name
    lunar = {
        '2026-02-16':'설날 연휴','2026-02-17':'설날','2026-02-18':'설날 연휴',
        '2026-05-25':'부처님오신날',
        '2026-09-24':'추석 연휴','2026-09-25':'추석','2026-09-26':'추석 연휴',
        '2027-02-05':'설날 연휴','2027-02-06':'설날','2027-02-07':'설날 연휴',
        '2027-05-13':'부처님오신날',
        '2027-09-14':'추석 연휴','2027-09-15':'추석','2027-09-16':'추석 연휴',
        '2028-01-26':'설날 연휴','2028-01-27':'설날','2028-01-28':'설날 연휴',
        '2028-05-02':'부처님오신날',
        '2028-10-02':'추석 연휴','2028-10-03':'추석','2028-10-04':'추석 연휴',
        '2029-02-12':'설날 연휴','2029-02-13':'설날','2029-02-14':'설날 연휴',
        '2029-05-21':'부처님오신날',
        '2029-10-05':'추석','2029-10-06':'추석 연휴',
        '2030-02-02':'설날 연휴','2030-02-03':'설날','2030-02-04':'설날 연휴',
        '2030-05-11':'부처님오신날',
        '2030-09-22':'추석 연휴','2030-09-23':'추석','2030-09-24':'추석 연휴',
        '2031-01-22':'설날 연휴','2031-01-23':'설날','2031-01-24':'설날 연휴',
        '2031-05-28':'부처님오신날',
        '2031-09-11':'추석 연휴','2031-09-12':'추석','2031-09-13':'추석 연휴',
        '2032-02-10':'설날 연휴','2032-02-11':'설날','2032-02-12':'설날 연휴',
        '2032-05-16':'부처님오신날',
        '2032-09-29':'추석 연휴','2032-09-30':'추석','2032-10-01':'추석 연휴',
        '2033-01-30':'설날 연휴','2033-01-31':'설날','2033-02-01':'설날 연휴',
        '2033-05-05':'부처님오신날',
        '2033-09-18':'추석 연휴','2033-09-19':'추석','2033-09-20':'추석 연휴',
        '2034-02-18':'설날 연휴','2034-02-19':'설날','2034-02-20':'설날 연휴',
        '2034-05-25':'부처님오신날',
        '2034-10-07':'추석 연휴','2034-10-08':'추석','2034-10-09':'추석 연휴',
        '2035-02-07':'설날 연휴','2035-02-08':'설날','2035-02-09':'설날 연휴',
        '2035-05-14':'부처님오신날',
        '2035-09-26':'추석 연휴','2035-09-27':'추석','2035-09-28':'추석 연휴',
    }
    db.update(lunar)
    return db

BASE_HOLIDAYS = _build_holiday_db()

def get_managed_holidays():
    def _f():
        try:
            sh = _open_sh()
            rows = sh.worksheet('공휴일').get_all_values()[1:]
            return {r[0].strip(): r[1].strip()
                    for r in rows if len(r) >= 2 and r[0].strip()}
        except Exception:
            return {}
    return _cached('managed_hols', _f, ttl=3600)

def get_day_label(ds: str):
    """Returns '' for workday, '토'/'일'/holiday_name for non-workday."""
    try:
        d = date.fromisoformat(ds)
    except Exception:
        return ''
    wd = d.weekday()
    if wd == 5: return '토'
    if wd == 6: return '일'
    name = BASE_HOLIDAYS.get(ds) or get_managed_holidays().get(ds, '')
    return name

def _week_range(ref_ds=None):
    ref = date.fromisoformat(ref_ds) if ref_ds else date.today()
    mon = ref - timedelta(days=ref.weekday())
    return mon, mon + timedelta(days=6)

def get_week_att(ref_ds=None):
    mon, sun = _week_range(ref_ds)
    months = set()
    d = mon
    while d <= sun:
        months.add((d.year, d.month))
        d += timedelta(days=1)
    all_recs = []
    for yr, mo in months:
        all_recs.extend(get_att_records(yr, mo))
    week_dates = set()
    d = mon
    while d <= sun:
        week_dates.add(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    return [r for r in all_recs if str(r.get('일자', '')).strip() in week_dates], mon, sun

def _ot_time(hours):
    """Convert OT hours float to HH:MM start/end (assumes 08:00 start)."""
    try:
        h = float(hours)
    except Exception:
        h = 0.0
    start_min = 8 * 60
    work_min  = int(h * 60)
    lunch_min = 60 if h > 4 else 0
    end_min   = start_min + work_min + lunch_min
    def fmt(m):
        return f'{m // 60:02d}:{m % 60:02d}'
    return fmt(start_min), fmt(end_min)

# ── 라우트 ──────────────────────────────────────────────────────
@app.route('/')
def index():
    today = date.today()
    ds    = today.strftime('%Y-%m-%d')
    year, month = today.year, today.month
    try:
        emps     = get_employees()
        att      = get_att_records(year, month)
        meal     = get_meal_today(ds)
        ot_rows  = calc_ot_status(emps, att, year, month)
        n_warn   = sum(1 for r in ot_rows if r['status'] == 'warn')
        n_danger = sum(1 for r in ot_rows if r['status'] == 'danger')
        n_max64  = sum(1 for r in ot_rows if r['status'] == 'max64')
        total_ot = sum(r['total_ot'] for r in ot_rows)

        lunch_req  = meal['중식신청']
        lunch_real = [r for r in meal['중식실식수']
                      if len(r) > 2 and r[2].strip() == '중식']
        dinner     = meal['저녁도시락']
        wkend      = meal['특근식사']
        guests     = meal['외부손님']
        today_menu = get_today_menu(ds)

        return render_template('index.html',
            today   = today.strftime('%Y년 %m월 %d일'),
            weekday = '월화수목금토일'[today.weekday()],
            year=year, month=month,
            n_emps   = len(emps),
            total_ot = int(total_ot),
            n_warn=n_warn, n_danger=n_danger, n_max64=n_max64,
            today_menu   = today_menu,
            n_lunch_req  = len(lunch_req),
            n_lunch_real = len(lunch_real),
            n_dinner     = len(dinner),
            n_wkend      = len(wkend),
            n_guest      = len(guests),
            updated = datetime.now().strftime('%H:%M'),
        )
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/overtime')
def overtime():
    today   = date.today()
    year    = int(request.args.get('year',  today.year))
    month   = int(request.args.get('month', today.month))
    factory = request.args.get('fac', '전체')
    try:
        emps = get_employees()
        if factory == '1공장':
            emps = [e for e in emps if str(e.get('사원번호','')).startswith('M')]
        elif factory == '2공장':
            emps = [e for e in emps if str(e.get('사원번호','')).startswith('S')]

        att = get_att_records(year, month)
        if factory != '전체':
            eids = {str(e.get('사원번호','')).strip() for e in emps}
            att  = [r for r in att if str(r.get('사원번호','')).strip() in eids]

        ot_rows  = calc_ot_status(emps, att, year, month)
        n_max64  = sum(1 for r in ot_rows if r['status'] == 'max64')
        n_danger = sum(1 for r in ot_rows if r['status'] == 'danger')
        n_warn   = sum(1 for r in ot_rows if r['status'] == 'warn')
        n_safe   = sum(1 for r in ot_rows if r['status'] == 'safe')

        # 이전/다음 월 계산
        prev_m = month - 1 if month > 1 else 12
        prev_y = year if month > 1 else year - 1
        next_m = month + 1 if month < 12 else 1
        next_y = year if month < 12 else year + 1

        return render_template('overtime.html',
            ot_rows=ot_rows, year=year, month=month, factory=factory,
            n_total=len(emps),
            n_max64=n_max64, n_danger=n_danger, n_warn=n_warn, n_safe=n_safe,
            prev_y=prev_y, prev_m=prev_m,
            next_y=next_y, next_m=next_m,
            updated=datetime.now().strftime('%H:%M'),
        )
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/meal')
def meal():
    today = date.today()
    ds    = request.args.get('date', today.strftime('%Y-%m-%d'))
    try:
        meal_data = get_meal_today(ds)

        lunch_req  = meal_data['중식신청']
        lunch_real = [r for r in meal_data['중식실식수']
                      if len(r) > 2 and r[2].strip() == '중식']
        dinner_m   = [r for r in meal_data['저녁도시락']
                      if len(r) <= 6 or r[6].strip() != '여']
        dinner_f   = [r for r in meal_data['저녁도시락']
                      if len(r) > 6 and r[6].strip() == '여']
        wkend_g    = [r for r in meal_data['특근식사']
                      if len(r) > 6 and r[6].strip() == '구내식당']
        wkend_c    = [r for r in meal_data['특근식사']
                      if len(r) > 6 and r[6].strip() == '중국집']
        guests     = meal_data['외부손님']

        today_menu = get_today_menu(ds)
        return render_template('meal.html',
            ds=ds,
            today_menu=today_menu,
            lunch_req=lunch_req, lunch_real=lunch_real,
            dinner_m=dinner_m, dinner_f=dinner_f,
            wkend_g=wkend_g, wkend_c=wkend_c,
            guests=guests,
            updated=datetime.now().strftime('%H:%M'),
        )
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/menu', methods=['GET', 'POST'])
def menu_edit():
    today = date.today()
    ds    = today.strftime('%Y-%m-%d')
    error = ''
    saved = False

    # 로그인 처리
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'login':
            if request.form.get('pw') == MENU_PW:
                session['menu_auth'] = True
            else:
                error = '비밀번호가 틀렸습니다.'
        elif action == 'save' and session.get('menu_auth'):
            menu_text = request.form.get('menu', '').strip()
            if menu_text:
                try:
                    save_today_menu(ds, menu_text)
                    saved = True
                except Exception as e:
                    error = str(e)
        elif action == 'logout':
            session.pop('menu_auth', None)
            return redirect(url_for('menu_edit'))

    current_menu = get_today_menu(ds) if session.get('menu_auth') else ''
    return render_template('menu_edit.html',
        ds=ds,
        authed=session.get('menu_auth', False),
        current_menu=current_menu,
        error=error,
        saved=saved,
        weekday='월화수목금토일'[today.weekday()],
    )


@app.route('/ot_schedule')
def ot_schedule():
    today  = date.today()
    ds     = today.strftime('%Y-%m-%d')
    ref_ds = request.args.get('week', ds)

    try:
        # ── 금일 잔업/특근 ─────────────────────────────
        today_recs = get_att_records(today.year, today.month)
        today_jn = [r for r in today_recs
                    if str(r.get('일자', '')).strip() == ds
                    and r.get('근태유형', '') == '잔업']
        today_sp = [r for r in today_recs
                    if str(r.get('일자', '')).strip() == ds
                    and r.get('근태유형', '') == '특근']

        # ── 금주 휴일 특근 일정 ─────────────────────────
        week_recs, mon, sun = get_week_att(ref_ds)

        # 특근 중 휴일(토/일/공휴일)인 날만
        hol_sp = []
        for r in week_recs:
            if r.get('근태유형', '') != '특근':
                continue
            ds_r  = str(r.get('일자', '')).strip()
            label = get_day_label(ds_r)
            if label:
                hol_sp.append(dict(r, _label=label))

        # 날짜별 그룹핑
        grouped = defaultdict(list)
        for r in sorted(hol_sp, key=lambda x: str(x.get('일자', ''))):
            grouped[str(r.get('일자', ''))].append(r)

        # 주간 사원별 총OT (주40H 초과시간 계산용)
        emp_week_ot = defaultdict(float)
        for r in week_recs:
            if r.get('근태유형', '') in ATT_CAT_OT:
                try:
                    emp_week_ot[str(r.get('사원번호', '')).strip()] += float(r.get('값', 0) or 0)
                except Exception:
                    pass

        return render_template('ot_schedule.html',
            today_ds      = ds,
            today_weekday = '월화수목금토일'[today.weekday()],
            today_jn      = today_jn,
            today_sp      = today_sp,
            grouped_sp    = dict(grouped),
            mon = mon.strftime('%Y-%m-%d'),
            sun = sun.strftime('%Y-%m-%d'),
            ref_ds        = ref_ds,
            emp_week_ot   = dict(emp_week_ot),
            updated       = datetime.now().strftime('%H:%M'),
        )
    except Exception as e:
        return render_template('error.html', error=str(e))


def _ot_week_data(ref_ds):
    """미리보기/내보내기 공통 데이터. (hol_sp, emp_week_ot, mon, sun) 반환."""
    week_recs, mon, sun = get_week_att(ref_ds)
    hol_sp = []
    for r in week_recs:
        if r.get('근태유형', '') != '특근':
            continue
        ds_r  = str(r.get('일자', '')).strip()
        label = get_day_label(ds_r)
        if label:
            hol_sp.append(dict(r, _label=label))
    hol_sp.sort(key=lambda x: str(x.get('일자', '')))
    emp_week_ot = defaultdict(float)
    for r in week_recs:
        if r.get('근태유형', '') in ATT_CAT_OT:
            try:
                emp_week_ot[str(r.get('사원번호', '')).strip()] += float(r.get('값', 0) or 0)
            except Exception:
                pass
    return hol_sp, dict(emp_week_ot), mon, sun


@app.route('/ot_schedule/preview')
def ot_schedule_preview():
    ref_ds  = request.args.get('week', date.today().strftime('%Y-%m-%d'))
    fac     = request.args.get('fac', 'MJ')   # 'MJ' or 'SCS'
    appr_err= request.args.get('appr_err', '')
    appr_ok = request.args.get('appr_ok', '')
    if fac not in ('MJ', 'SCS'):
        fac = 'MJ'
    fac_prefix = 'M' if fac == 'MJ' else 'S'

    try:
        hol_sp, emp_week_ot, mon, sun = _ot_week_data(ref_ds)

        # 공장별 필터
        hol_sp = [r for r in hol_sp
                  if str(r.get('사원번호', '')).strip().upper().startswith(fac_prefix.upper())]

        # 날짜 목록 (unique, sorted)
        unique_dates = sorted(set(str(r.get('일자', '')).strip() for r in hol_sp))

        # GL 이름 — query param gl_YYYYMMDD=이름
        date_labels = []
        for ds in unique_dates:
            gl = request.args.get(f'gl_{ds.replace("-","")}', '')
            try:
                d_obj = date.fromisoformat(ds)
                label = f'{d_obj.strftime("%m/%d")} ({get_day_label(ds)})'
            except Exception:
                label = ds
            date_labels.append({'ds': ds, 'label': label, 'gl': gl})

        # 사람별 피벗
        person_map: dict = {}
        for r in hol_sp:
            ds_r  = str(r.get('일자', '')).strip()
            eid   = str(r.get('사원번호', '')).strip()
            value = r.get('값', 0)
            ts, te = _ot_time(value)
            note   = str(r.get('비고', '')).strip()
            if eid not in person_map:
                person_map[eid] = {
                    'dept': str(r.get('부서명', '')).strip(),
                    'name': str(r.get('성명', '')).strip(),
                    'task': note,
                    'days': {},
                }
            person_map[eid]['days'][ds_r] = f'{ts}~{te}'
            if note and note not in person_map[eid]['task']:
                sep = ', ' if person_map[eid]['task'] else ''
                person_map[eid]['task'] += sep + note

        # 부서 순서 (등장 순서 기준)
        dept_order: list = []
        for p in person_map.values():
            if p['dept'] not in dept_order:
                dept_order.append(p['dept'])

        persons = sorted(person_map.items(),
                         key=lambda x: (dept_order.index(x[1]['dept'])
                                        if x[1]['dept'] in dept_order else 999,
                                        x[1]['name']))

        # rowspan 계산 (dept 연속 그룹)
        rows = []
        no = 1
        i = 0
        while i < len(persons):
            dept = persons[i][1]['dept']
            j = i + 1
            while j < len(persons) and persons[j][1]['dept'] == dept:
                j += 1
            span = j - i
            for k in range(i, j):
                _, p = persons[k]
                rows.append({
                    'no':        no,
                    'dept':      dept,
                    'dept_span': span if k == i else 0,
                    'name':      p['name'],
                    'task':      p['task'],
                    'days':      p['days'],
                })
                no += 1
            i = j

        # 날짜별 인원 합계
        totals = {ds: sum(1 for _, p in persons if ds in p['days'])
                  for ds in unique_dates}

        # title 생성
        if unique_dates:
            d0 = date.fromisoformat(unique_dates[0])
            d1 = date.fromisoformat(unique_dates[-1])
            period = f'{d0.strftime("%m/%d")}~{d1.strftime("%m/%d")}'
        else:
            period = f'{mon.strftime("%m/%d")}~{sun.strftime("%m/%d")}'
        doc_title = f'{mon.strftime("%m")}월 특근 근무자 명단 ({period})'

        # 결재 상태
        week_key = mon.strftime('%Y-%m-%d')
        appr     = get_approval(week_key, fac)
        raw_cfg  = APPROVAL_CFG.get(fac, {})
        # 비밀번호 제외하고 템플릿에 전달
        appr_cfg = {k: {ik: iv for ik, iv in v.items() if ik != 'pw'}
                    for k, v in raw_cfg.items()}

        # 도장 이미지 base64 인코딩 (없으면 None)
        def _stamp_b64(name):
            try:
                p = os.path.join(os.path.dirname(__file__), 'static', 'stamps', f'{name}.png')
                with open(p, 'rb') as f:
                    return base64.b64encode(f.read()).decode()
            except Exception:
                return None

        grp_stamp = _stamp_b64(appr_cfg.get('grp', {}).get('stamp', '')) if appr_cfg else None
        ceo_stamp = _stamp_b64(appr_cfg.get('ceo', {}).get('stamp', '')) if appr_cfg else None

        return render_template('ot_preview.html',
            rows        = rows,
            date_labels = date_labels,
            unique_dates= unique_dates,
            totals      = totals,
            doc_title   = doc_title,
            mon         = mon,
            sun         = sun,
            ref_ds      = ref_ds,
            fac         = fac,
            week_key    = week_key,
            appr        = appr,
            appr_cfg    = appr_cfg,
            grp_stamp   = grp_stamp,
            ceo_stamp   = ceo_stamp,
            appr_err    = appr_err,
            appr_ok     = appr_ok,
            today_str   = date.today().strftime('%Y-%m-%d'),
        )
    except Exception as e:
        return render_template('error.html', error=str(e))


@app.route('/ot_schedule/export')
def ot_schedule_export():
    ref_ds = request.args.get('week', date.today().strftime('%Y-%m-%d'))
    fac    = request.args.get('fac', 'MJ')
    if fac not in ('MJ', 'SCS'):
        fac = 'MJ'
    fac_prefix = 'M' if fac == 'MJ' else 'S'
    try:
        from openpyxl import Workbook
        from openpyxl.styles import (Font, PatternFill, Alignment,
                                     Border, Side, GradientFill)
        from openpyxl.utils import get_column_letter

        hol_sp, emp_week_ot, mon, sun = _ot_week_data(ref_ds)
        hol_sp = [r for r in hol_sp
                  if str(r.get('사원번호', '')).strip().upper().startswith(fac_prefix.upper())]

        wb = Workbook()
        ws = wb.active
        ws.title = '특근신청서'

        # 컬럼 너비
        col_w = [6, 16, 10, 10, 14, 10, 30, 14]
        for i, w in enumerate(col_w, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        # 스타일 헬퍼
        def bd(style='thin'):
            s = Side(style=style)
            return Border(left=s, right=s, top=s, bottom=s)
        def fill(hex_color):
            return PatternFill('solid', fgColor=hex_color)
        def aln(h='center', v='center', wrap=False):
            return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

        # ── 타이틀 ─────────────────────────────────────────
        ws.row_dimensions[1].height = 8
        ws.row_dimensions[2].height = 32
        ws.row_dimensions[3].height = 20
        ws.row_dimensions[4].height = 8

        ws.merge_cells('A2:H2')
        tc = ws['A2']
        tc.value     = 'MJT 주식회사  휴일 특근 신청서'
        tc.font      = Font(size=18, bold=True, color='1A237E')
        tc.alignment = aln('center')

        ws.merge_cells('A3:H3')
        dc = ws['A3']
        period_str = f'{mon.strftime("%Y년 %m월 %d일")} ~ {sun.strftime("%m월 %d일")}  |  작성일 {date.today().strftime("%Y-%m-%d")}'
        dc.value     = period_str
        dc.font      = Font(size=10, color='555555')
        dc.alignment = aln('center')

        # ── 헤더 행 ────────────────────────────────────────
        HDR_ROW = 5
        ws.row_dimensions[HDR_ROW].height = 22
        headers = ['No', '근무일', '시작시간', '종료시간', '공정(부서)', '성명', '근로 사유', '주40H 초과시간']
        hdr_fill = fill('1565C0')
        for col, h in enumerate(headers, 1):
            c = ws.cell(HDR_ROW, col, h)
            c.font      = Font(bold=True, color='FFFFFF', size=10)
            c.fill      = hdr_fill
            c.alignment = aln('center')
            c.border    = bd()

        # ── 데이터 행 ───────────────────────────────────────
        DATE_COLORS = ['E8F5E9', 'E3F2FD', 'FFF3E0', 'F3E5F5', 'FCE4EC', 'E0F7FA']
        date_color_map = {}
        color_idx = 0

        row_num = HDR_ROW + 1
        no = 1
        for r in hol_sp:
            ds_r   = str(r.get('일자', '')).strip()
            label  = r.get('_label', '')
            dept   = str(r.get('부서명', '')).strip()
            name   = str(r.get('성명', '')).strip()
            value  = r.get('값', 0)
            note   = str(r.get('비고', '')).strip()
            eid    = str(r.get('사원번호', '')).strip()
            week_h = emp_week_ot.get(eid, float(value or 0))

            try:
                d_obj = date.fromisoformat(ds_r)
                day_str = f'{d_obj.strftime("%m/%d")} ({label})'
            except Exception:
                day_str = ds_r

            t_start, t_end = _ot_time(value)

            if ds_r not in date_color_map:
                date_color_map[ds_r] = DATE_COLORS[color_idx % len(DATE_COLORS)]
                color_idx += 1
            row_fill = fill(date_color_map[ds_r])

            ws.row_dimensions[row_num].height = 18
            row_data = [no, day_str, t_start, t_end, dept, name, note,
                        f'{week_h:.0f}H']
            for col, val in enumerate(row_data, 1):
                c = ws.cell(row_num, col, val)
                c.fill      = row_fill
                c.border    = bd()
                c.font      = Font(size=10)
                c.alignment = aln('center' if col != 7 else 'left', wrap=True)
            row_num += 1
            no += 1

        if no == 1:
            ws.merge_cells(f'A{row_num}:H{row_num}')
            c = ws.cell(row_num, 1, '해당 기간 휴일 특근 기록 없음')
            c.alignment = aln('center')
            c.font      = Font(italic=True, color='888888')
            row_num += 1

        # ── 결재 행 ────────────────────────────────────────
        row_num += 1
        ws.row_dimensions[row_num].height = 18
        ws.merge_cells(f'A{row_num}:B{row_num}')
        ws.cell(row_num, 1, '담당').font = Font(bold=True, size=10)
        ws.cell(row_num, 1).alignment    = aln('center')
        ws.cell(row_num, 1).border       = bd()
        ws.merge_cells(f'C{row_num}:D{row_num}')
        ws.cell(row_num, 3, '팀장').font  = Font(bold=True, size=10)
        ws.cell(row_num, 3).alignment     = aln('center')
        ws.cell(row_num, 3).border        = bd()
        ws.merge_cells(f'E{row_num}:F{row_num}')
        ws.cell(row_num, 5, '부문장').font = Font(bold=True, size=10)
        ws.cell(row_num, 5).alignment      = aln('center')
        ws.cell(row_num, 5).border         = bd()
        ws.merge_cells(f'G{row_num}:H{row_num}')
        ws.cell(row_num, 7, '대표이사').font = Font(bold=True, size=10)
        ws.cell(row_num, 7).alignment        = aln('center')
        ws.cell(row_num, 7).border           = bd()
        row_num += 1
        ws.row_dimensions[row_num].height = 40
        for col in [1, 3, 5, 7]:
            end = col + 1
            ws.merge_cells(f'{get_column_letter(col)}{row_num}:{get_column_letter(end)}{row_num}')
            ws.cell(row_num, col, '').border = bd()

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        fname = f'휴일특근신청서_{mon.strftime("%Y%m%d")}.xlsx'
        return send_file(buf, download_name=fname,
                         as_attachment=True,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        return render_template('error.html', error=str(e))


# ── 결재 함수 ────────────────────────────────────────────────────
_APPR_SHEET = '특근결재'
# 컬럼: 주차키|공장|상태|1차자|1차시|1차결|1차사유|최종자|최종시|최종결|최종사유

def get_approval(week_key: str, factory: str) -> dict:
    def _f():
        try:
            sh   = _open_sh()
            rows = sh.worksheet(_APPR_SHEET).get_all_values()[1:]
            for r in rows:
                if len(r) >= 2 and r[0].strip() == week_key and r[1].strip() == factory:
                    def _g(i): return r[i].strip() if len(r) > i else ''
                    return {'week_key': _g(0), 'factory': _g(1), 'status': _g(2) or '대기',
                            'grp_name': _g(3), 'grp_dt': _g(4), 'grp_dec': _g(5), 'grp_reason': _g(6),
                            'ceo_name': _g(7), 'ceo_dt': _g(8), 'ceo_dec': _g(9), 'ceo_reason': _g(10)}
        except Exception:
            pass
        return {'week_key': week_key, 'factory': factory, 'status': '대기',
                'grp_name':'','grp_dt':'','grp_dec':'','grp_reason':'',
                'ceo_name':'','ceo_dt':'','ceo_dec':'','ceo_reason':''}
    return _cached(f'appr_{week_key}_{factory}', _f, ttl=60)

def _invalidate_approval(week_key, factory):
    with _lock:
        _cache.pop(f'appr_{week_key}_{factory}', None)

def save_approval(week_key, factory, level, decision, reason, approver_name):
    sh   = _open_sh()
    ws   = sh.worksheet(_APPR_SHEET)
    now  = datetime.now().strftime('%Y-%m-%d %H:%M')
    rows = ws.get_all_values()
    row_idx = None
    for i, r in enumerate(rows[1:], start=2):
        if len(r) >= 2 and r[0].strip() == week_key and r[1].strip() == factory:
            row_idx = i; break

    if level == 'grp':
        new_status = '1차완료' if decision == '승인' else '반려'
        if row_idx:
            ws.update(range_name=f'C{row_idx}:G{row_idx}',
                      values=[[new_status, approver_name, now, decision, reason]])
        else:
            ws.append_row([week_key, factory, new_status,
                           approver_name, now, decision, reason, '', '', '', ''],
                          value_input_option='USER_ENTERED')
    else:  # ceo
        new_status = '최종승인' if decision == '승인' else '반려'
        if row_idx:
            ws.update(range_name=f'C{row_idx}', values=[[new_status]])
            ws.update(range_name=f'H{row_idx}:K{row_idx}',
                      values=[[approver_name, now, decision, reason]])
        else:
            ws.append_row([week_key, factory, new_status,
                           '', '', '', '', approver_name, now, decision, reason],
                          value_input_option='USER_ENTERED')
    _invalidate_approval(week_key, factory)


@app.route('/ot_schedule/approve', methods=['POST'])
def ot_approve():
    week_key = request.form.get('week_key', '')
    factory  = request.form.get('factory', 'MJ')
    level    = request.form.get('level', 'grp')
    decision = request.form.get('decision', '승인')
    pw       = request.form.get('pw', '')
    reason   = request.form.get('reason', '').strip()
    ref_ds   = request.form.get('ref_ds', week_key)
    fac      = request.form.get('fac', factory)

    # 돌아갈 URL 구성
    back = f'/ot_schedule/preview?week={ref_ds}&fac={fac}'
    for key, val in request.form.items():
        if key.startswith('gl_'):
            back += f'&{key}={val}'

    if factory not in APPROVAL_CFG or level not in APPROVAL_CFG[factory]:
        return redirect(back + '&appr_err=설정오류')

    correct_pw = APPROVAL_CFG[factory][level]['pw']
    if not correct_pw:
        return redirect(back + '&appr_err=비밀번호미설정')
    if pw != correct_pw:
        return redirect(back + '&appr_err=비밀번호오류')

    state = get_approval(week_key, factory)
    status = state['status']
    if level == 'grp' and status not in ('대기', '반려'):
        return redirect(back + '&appr_err=이미1차처리됨')
    if level == 'ceo' and status != '1차완료':
        return redirect(back + '&appr_err=1차결재필요')

    name = APPROVAL_CFG[factory][level]['name']
    try:
        save_approval(week_key, factory, level, decision, reason, name)
    except Exception as e:
        return redirect(back + f'&appr_err={str(e)[:30]}')

    return redirect(back + '&appr_ok=1')


def get_company_events(year, month):
    def _f():
        try:
            sh   = _open_sh()
            rows = sh.worksheet('회사일정').get_all_values()[1:]
            result = []
            for r in rows:
                if len(r) < 3 or not r[0].strip():
                    continue
                ds = r[0].strip()
                if not (ds.startswith(f'{year}-{month:02d}') or
                        ds.startswith(f'{year}-{str(month).zfill(2)}')):
                    continue
                result.append({'ds': ds, 'type': r[1].strip(),
                                'content': r[2].strip(),
                                'note': r[3].strip() if len(r) > 3 else ''})
            return result
        except Exception:
            return []
    return _cached(f'events_{year}_{month}', _f, ttl=300)

def save_company_event(ds, ev_type, content, note=''):
    sh = _open_sh()
    ws = sh.worksheet('회사일정')
    now_s = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    ws.append_row([ds, ev_type, content, note, now_s, '웹'], value_input_option='USER_ENTERED')
    with _lock:
        try:
            yr, mo = int(ds[:4]), int(ds[5:7])
            _cache.pop(f'events_{yr}_{mo}', None)
        except Exception:
            pass

def delete_company_event(ds, content):
    sh  = _open_sh()
    ws  = sh.worksheet('회사일정')
    all = ws.get_all_values()
    for i, row in enumerate(all[1:], start=2):
        if len(row) >= 3 and row[0].strip() == ds and row[2].strip() == content:
            ws.delete_rows(i)
            with _lock:
                try:
                    yr, mo = int(ds[:4]), int(ds[5:7])
                    _cache.pop(f'events_{yr}_{mo}', None)
                except Exception:
                    pass
            return True
    return False


@app.route('/calendar', methods=['GET', 'POST'])
def calendar_view():
    today = date.today()
    year  = int(request.args.get('year',  today.year))
    month = int(request.args.get('month', today.month))
    error = ''
    saved = False

    if request.method == 'POST':
        if request.form.get('pw') == MENU_PW:
            action = request.form.get('action', '')
            if action == 'add':
                ds      = request.form.get('ds', '').strip()
                ev_type = request.form.get('type', '행사').strip()
                content = request.form.get('content', '').strip()
                note    = request.form.get('note', '').strip()
                if ds and content:
                    try:
                        save_company_event(ds, ev_type, content, note)
                        saved = True
                    except Exception as e:
                        error = str(e)
                else:
                    error = '날짜와 내용을 입력해주세요.'
            elif action == 'delete':
                ds      = request.form.get('ds', '').strip()
                content = request.form.get('content', '').strip()
                try:
                    delete_company_event(ds, content)
                    saved = True
                except Exception as e:
                    error = str(e)
        else:
            error = '비밀번호가 틀렸습니다.'

    events    = get_company_events(year, month)
    ev_by_day = defaultdict(list)
    for ev in events:
        try:
            day = int(ev['ds'].split('-')[2])
            ev_by_day[day].append(ev)
        except Exception:
            pass

    import calendar as _cal
    first_wd, days_in_month = _cal.monthrange(year, month)
    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    next_m = month + 1 if month < 12 else 1
    next_y = year if month < 12 else year + 1

    # 달력 주 구성 (일~토 순서, 일요일 시작)
    # first_wd: Mon=0 ... Sun=6  →  offset: Sun=0, so offset=(first_wd+1)%7
    offset = (first_wd + 1) % 7
    weeks  = []
    week   = [None] * offset
    for d in range(1, days_in_month + 1):
        week.append(d)
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))

    ev_types = ['공휴일', '회의', '행사', '생산', '점검', '기타']
    ev_colors = {
        '공휴일': '#FEE2E2', '회의': '#DBEAFE', '행사': '#D1FAE5',
        '생산': '#FFF3E0', '점검': '#EDE9FE', '기타': '#F3F4F6',
    }

    return render_template('calendar.html',
        year=year, month=month,
        weeks=weeks, ev_by_day=dict(ev_by_day),
        ev_colors=ev_colors, ev_types=ev_types,
        today=today, prev_y=prev_y, prev_m=prev_m,
        next_y=next_y, next_m=next_m,
        error=error, saved=saved,
        updated=datetime.now().strftime('%H:%M'),
    )


@app.route('/api/refresh')
def api_refresh():
    _clear_cache()
    return jsonify({'ok': True, 'ts': datetime.now().strftime('%H:%M:%S')})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
