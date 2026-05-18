"""
MJT 모바일 현황 대시보드 — Flask
Google Sheets 데이터를 모바일 브라우저로 조회 + 오늘 메뉴 등록
"""
import os, json, base64, time, threading, secrets
from datetime import datetime, date
from flask import Flask, render_template, jsonify, request, redirect, url_for, session

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))

# 식당 메뉴 등록 비밀번호 (환경변수 MENU_PW 로 변경 가능, 기본값 3838)
MENU_PW = os.environ.get('MENU_PW', '3838')

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


@app.route('/api/refresh')
def api_refresh():
    _clear_cache()
    return jsonify({'ok': True, 'ts': datetime.now().strftime('%H:%M:%S')})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
