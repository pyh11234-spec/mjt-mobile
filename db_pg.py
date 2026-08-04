"""
web_app용 PostgreSQL (Supabase) 연결 헬퍼.
환경변수 SUPABASE_DB_URL 필요 (render.com → Environment 에 등록).
"""
import os, threading
from contextlib import contextmanager
from datetime import date as _date, timezone as _timezone, timedelta as _timedelta

_KST = _timezone(_timedelta(hours=9))


def _to_kst(v):
    """timestamptz(절대시각, UTC) → 한국시간으로 변환. naive/date는 그대로."""
    if getattr(v, 'tzinfo', None) is not None:
        try:
            return v.astimezone(_KST)
        except Exception:
            return v
    return v

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    PG_OK = True
except ImportError:
    PG_OK = False

_pool = None
_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                url = os.environ.get('SUPABASE_DB_URL', '').strip()
                if not url:
                    raise RuntimeError('SUPABASE_DB_URL 환경변수 미설정')
                _pool = ConnectionPool(
                    conninfo=url, min_size=1, max_size=5, timeout=10,
                    kwargs={'row_factory': dict_row}
                )
    return _pool


def is_available() -> bool:
    """PG 연결 가능 여부 (render 환경변수 안 됐을 때 graceful fallback)."""
    if not PG_OK:
        return False
    return bool(os.environ.get('SUPABASE_DB_URL', '').strip())


@contextmanager
def cursor():
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit()


def query(sql, params=None):
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql, params=None):
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql, params=None) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


# ── 관리 공휴일(holidays) — 데스크탑 meal_repo와 동일 테이블(SSoT 통일 2026-07-13) ──
def get_managed_holidays() -> dict:
    return {(r['holiday_date'].isoformat() if hasattr(r['holiday_date'], 'isoformat') else r['holiday_date']): (r['name'] or '')
            for r in query("SELECT holiday_date, name FROM holidays")}


def add_managed_holiday(date_str: str, name: str) -> None:
    execute(
        "INSERT INTO holidays (holiday_date, name) VALUES (%s,%s) "
        "ON CONFLICT (holiday_date) DO UPDATE SET name=EXCLUDED.name",
        (date_str, name))


def delete_managed_holiday(date_str: str) -> None:
    execute("DELETE FROM holidays WHERE holiday_date=%s", (date_str,))


# ══════════════════════════════════════════════════════════════════
# 식수(급식) 데이터 — Supabase SSoT.  (데스크탑 meal_repo와 동일 역할)
# 읽기는 기존 템플릿/소비처가 위치색인을 쓰므로 Sheets와 동일한 컬럼 순서의
# list 로 반환한다(소비처 무수정). 쓰기는 테이블에 직접 INSERT.
# ══════════════════════════════════════════════════════════════════
def _hms(v) -> str:
    v = _to_kst(v)
    return v.strftime('%H:%M:%S') if hasattr(v, 'strftime') else (str(v) if v else '')


def _ymd(v) -> str:
    v = _to_kst(v)
    return v.strftime('%Y-%m-%d') if hasattr(v, 'strftime') else (str(v) if v else '')


def meal_today(ds: str) -> dict:
    """ds(YYYY-MM-DD)의 식수 현황. 각 값은 Sheets와 동일 위치배열 list."""
    out = {'중식신청': [], '중식실식수': [], '저녁도시락': [], '특근식사': [], '외부손님': []}
    for r in query("SELECT req_date,req_time,factory,emp_id,emp_name,dept,status "
                   "FROM lunch_requests WHERE req_date=%s ORDER BY req_time", (ds,)):
        out['중식신청'].append([_ymd(r['req_date']), _hms(r['req_time']), r['factory'] or '',
                              r['emp_id'] or '', r['emp_name'] or '', r['dept'] or '', r['status'] or ''])
    for r in query("SELECT actual_date,actual_time,type,emp_id,emp_name,dept,rank,factory,"
                   "mgr_emp_id,mgr_name FROM lunch_actuals WHERE actual_date=%s ORDER BY actual_time", (ds,)):
        typ = r['type'] or ''
        # Sheets 위치 계약: 중식 → [..,직급,'',공장] / 외부손님 → [..,'',담당자사번,담당자명]
        if typ == '중식':
            col6, col7, col8 = r['rank'] or '', '', r['factory'] or ''
        else:
            col6, col7, col8 = '', r['mgr_emp_id'] or '', r['mgr_name'] or ''
        out['중식실식수'].append([_ymd(r['actual_date']), _hms(r['actual_time']), typ,
                               r['emp_id'] or '', r['emp_name'] or '', r['dept'] or '',
                               col6, col7, col8])
    for r in query("SELECT req_date,req_time,emp_id,emp_name,dept,rank,gender,reason "
                   "FROM dinner_requests WHERE req_date=%s ORDER BY req_time", (ds,)):
        out['저녁도시락'].append([_ymd(r['req_date']), _hms(r['req_time']), r['emp_id'] or '',
                              r['emp_name'] or '', r['dept'] or '', r['rank'] or '',
                              r['gender'] or '', r['reason'] or ''])
    for r in query("SELECT meal_date,req_time,emp_id,emp_name,dept,rank,mode,menu,price,co_pay,"
                   "per_pay,headcount,memo,no_meal FROM weekend_meals WHERE meal_date=%s ORDER BY req_time", (ds,)):
        out['특근식사'].append([_ymd(r['meal_date']), _hms(r['req_time']), r['emp_id'] or '',
                             r['emp_name'] or '', r['dept'] or '', r['rank'] or '', r['mode'] or '',
                             r['menu'] or '', r['price'] or 0, r['co_pay'] or 0, r['per_pay'] or 0,
                             r['headcount'] or 1, r['memo'] or '', 'Y' if r['no_meal'] else ''])
    for r in query("SELECT visit_date,reg_time,mgr_emp_id,mgr_name,mgr_dept,company,guest_name,"
                   "reason,person_count FROM guests WHERE visit_date=%s ORDER BY reg_time", (ds,)):
        out['외부손님'].append([_ymd(r['visit_date']), _ymd(r['reg_time']), _hms(r['reg_time']),
                             r['mgr_emp_id'] or '', r['mgr_name'] or '', r['mgr_dept'] or '',
                             r['company'] or '', r['guest_name'] or '', r['reason'] or '',
                             r['person_count'] or 1])
    return out


def meal_expected(ds: str) -> dict:
    """특정일 예정 인원(신청 기준, 식당 준비용) — 중식신청·저녁도시락·특근식사·외부손님."""
    lunch = query_one("SELECT COUNT(*) c FROM lunch_requests WHERE req_date=%s", (ds,))
    dinner = query_one("SELECT COUNT(*) c FROM dinner_requests WHERE req_date=%s", (ds,))
    weekend = query_one("SELECT COALESCE(SUM(headcount),0) c FROM weekend_meals WHERE meal_date=%s", (ds,))
    guest = query_one("SELECT COALESCE(SUM(person_count),0) c FROM guests WHERE visit_date=%s", (ds,))
    lunch_c = (lunch or {}).get('c') or 0
    dinner_c = (dinner or {}).get('c') or 0
    weekend_c = (weekend or {}).get('c') or 0
    guest_c = (guest or {}).get('c') or 0
    return {'lunch': lunch_c, 'dinner': dinner_c, 'weekend': weekend_c, 'guest': guest_c,
            'total': lunch_c + dinner_c + weekend_c + guest_c}


def meal_range_summary(d_from: str, d_to: str) -> dict:
    """기간 내 일자별 식수 집계(실식수 기준) — 데스크탑 get_settlement_data()와 동일 카테고리
    (도시락 남/여, 특근 구내/도시락/배달 3분류)로 통일. 특근 co_pay/per_pay는 참고용이며
    데스크탑과 동일하게 정산 금액(total_amount)엔 포함하지 않는다(중식+도시락만 단가 적용)."""
    days = {}

    def _row(ds):
        return days.setdefault(ds, {'date': ds, 'lunch': 0, 'dinner_male': 0, 'dinner_female': 0,
                                     'wkg': 0, 'wkb': 0, 'wkd': 0, 'weekend_copay': 0,
                                     'weekend_perpay': 0, 'guest': 0})

    for r in query("SELECT actual_date, COUNT(*) c FROM lunch_actuals "
                   "WHERE type='중식' AND actual_date BETWEEN %s AND %s "
                   "GROUP BY actual_date", (d_from, d_to)):
        _row(_ymd(r['actual_date']))['lunch'] = r['c']
    for r in query("SELECT req_date, gender, COUNT(*) c FROM dinner_requests "
                   "WHERE req_date BETWEEN %s AND %s GROUP BY req_date, gender", (d_from, d_to)):
        d = _row(_ymd(r['req_date']))
        if (r['gender'] or '').strip() == '여':
            d['dinner_female'] += r['c']
        else:
            d['dinner_male'] += r['c']
    for r in query("SELECT meal_date, mode, COUNT(*) cnt, COALESCE(SUM(co_pay),0) cp, "
                   "COALESCE(SUM(per_pay),0) pp FROM weekend_meals "
                   "WHERE meal_date BETWEEN %s AND %s GROUP BY meal_date, mode", (d_from, d_to)):
        d = _row(_ymd(r['meal_date']))
        mode = (r['mode'] or '').strip()
        if mode == '구내식당':
            d['wkg'] += r['cnt']
        elif mode == '도시락':
            d['wkb'] += r['cnt']
        elif mode == '배달':
            d['wkd'] += r['cnt']
        d['weekend_copay'] += r['cp']; d['weekend_perpay'] += r['pp']
    for r in query("SELECT visit_date, COALESCE(SUM(person_count),0) c FROM guests "
                   "WHERE visit_date BETWEEN %s AND %s GROUP BY visit_date", (d_from, d_to)):
        _row(_ymd(r['visit_date']))['guest'] = r['c']

    rows = [days[k] for k in sorted(days.keys())]
    keys = ('lunch', 'dinner_male', 'dinner_female', 'wkg', 'wkb', 'wkd',
            'weekend_copay', 'weekend_perpay', 'guest')
    totals = {k: sum(r[k] for r in rows) for k in keys}
    return {'rows': rows, 'totals': totals}


def save_settlement(d_from, d_to, lunch_count, guest_count, lunch_price,
                     dinner_male_count, dinner_female_count, dinner_price,
                     wkg_count, wkb_count, wkd_count, weekend_copay, weekend_perpay,
                     created_by, biz_scope='전체') -> int:
    """정산 확정 저장 — (중식+외부손님)×중식단가 + 도시락×도시락단가 = 총액(데스크탑과 동일 규칙,
    특근 co_pay는 참고용으로만 저장·정산 총액엔 미포함). 결제여부는 미결제(False)로 시작."""
    total = ((lunch_count + guest_count) * lunch_price +
             (dinner_male_count + dinner_female_count) * dinner_price)
    with cursor() as cur:
        cur.execute(
            "INSERT INTO meal_settlements (d_from, d_to, lunch_count, guest_count, lunch_price, "
            "dinner_male_count, dinner_female_count, dinner_price, wkg_count, wkb_count, wkd_count, "
            "weekend_copay, weekend_perpay, biz_scope, total_amount, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (d_from, d_to, lunch_count, guest_count, lunch_price, dinner_male_count, dinner_female_count,
             dinner_price, wkg_count, wkb_count, wkd_count, weekend_copay, weekend_perpay, biz_scope,
             total, created_by))
        return cur.fetchone()['id']


def list_settlements(limit: int = 30) -> list:
    rows = query(
        "SELECT id, d_from, d_to, lunch_count, guest_count, lunch_price, dinner_male_count, "
        "dinner_female_count, dinner_price, wkg_count, wkb_count, wkd_count, weekend_copay, "
        "weekend_perpay, biz_scope, total_amount, paid, paid_at, paid_by, created_by, created_at, memo "
        "FROM meal_settlements ORDER BY d_from DESC, id DESC LIMIT %s", (limit,))
    out = []
    for r in rows:
        out.append({**r, 'd_from': _ymd(r['d_from']), 'd_to': _ymd(r['d_to']),
                    'paid_at': _ymd(r['paid_at']) if r['paid_at'] else '',
                    'created_at': _ymd(r['created_at']) if r['created_at'] else ''})
    return out


def toggle_settlement_paid(sid: int, paid: bool, by: str) -> None:
    if paid:
        execute("UPDATE meal_settlements SET paid=TRUE, paid_at=NOW(), paid_by=%s WHERE id=%s", (by, sid))
    else:
        execute("UPDATE meal_settlements SET paid=FALSE, paid_at=NULL, paid_by=NULL WHERE id=%s", (sid,))


def recent_export_logs(limit: int = 20) -> list:
    """식수 집계·정산 다운로드 로그(누가·언제·무엇을) — access_logs 재사용."""
    rows = query(
        "SELECT emp_id, path, accessed_at FROM access_logs "
        "WHERE path LIKE '/manage/stats%%' AND success=TRUE "
        "ORDER BY accessed_at DESC LIMIT %s", (limit,))
    emap = {}
    if rows:
        ids = tuple({r['emp_id'] for r in rows if r['emp_id']}) or ('',)
        for e in query("SELECT emp_id, name FROM employees WHERE emp_id = ANY(%s)", (list(ids),)):
            emap[e['emp_id']] = e['name']
    label = {'/manage/stats/export.xlsx': '엑셀(정산용)', '/manage/stats/export.png': '이미지(정산용)',
             '/manage/stats/expected.png': '식당 공유용 이미지'}
    out = []
    for r in rows:
        out.append({'emp_id': r['emp_id'], 'name': emap.get(r['emp_id'], r['emp_id']),
                     'kind': label.get(r['path'], r['path']),
                     'at': _to_kst(r['accessed_at']).strftime('%Y-%m-%d %H:%M') if r['accessed_at'] else ''})
    return out


def op_settings() -> dict:
    return {r['key']: r['value'] for r in query("SELECT key,value FROM op_settings")}


def today_menu(ds: str) -> str:
    r = query_one("SELECT menu FROM today_menus WHERE menu_date=%s", (ds,))
    return (r['menu'] if r else '') or ''


def delivery_vendors(active_only: bool = True) -> list:
    sql = "SELECT DISTINCT vendor FROM chinese_menus" + (" WHERE active=TRUE" if active_only else "") + " ORDER BY vendor"
    return [(r['vendor'] or '중국집') for r in query(sql)]


def delivery_menus(vendor: str = None, active_only: bool = True) -> list:
    sql = "SELECT name,price,vendor FROM chinese_menus WHERE TRUE"
    params = []
    if active_only:
        sql += " AND active=TRUE"
    if vendor:
        sql += " AND vendor=%s"; params.append(vendor)
    sql += " ORDER BY vendor,name"
    return [{'name': r['name'], 'price': r['price'] or 0, 'vendor': r['vendor'] or '중국집'}
            for r in query(sql, tuple(params))]


def chinese_menus(active_only: bool = True) -> list:   # 구 호환
    return delivery_menus(active_only=active_only)


def wkend_plan(from_date: str, limit: int = 8) -> list:
    """from_date 이후 운영하는 특근일(오전/오후 중 하나라도 '없음' 아님). checkin.html 호환."""
    rows = query("SELECT meal_date,am_mode,pm_mode,deadline,day_deadline,support,notice FROM weekend_settings "
                 "WHERE meal_date >= %s AND (COALESCE(am_mode,'없음')<>'없음' OR COALESCE(pm_mode,'없음')<>'없음') "
                 "ORDER BY meal_date LIMIT %s", (from_date, limit))
    out = []
    for r in rows:
        ds = _ymd(r['meal_date'])
        try:
            wd = '월화수목금토일'[_date.fromisoformat(ds).weekday()]
        except Exception:
            wd = ''
        out.append({'date': ds, 'weekday': wd, 'am_mode': r['am_mode'] or '없음',
                    'pm_mode': r['pm_mode'] or '없음', 'deadline': r['deadline'] or '',
                    'day_deadline': r.get('day_deadline') or '',
                    'support': r['support'] or 0, 'notice': r['notice'] or ''})
    return out


def wkend_setting(ds: str):
    r = query_one("SELECT am_mode,pm_mode,deadline,day_deadline,support,notice FROM weekend_settings WHERE meal_date=%s", (ds,))
    if not r:
        return None
    return {'am_mode': r['am_mode'] or '없음', 'pm_mode': r['pm_mode'] or '없음',
            'deadline': r['deadline'] or '', 'day_deadline': r.get('day_deadline') or '',
            'support': r['support'] or 0, 'notice': r['notice'] or ''}


def is_weekend_worker(ds: str, emp_id: str) -> bool:
    """ds에 att_type='특근' 으로 근태 등록된 근로자인지(특근식사 자격 게이트)."""
    return query_one("SELECT 1 FROM attendance_records "
                     "WHERE att_date=%s AND emp_id=%s AND att_type='특근' LIMIT 1",
                     (ds, emp_id)) is not None


# ── 특근/잔업/휴가 개인 신청 (모바일 자율등록, 2026-08-04) ──────────────────
# 팀장님 지시: "일단 하나씩" — 근태프로그램(attendance.py)의 add_att()과 완전히 동일한
# attendance_records 스키마를 그대로 재사용한다(새 테이블 안 만듦). 그래서 특근은 별도
# 연동 코드 없이도 기존 그룹장→사장 주간 결재 화면(approval.py, att_repo.appr_load_records
# — att_type+날짜범위로만 조회)에 자동으로 잡힌다. source='모바일신청'으로만 관리자
# 수기입력과 구분(조회 표시용, 로직 분기 없음).
MIN_REST_HOURS = 11.5  # attendance.py와 동일 상수(특별연장근로 인가 건강보호조치)


def add_att_request(year, month, ds, factory, dept, eid, name, rank,
                    att_type, value, note, source='모바일신청') -> bool:
    try:
        val = float(value) if str(value).strip() not in ('', 'None') else None
    except (TypeError, ValueError):
        val = None
    execute(
        "INSERT INTO attendance_records "
        "(year, month, att_date, factory, dept, emp_id, emp_name, rank, "
        "att_type, value, note, source, admin_name) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (int(year), int(month), ds, factory, dept, eid, name, rank,
         att_type, val, note, source, name))
    return True


def prev_workday_end(eid: str, ds: str):
    """ds 전날 근무 종료시각(HH:MM) 추정 — attendance.py _do_save의 11.5H 연속휴식 검열과
    동일 로직(전날 OT 기록 있으면 그 종료시각, 없고 평일+종일휴가 아니면 표준퇴근 17:00,
    그 외(주말/종일휴가)면 None=계산 제외)."""
    import re as _re
    try:
        cur = _date.fromisoformat(ds)
    except Exception:
        return None
    prev = cur - _timedelta(days=1)
    rows = query("SELECT att_type, value, note FROM attendance_records "
                "WHERE emp_id=%s AND att_date=%s", (eid, prev.isoformat()))
    ends, full_leave = [], False
    for r in rows:
        atype = r['att_type']
        if atype in ('잔업', '특근'):
            mm = _re.search(r'\[(\d{1,2}:\d{2})\s*~\s*(\d{1,2}:\d{2})\]', r['note'] or '')
            if mm:
                ends.append(mm.group(2))
        elif atype in ('연차', '계절휴가', '하계휴가', '기휴'):
            try:
                if float(r['value'] or 0) >= 1.0:
                    full_leave = True
            except (TypeError, ValueError):
                pass
    if ends:
        return max(ends)
    if prev.weekday() < 5 and not full_leave:
        return '17:00'
    return None


def rest_check(eid: str, ds: str, start_hhmm: str):
    """반환 dict: {'ok':bool, 'gap_h':float|None, 'prev_end':str|None, 'earliest_ok':str|None}.
    prev_workday_end()이 None이면(주말/종일휴가 다음날) 검사 대상 아님 → ok=True."""
    prev_end = prev_workday_end(eid, ds)
    if not prev_end:
        return {'ok': True, 'gap_h': None, 'prev_end': None, 'earliest_ok': None}

    def _to_min(hhmm):
        h, m = hhmm.split(':')
        return int(h) * 60 + int(m)
    pe_min = _to_min(prev_end)
    ns_min = _to_min(start_hhmm)
    gap_h = ((1440 - pe_min) + ns_min) / 60.0
    if gap_h < MIN_REST_HOURS:
        earliest_min = pe_min + int(round(MIN_REST_HOURS * 60))
        eh, em = divmod(earliest_min % 1440, 60)
        return {'ok': False, 'gap_h': round(gap_h, 1), 'prev_end': prev_end,
                'earliest_ok': f'{eh:02d}:{em:02d}'}
    return {'ok': True, 'gap_h': round(gap_h, 1), 'prev_end': prev_end, 'earliest_ok': None}


# ── 주간 당직 메일 자동발송 (서버 측) ─────────────────────────────
def duties_between(d_from: str, d_to: str) -> list:
    """기간 내 당직 배정(날짜 오름차순)."""
    rows = query("SELECT duty_date, weekday, emp_name FROM duty_assignments "
                 "WHERE duty_date BETWEEN %s AND %s ORDER BY duty_date", (d_from, d_to))
    return [{'date': _ymd(r['duty_date']), 'weekday': r['weekday'] or '',
             'name': r['emp_name'] or ''} for r in rows]


def duty_roster_names() -> list:
    rows = query("SELECT emp_name FROM duty_roster ORDER BY no NULLS LAST, emp_name")
    return [r['emp_name'] for r in rows if r.get('emp_name')]


def duty_history_between(d_from: str, d_to: str) -> list:
    """기간 내 당직자 변경 이력(최신순) — 모바일 '당직자 보기' 월별 화면용."""
    rows = query(
        "SELECT duty_date, old_emp_name, new_emp_name, changed_at, note "
        "FROM duty_change_history WHERE duty_date BETWEEN %s AND %s "
        "ORDER BY changed_at DESC", (d_from, d_to))
    return [{'date': _ymd(r['duty_date']), 'old_name': r['old_emp_name'] or '',
             'new_name': r['new_emp_name'] or '',
             'changed_at': r['changed_at'].strftime('%m-%d %H:%M') if r.get('changed_at') else '',
             'note': r['note'] or ''} for r in rows]


def emails_by_name() -> dict:
    """재직자 성명→이메일(이메일 있는 사람만)."""
    rows = query("SELECT name, email FROM employees "
                 "WHERE active=true AND COALESCE(email,'')<>''")
    out = {}
    for r in rows:
        nm = (r.get('name') or '').strip()
        em = (r.get('email') or '').strip()
        if nm and em:
            out.setdefault(nm, em)
    return out


def duty_mail_already_sent(week_key: str) -> bool:
    return query_one("SELECT 1 FROM duty_mail_log WHERE week_key=%s", (week_key,)) is not None


def mark_duty_mail_sent(week_key: str, count: int, source: str = 'server') -> None:
    execute("INSERT INTO duty_mail_log (week_key, count, source) VALUES (%s,%s,%s) "
            "ON CONFLICT (week_key) DO UPDATE SET count=EXCLUDED.count, "
            "source=EXCLUDED.source, sent_at=NOW()", (week_key, count, source))


def has_lunch_req(ds: str, emp_id: str) -> bool:
    return query_one("SELECT 1 FROM lunch_requests WHERE req_date=%s AND emp_id=%s LIMIT 1", (ds, emp_id)) is not None


def add_lunch_req(ds, factory, emp_id, name, dept, status='신청') -> bool:
    if has_lunch_req(ds, emp_id):
        return False
    execute("INSERT INTO lunch_requests (req_date,factory,emp_id,emp_name,dept,status) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (req_date,emp_id) DO NOTHING",
            (ds, factory, emp_id, name, dept, status))
    return True


def has_dinner(ds: str, emp_id: str) -> bool:
    return query_one("SELECT 1 FROM dinner_requests WHERE req_date=%s AND emp_id=%s LIMIT 1", (ds, emp_id)) is not None


def add_dinner(ds, emp_id, name, dept, rank='', gender='', reason='') -> bool:
    if has_dinner(ds, emp_id):
        return False
    execute("INSERT INTO dinner_requests (req_date,emp_id,emp_name,dept,rank,gender,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (req_date,emp_id) DO NOTHING",
            (ds, emp_id, name, dept, rank, gender, reason))
    return True


def cancel_lunch_req(ds: str, emp_id: str) -> int:
    """본인 중식 사전신청 취소(마감 전). 반환=삭제 건수."""
    return execute("DELETE FROM lunch_requests WHERE req_date=%s AND emp_id=%s", (ds, emp_id))


def cancel_dinner(ds: str, emp_id: str) -> int:
    """본인 저녁 신청 취소(마감 전). 반환=삭제 건수."""
    return execute("DELETE FROM dinner_requests WHERE req_date=%s AND emp_id=%s", (ds, emp_id))


def has_weekend(ds: str, emp_id: str, slot: str = None) -> bool:
    """slot 지정 시 그 슬롯(오전/오후)만 확인 — 같은 날 오전·오후 둘 다 신청 가능하게 하기 위함."""
    if slot:
        return query_one("SELECT 1 FROM weekend_meals WHERE meal_date=%s AND emp_id=%s AND slot=%s LIMIT 1",
                         (ds, emp_id, slot)) is not None
    return query_one("SELECT 1 FROM weekend_meals WHERE meal_date=%s AND emp_id=%s LIMIT 1", (ds, emp_id)) is not None


def add_weekend(ds, emp_id, name, dept, rank, mode, menu, price=0, co_pay=0, per_pay=0,
                headcount=1, no_meal=False, slot='', vendor='', memo='웹신청') -> bool:
    if has_weekend(ds, emp_id, slot or None):
        return False
    execute("INSERT INTO weekend_meals (meal_date,emp_id,emp_name,dept,rank,mode,menu,price,"
            "co_pay,per_pay,headcount,no_meal,slot,vendor,memo) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ds, emp_id, name, dept, rank, mode, menu, price, co_pay, per_pay, headcount, bool(no_meal), slot, vendor, memo))
    return True


def add_guest(visit_date, mgr_id, mgr_name, mgr_dept, company='', guest_name='',
              reason='', person_count=1, source='사전') -> None:
    execute("INSERT INTO guests (visit_date,mgr_emp_id,mgr_name,mgr_dept,company,guest_name,"
            "reason,person_count,source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (visit_date, mgr_id, mgr_name, mgr_dept, company, guest_name, reason, person_count, source))


# ══════════════════════════════════════════════════════════════════
# 특근결재 (ot_approvals) + 결재라인 비번(approval_lines) — 데스크탑 att_repo와 공유
# ══════════════════════════════════════════════════════════════════
def get_approval(week_key: str, factory: str) -> dict:
    r = query_one("SELECT week_key,factory,status,grp_name,grp_dt,grp_dec,grp_reason,"
                  "ceo_name,ceo_dt,ceo_dec,ceo_reason FROM ot_approvals "
                  "WHERE week_key=%s AND factory=%s", (week_key, factory))
    if not r:
        return {'week_key': week_key, 'factory': factory, 'status': '대기',
                'grp_name': '', 'grp_dt': '', 'grp_dec': '', 'grp_reason': '',
                'ceo_name': '', 'ceo_dt': '', 'ceo_dec': '', 'ceo_reason': ''}
    return {'week_key': r['week_key'], 'factory': r['factory'], 'status': r['status'] or '대기',
            'grp_name': r['grp_name'] or '', 'grp_dt': r['grp_dt'] or '',
            'grp_dec': r['grp_dec'] or '', 'grp_reason': r['grp_reason'] or '',
            'ceo_name': r['ceo_name'] or '', 'ceo_dt': r['ceo_dt'] or '',
            'ceo_dec': r['ceo_dec'] or '', 'ceo_reason': r['ceo_reason'] or ''}


def save_approval(week_key, factory, level, decision, reason, approver_name) -> None:
    from datetime import datetime
    now_s = datetime.now().strftime('%Y-%m-%d %H:%M')
    if level == 'grp':
        new_status = '1차완료' if decision == '승인' else '반려'
        execute("INSERT INTO ot_approvals (week_key,factory,status,grp_name,grp_dt,grp_dec,grp_reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (week_key,factory) DO UPDATE SET "
                "status=EXCLUDED.status, grp_name=EXCLUDED.grp_name, grp_dt=EXCLUDED.grp_dt, "
                "grp_dec=EXCLUDED.grp_dec, grp_reason=EXCLUDED.grp_reason, updated_at=NOW()",
                (week_key, factory, new_status, approver_name, now_s, decision, reason))
    else:
        new_status = '최종승인' if decision == '승인' else '반려'
        execute("INSERT INTO ot_approvals (week_key,factory,status,ceo_name,ceo_dt,ceo_dec,ceo_reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (week_key,factory) DO UPDATE SET "
                "status=EXCLUDED.status, ceo_name=EXCLUDED.ceo_name, ceo_dt=EXCLUDED.ceo_dt, "
                "ceo_dec=EXCLUDED.ceo_dec, ceo_reason=EXCLUDED.ceo_reason, updated_at=NOW()",
                (week_key, factory, new_status, approver_name, now_s, decision, reason))


def revert_approval(week_key: str, factory: str) -> str:
    """결재를 한 단계 되돌림. 최종처리→1차완료 / 1차만→대기(행삭제). 반환=새 상태."""
    r = query_one("SELECT grp_dec, ceo_dec FROM ot_approvals WHERE week_key=%s AND factory=%s",
                  (week_key, factory))
    if not r:
        return '대기'
    if r.get('ceo_dec'):
        execute("UPDATE ot_approvals SET status='1차완료', ceo_name='', ceo_dt='', "
                "ceo_dec='', ceo_reason='', updated_at=NOW() WHERE week_key=%s AND factory=%s",
                (week_key, factory))
        return '1차완료'
    execute("DELETE FROM ot_approvals WHERE week_key=%s AND factory=%s", (week_key, factory))
    return '대기'


def approval_line(factory: str, level: str):
    r = query_one("SELECT approver_name, title, pw_hash FROM approval_lines "
                  "WHERE factory=%s AND level=%s", (factory, level))
    if not r:
        return None
    return {'name': r['approver_name'] or '', 'title': r['title'] or '',
            'has_pw': bool(r['pw_hash'])}


def check_approver_pw(factory: str, level: str, pw: str) -> bool:
    """저장된 결재자 비번 해시와 일치하는지. 미설정이면 False(→호출측 폴백)."""
    import hashlib
    r = query_one("SELECT pw_hash FROM approval_lines WHERE factory=%s AND level=%s",
                  (factory, level))
    if not r or not r['pw_hash']:
        return False
    return hashlib.sha256((pw or '').encode()).hexdigest() == r['pw_hash']


def has_approver_pw(factory: str, level: str) -> bool:
    r = query_one("SELECT pw_hash FROM approval_lines WHERE factory=%s AND level=%s",
                  (factory, level))
    return bool(r and r['pw_hash'])


# ── 식당 전용 단말(dining_terminal) — 데스크탑 dining_lock의 서버측 대응(동일 SQL) ──
# 다른 LAN 데스크탑이 '/api/desktop/dining/*'로 호출 → 여기서 mjt DB 처리(직결 미노출).
def _dining_ensure():
    execute("""CREATE TABLE IF NOT EXISTS dining_terminal (
        id INTEGER PRIMARY KEY DEFAULT 1, device_guid TEXT, hostname TEXT,
        designated_at TIMESTAMP DEFAULT now(),
        CONSTRAINT dining_terminal_single CHECK (id = 1))""")

def dining_get():
    _dining_ensure()
    r = query_one("SELECT device_guid, hostname, designated_at FROM dining_terminal WHERE id=1")
    return r if (r and r.get('device_guid')) else None

def dining_claim(guid, host) -> bool:
    _dining_ensure()
    n = execute("""INSERT INTO dining_terminal (id, device_guid, hostname, designated_at)
        VALUES (1, %s, %s, now()) ON CONFLICT (id) DO UPDATE
        SET device_guid=EXCLUDED.device_guid, hostname=EXCLUDED.hostname, designated_at=now()
        WHERE dining_terminal.device_guid IS NULL""", (guid, host))
    return bool(n and n > 0)

def dining_force_claim(guid, host):
    _dining_ensure()
    execute("""INSERT INTO dining_terminal (id, device_guid, hostname, designated_at)
        VALUES (1, %s, %s, now()) ON CONFLICT (id) DO UPDATE
        SET device_guid=EXCLUDED.device_guid, hostname=EXCLUDED.hostname, designated_at=now()""",
        (guid, host))

def dining_release():
    _dining_ensure()
    execute("UPDATE dining_terminal SET device_guid=NULL, hostname=NULL WHERE id=1")


# ── 메뉴 신청함(meal_menu_requests) — 3공장 식당 메모장(종이) 디지털화, 2026-07-24 ──
# 직원이 원하는 메뉴를 자유롭게 적으면 식당 관리자(MENU_PW 인증)가 모아서 업체에 전달.
def _menu_req_ensure():
    execute("""CREATE TABLE IF NOT EXISTS meal_menu_requests (
        id SERIAL PRIMARY KEY,
        emp_id TEXT, emp_name TEXT,
        content TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '접수',
        created_at TIMESTAMP DEFAULT now(),
        done_at TIMESTAMP)""")

def add_menu_request(emp_id: str, emp_name: str, content: str) -> bool:
    content = (content or '').strip()
    if not content:
        return False
    _menu_req_ensure()
    execute("INSERT INTO meal_menu_requests (emp_id, emp_name, content, created_at) "
            "VALUES (%s,%s,%s, now())", (emp_id, emp_name, content))
    return True

def list_menu_requests(status: str = None, limit: int = 200) -> list:
    _menu_req_ensure()
    if status:
        return query("SELECT id, emp_id, emp_name, content, status, created_at, done_at "
                     "FROM meal_menu_requests WHERE status=%s ORDER BY created_at DESC LIMIT %s",
                     (status, limit))
    return query("SELECT id, emp_id, emp_name, content, status, created_at, done_at "
                 "FROM meal_menu_requests ORDER BY created_at DESC LIMIT %s", (limit,))

def set_menu_request_status(rid: int, status: str) -> None:
    _menu_req_ensure()
    if status == '전달완료':
        execute("UPDATE meal_menu_requests SET status=%s, done_at=now() WHERE id=%s", (status, rid))
    else:
        execute("UPDATE meal_menu_requests SET status=%s, done_at=NULL WHERE id=%s", (status, rid))


# ── 얼굴 임베딩(face_embeddings, 단일행 id=1) — 데스크탑 face_sync의 서버측 대응 ──
def face_meta():
    return query_one("SELECT uploaded_at, emp_count, uploaded_by, uploader_pc "
                     "FROM face_embeddings WHERE id=1")

def face_data():
    return query_one("SELECT emb_data, labels_json FROM face_embeddings WHERE id=1")

def face_labels():
    r = query_one("SELECT labels_json FROM face_embeddings WHERE id=1")
    return (r or {}).get('labels_json')

def face_upload(emb_bytes, labels_json, emp_count, by_emp, pc_name, note):
    execute("""INSERT INTO face_embeddings
            (id, emb_data, labels_json, emp_count, uploaded_by, uploader_pc, uploaded_at, note)
        VALUES (1, %s, %s, %s, %s, %s, NOW(), %s)
        ON CONFLICT (id) DO UPDATE SET
            emb_data=EXCLUDED.emb_data, labels_json=EXCLUDED.labels_json,
            emp_count=EXCLUDED.emp_count, uploaded_by=EXCLUDED.uploaded_by,
            uploader_pc=EXCLUDED.uploader_pc, uploaded_at=NOW(), note=EXCLUDED.note""",
        (emb_bytes, labels_json, emp_count, by_emp, pc_name, note))


# ── 사내 설문·경조사 (허브가 생성, 직원이 모바일로 응답·참여 — 같은 Supabase 공유) ──
def _survey_audience_pg(target_type: str, target_value: str):
    """이 설문 대상 직원(재직, LEGACY 제외) — 허브 app.py `_survey_audience()`와 동일 로직으로 대칭 유지
    (전사 규칙 #36: 흡수·공유 로직은 학습 비대칭 없이 원본과 같은 기준으로)."""
    rows = query("SELECT emp_id, name, dept, factory, biz_entity FROM employees WHERE active=true")
    rows = [e for e in rows if not (e.get('name') or '').startswith('LEGACY')]
    tt, tv = (target_type or 'all'), (target_value or '')
    if tt == 'dept':
        rows = [e for e in rows if (e.get('dept') or '').strip() == tv]
    elif tt == 'factory':
        rows = [e for e in rows if (e.get('factory') or '').strip() == tv]
    elif tt == 'biz':
        rows = [e for e in rows if (e.get('biz_entity') or '').strip() == tv]
    return rows


def surveys_for(emp_id: str):
    """이 직원 대상 설문(진행중+마감, 대상 매칭) + 응답여부·참여율·(유기명이면)미응답자 명단.
    ★2026-07-12(팀장님 요청): 응답해도 목록에서 사라지지 않고 계속 남도록 — status='진행중'만 보던 것을
    '진행중'+'마감' 둘 다 포함해 항상 누적 표시. 참여현황(참여율·진행/종료·미응답 현황)을 함께 계산."""
    if not emp_id:
        return []
    emp = query_one("SELECT dept, factory, biz_entity FROM employees WHERE UPPER(emp_id)=%s",
                    (emp_id.strip().upper(),))
    if not emp:
        return []
    rows = query("SELECT id, title, description, anonymous, target_type, target_value, "
                 "status, created_by, created_at "
                 "FROM surveys WHERE status IN ('진행중','마감') "
                 "ORDER BY (status='진행중') DESC, created_at DESC")
    out = []
    for s in rows:
        tt, tv = (s.get('target_type') or 'all'), (s.get('target_value') or '')
        if tt == 'dept' and (emp.get('dept') or '') != tv:
            continue
        if tt == 'factory' and (emp.get('factory') or '') != tv:
            continue
        if tt == 'biz' and (emp.get('biz_entity') or '') != tv:
            continue
        resp_rows = query("SELECT emp_id FROM survey_responses WHERE survey_id=%s", (s['id'],))
        resp_ids = {r['emp_id'] for r in resp_rows}
        s['responded'] = emp_id in resp_ids
        aud = _survey_audience_pg(tt, tv)
        s['n_aud'] = len(aud)
        s['n_resp'] = len(resp_ids)
        s['rate'] = round(s['n_resp'] / s['n_aud'] * 100) if s['n_aud'] else 0
        # 무기명은 인원수만(개인 식별 불가 원칙 유지), 실명은 안 한 사람 이름까지
        s['nonresp'] = [] if s['anonymous'] else [a['name'] for a in aud if a['emp_id'] not in resp_ids]
        qs = query('SELECT id, qtype, text, options, required FROM survey_questions '
                   'WHERE survey_id=%s ORDER BY "order", id', (s['id'],))
        for q in qs:
            q['opts'] = [o.strip() for o in (q.get('options') or '').split('\n') if o.strip()]
        s['questions'] = qs
        out.append(s)
    return out


def survey_one(emp_id: str, sid: int):
    """응답 폼용 — 한 설문(대상·진행중·미응답 검증 포함)."""
    for s in surveys_for(emp_id):
        if s['id'] == sid:
            return s
    return None


def submit_survey(emp_id: str, sid: int, answers: dict):
    """answers={문항id: 값 또는 [값들]}. 검증+삽입. 반환 (ok, msg)."""
    s = query_one("SELECT status FROM surveys WHERE id=%s", (sid,))
    if not s or s.get('status') != '진행중':
        return False, '진행중 설문이 아닙니다'
    if query_one("SELECT 1 FROM survey_responses WHERE survey_id=%s AND emp_id=%s", (sid, emp_id)):
        return False, '이미 응답했습니다'
    qids = {r['id'] for r in query("SELECT id FROM survey_questions WHERE survey_id=%s", (sid,))}
    with cursor() as cur:
        cur.execute("INSERT INTO survey_responses (survey_id, emp_id, submitted_at) "
                    "VALUES (%s,%s, now()) RETURNING id", (sid, emp_id))
        rid = cur.fetchone()['id']
        for k, v in (answers or {}).items():
            try:
                qid = int(k)
            except Exception:
                continue
            if qid not in qids:
                continue
            if isinstance(v, list):
                v = '|'.join(str(x) for x in v)
            cur.execute("INSERT INTO survey_answers (response_id, question_id, value) "
                        "VALUES (%s,%s,%s)", (rid, qid, str(v)))
    return True, '응답이 제출되었습니다'


def condolences_for(emp_id: str):
    """진행중 경조사 공지 + 자율참여 여부."""
    rows = query("SELECT id, title, kind, event_date, detail, peer_enabled, suggested_amount "
                 "FROM condolence_events WHERE status='진행중' ORDER BY created_at DESC")
    for ev in rows:
        ev['joined'] = bool(emp_id) and query_one(
            "SELECT 1 FROM condolence_contributions WHERE event_id=%s AND emp_id=%s",
            (ev['id'], emp_id)) is not None
    return rows


def join_condolence(emp_id: str, eid: int, amount: int = 10000):
    ev = query_one("SELECT status, peer_enabled FROM condolence_events WHERE id=%s", (eid,))
    if not ev or ev.get('status') != '진행중' or not ev.get('peer_enabled'):
        return False, '참여할 수 없는 경조사입니다'
    if query_one("SELECT 1 FROM condolence_contributions WHERE event_id=%s AND emp_id=%s", (eid, emp_id)):
        return False, '이미 참여했습니다'
    with cursor() as cur:
        cur.execute("INSERT INTO condolence_contributions (event_id, emp_id, amount, paid, created_at) "
                    "VALUES (%s,%s,%s, FALSE, now())", (eid, emp_id, amount))
    return True, '참여 완료'


# ── 공지·게시판 (허브가 작성, 직원이 읽고 댓글) ──
def notices_for(emp_id: str):
    """이 직원 대상 게시중 공지 + 읽음여부 + 댓글수 (고정 우선·최신순)."""
    if not emp_id:
        return []
    emp = query_one("SELECT dept, factory, biz_entity FROM employees WHERE UPPER(emp_id)=%s",
                    (emp_id.strip().upper(),))
    if not emp:
        return []
    rows = query("SELECT id,title,body,importance,target_type,target_value,pinned,created_by,created_at "
                 "FROM notices WHERE status='게시중' ORDER BY pinned DESC, created_at DESC")
    out = []
    for n in rows:
        tt, tv = (n.get('target_type') or 'all'), (n.get('target_value') or '')
        if tt == 'dept' and (emp.get('dept') or '') != tv:
            continue
        if tt == 'factory' and (emp.get('factory') or '') != tv:
            continue
        if tt == 'biz' and (emp.get('biz_entity') or '') != tv:
            continue
        n['read'] = query_one("SELECT 1 FROM notice_reads WHERE notice_id=%s AND emp_id=%s",
                              (n['id'], emp_id)) is not None
        n['ncmt'] = query_one("SELECT count(*) c FROM notice_comments WHERE notice_id=%s", (n['id'],))['c']
        out.append(n)
    return out


def notice_one(emp_id: str, nid: int):
    for n in notices_for(emp_id):
        if n['id'] == nid:
            return n
    return None


def mark_notice_read(emp_id: str, nid: int):
    if not emp_id:
        return
    if not query_one("SELECT 1 FROM notice_reads WHERE notice_id=%s AND emp_id=%s", (nid, emp_id)):
        with cursor() as cur:
            cur.execute("INSERT INTO notice_reads (notice_id, emp_id, read_at) VALUES (%s,%s, now())",
                        (nid, emp_id))


def notice_comments(nid: int):
    return query("SELECT c.emp_id, c.body, c.created_at, e.name FROM notice_comments c "
                 "LEFT JOIN employees e ON UPPER(e.emp_id)=UPPER(c.emp_id) "
                 "WHERE c.notice_id=%s ORDER BY c.created_at", (nid,))


def add_notice_comment(emp_id: str, nid: int, body: str):
    body = (body or '').strip()
    if not emp_id or not body:
        return False
    with cursor() as cur:
        cur.execute("INSERT INTO notice_comments (notice_id, emp_id, body, created_at) "
                    "VALUES (%s,%s,%s, now())", (nid, emp_id, body))
    return True


def unread_notice_count(emp_id: str):
    return sum(1 for n in notices_for(emp_id) if not n.get('read'))


def add_feedback(app_key, app_name, category, title, content, page, emp_id, emp_name, contact):
    """전사 개선·고장 접수 — 허브와 같은 mjt DB의 feedbacks(SSoT)에 직접 적재. 시크릿 마스터가 취합."""
    content = (content or '').strip()
    if not content:
        return None
    with cursor() as cur:
        cur.execute(
            "INSERT INTO feedbacks (app_key, app_name, category, title, content, page, "
            "emp_id, emp_name, contact, status, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'접수', now(), now()) RETURNING id",
            (app_key, app_name, (category or '개선제안')[:20], (title or '')[:200], content[:5000],
             (page or '')[:200], (emp_id or '')[:40], (emp_name or '')[:100], (contact or '')[:100]))
        return cur.fetchone()['id']


# ── 회사일정(company_events) — 데스크탑 att_repo와 같은 Supabase 표(Sheets→Supabase 통일) ──
def company_events(year: int, month: int):
    rows = query("SELECT event_date, ev_type, content, admin FROM company_events "
                 "WHERE EXTRACT(YEAR FROM event_date)=%s AND EXTRACT(MONTH FROM event_date)=%s "
                 "ORDER BY event_date", (int(year), int(month)))
    return [{'ds': (r['event_date'].isoformat() if r['event_date'] else ''),
             'type': r.get('ev_type') or '', 'content': r.get('content') or '',
             'note': r.get('admin') or ''} for r in rows]


def add_company_event(ds, ev_type, content, note=''):
    with cursor() as cur:
        cur.execute("INSERT INTO company_events (event_date, ev_type, content, admin, created_at) "
                    "VALUES (%s,%s,%s,%s, now())", (ds, ev_type, content, note))
    return True


def delete_company_event(ds, content):
    with cursor() as cur:
        cur.execute("DELETE FROM company_events WHERE event_date=%s AND content=%s", (ds, content))
    return True


# ── 설비 정기점검(PM) 스케줄 — 통합관리시스템 허브(eq_maint_schedule/eq_maint_log)가 SSoT.
# 같은 mjt DB를 쓰므로 API 없이 같은 테이블을 직접 읽고/완료처리 쓴다(허브 app.py의
# equipment_maintenance()/equipment_maintenance_complete()와 동일 로직 대칭 유지 — #36).
# 비정기 이슈(eq_issues)는 이미 위에서 다루고 있고, 이건 "주기가 되면 반복되는" 정기 PM 전용.
_PM_UNIT_LABEL = {'day': '일', 'week': '주', 'month': '개월', 'year': '년'}


def _pm_add_interval(base, unit, value):
    """기준일 + (주기단위·값) → 다음 점검일. 허브 app.py `_add_interval`과 완전히 동일한 계산식
    (월/년은 말일 초과분을 그 달 말일로 보정) — 어느 쪽에서 완료 처리하든 결과가 같아야 한다."""
    import calendar as _cal
    if unit == 'week':
        return base + _timedelta(weeks=value)
    if unit == 'month':
        m0 = base.month - 1 + value
        y = base.year + m0 // 12
        m = m0 % 12 + 1
        d = min(base.day, _cal.monthrange(y, m)[1])
        return _date(y, m, d)
    if unit == 'year':
        y = base.year + value
        try:
            return base.replace(year=y)
        except ValueError:
            return _date(y, 2, 28)
    return base + _timedelta(days=value)  # day 및 알수없는 값 폴백


def eqmaint_dashboard(days_ahead: int = 31) -> dict:
    """기한초과 / 향후 days_ahead일 이내 예정 / 다음점검일 미설정 — 정기점검 목록 3분류 +
    공정별 그룹(현장에서 자기 공정만 훑어볼 수 있도록, 2026-07-20 팀장님 피드백 — 목록이
    너무 많으니 공정·설비 단위로 나눠서 보이게)."""
    rows = query("""
        SELECT s.id, s.eq_id, m.eq_name, m.process_id, s.item, s.interval_unit, s.interval_value,
               s.last_done_date, s.next_due_date, s.in_charge_dept, s.in_charge_emp, s.note
        FROM eq_maint_schedule s LEFT JOIN eq_machines m ON m.eq_id = s.eq_id
        WHERE s.active = TRUE
        ORDER BY m.process_id NULLS LAST, s.eq_id, s.next_due_date NULLS LAST
    """)
    today = _date.today()
    horizon = today + _timedelta(days=days_ahead)
    overdue, upcoming, no_due = [], [], []
    by_process = {}
    for r in rows:
        d = r.get('next_due_date')
        item = {
            'id': r['id'], 'eq_id': r['eq_id'], 'eq_name': r.get('eq_name') or r['eq_id'],
            'process': r.get('process_id') or '미분류',
            'item': r['item'],
            'cycle': '%s%s마다' % (r['interval_value'], _PM_UNIT_LABEL.get(r['interval_unit'], r['interval_unit'])),
            'last_done_date': _ymd(r['last_done_date']) if r.get('last_done_date') else '',
            'next_due_date': _ymd(d) if d else '',
            'in_charge_dept': r.get('in_charge_dept') or '', 'in_charge_emp': r.get('in_charge_emp') or '',
            'note': r.get('note') or '',
        }
        if not d:
            no_due.append(item)
            continue
        elif d < today:
            item['days_over'] = (today - d).days
            overdue.append(item)
        elif d <= horizon:
            upcoming.append(item)
        else:
            continue
        by_process.setdefault(item['process'], []).append(item)

    groups = []
    for proc in sorted(by_process.keys()):
        items = by_process[proc]
        items.sort(key=lambda x: (0 if x.get('days_over') else 1, x.get('next_due_date') or ''))
        groups.append({
            # ★키 이름 'items' 절대 금지 — Jinja가 dict.items() 내장 메서드로 오인식해
            # `{% for p in g.items %}`가 TypeError로 500 에러남(허브에서 같은 사고 이미 겪음).
            'process': proc, 'entries': items,
            'overdue_n': sum(1 for i in items if i.get('days_over')),
            'upcoming_n': sum(1 for i in items if not i.get('days_over')),
        })
    return {'overdue': overdue, 'upcoming': upcoming, 'no_due': no_due, 'groups': groups,
            'total': len(overdue) + len(upcoming) + len(no_due)}


def eqmaint_complete(sched_id: int, done_by: str, note: str) -> bool:
    """정기점검 완료 처리 — 담당자·날짜·점검결과를 이력(eq_maint_log)에 남기고, 다음 점검일을
    주기만큼 자동으로 밀어 다시 대기 상태로 되돌린다(허브에서 완료 처리한 것과 동일 효과)."""
    r = query_one("SELECT interval_unit, interval_value FROM eq_maint_schedule WHERE id=%s AND active=TRUE",
                  (sched_id,))
    if not r:
        return False
    today = _date.today()
    next_due = _pm_add_interval(today, r['interval_unit'], r['interval_value'] or 1)
    with cursor() as cur:
        cur.execute("UPDATE eq_maint_schedule SET last_done_date=%s, next_due_date=%s, updated_at=NOW() "
                    "WHERE id=%s", (today, next_due, sched_id))
        cur.execute("INSERT INTO eq_maint_log (sched_id, done_date, done_by, note, created_at) "
                    "VALUES (%s,%s,%s,%s, NOW())", (sched_id, today, (done_by or '').strip()[:120],
                                                      (note or '').strip()[:2000]))
    return True


def eqmaint_log_for(sched_id: int, limit: int = 5) -> list:
    """이 정기점검의 완료 이력(최신순) — 완료 처리해도 사라지지 않고 남는 기록."""
    rows = query("SELECT done_date, done_by, note FROM eq_maint_log "
                 "WHERE sched_id=%s ORDER BY done_date DESC, id DESC LIMIT %s", (sched_id, limit))
    return [{'date': _ymd(r['done_date']), 'by': r.get('done_by') or '', 'note': r.get('note') or ''}
            for r in rows]


FACTORY_ORDER = ['MJ 1공장', 'SCS 2공장', 'CnB-P']


def eq_kpi_by_factory() -> list:
    """설비 KPI를 공장(MJ/SCS/CnB)별로 쪼개서 반환 — 2026-07-21 팀장님 요청:
    "전체설비/점검중/오늘접수 같은 대시보드가 그냥 숫자만 있어서 의미가 크게 없다.
    MJ/SCS/CnB나 공정별로 나눠 볼 수 있으면 좋겠다." 공장별 소계를 먼저 제공(공정별 세부는
    이미 있는 '이번 달 공정별 이슈' 막대그래프가 별도로 담당)."""
    def _by_factory(sql, params=()):
        return {r['factory']: r['c'] for r in query(sql, params)}

    total = _by_factory("SELECT factory, COUNT(*) c FROM eq_machines WHERE active=TRUE GROUP BY factory")
    issues = _by_factory(
        "SELECT m.factory, COUNT(*) c FROM eq_issues i JOIN eq_machines m ON m.eq_id=i.eq_id "
        "WHERE i.status IN ('신규','이관','점검중') GROUP BY m.factory")
    today = _by_factory(
        "SELECT m.factory, COUNT(*) c FROM eq_issues i JOIN eq_machines m ON m.eq_id=i.eq_id "
        "WHERE DATE(i.occurred_at)=CURRENT_DATE GROUP BY m.factory")
    this_m = _by_factory(
        "SELECT m.factory, COUNT(*) c FROM eq_issues i JOIN eq_machines m ON m.eq_id=i.eq_id "
        "WHERE i.status='완료' AND DATE_TRUNC('month',i.closed_at)=DATE_TRUNC('month',CURRENT_DATE) "
        "GROUP BY m.factory")
    pm_overdue = _by_factory(
        "SELECT m.factory, COUNT(*) c FROM eq_maint_schedule s JOIN eq_machines m ON m.eq_id=s.eq_id "
        "WHERE s.active=TRUE AND s.next_due_date < CURRENT_DATE GROUP BY m.factory")
    pm_upcoming = _by_factory(
        "SELECT m.factory, COUNT(*) c FROM eq_maint_schedule s JOIN eq_machines m ON m.eq_id=s.eq_id "
        "WHERE s.active=TRUE AND s.next_due_date >= CURRENT_DATE "
        "AND s.next_due_date <= CURRENT_DATE + 31 GROUP BY m.factory")

    factories = list(FACTORY_ORDER)
    for f in list(total) + list(issues):
        if f and f not in factories:
            factories.append(f)   # 예상 밖 공장값도 누락 없이 노출(예: '공통' 등)

    out = []
    for f in factories:
        out.append({
            'factory': f, 'total': total.get(f, 0), 'issues': issues.get(f, 0),
            'today': today.get(f, 0), 'this_m': this_m.get(f, 0),
            'pm_overdue': pm_overdue.get(f, 0), 'pm_upcoming': pm_upcoming.get(f, 0),
        })
    return [g for g in out if g['total'] or g['issues'] or g['pm_overdue'] or g['pm_upcoming']]


# ── 설비 이슈 진행 이력(eq_issue_history) — 데스크탑(설비보전실)이 남기는 단계별 기록을
# 모바일에서도 볼 수 있게(2026-07-21 팀장님 요청: "고장 접수가 너무 단순해서 건별 진행상황을
# 보고 싶다"). 30건 정도의 이슈를 한 번에 보여주는 화면이라 이슈별로 따로따로 조회하지 않고
# IN절 한 번으로 배치 조회 후 issue_id별로 묶어 돌려준다(N+1 방지).
STAGE_ICON = {'신규등록': '📝', '이관': '📤', '점검시작': '🔧', '진행메모': '💬', '수리완료': '✅'}


def fmt_eta(eta_at) -> str:
    """예상 수리 완료(eta_at)를 모바일 카드에 짧게 보여줄 문구로 변환(2026-07-21 팀장님 요청 —
    담당자가 점검시작 시 입력한 예상완료를 요청자도 볼 수 있게). eq_issues.eta_at은 데스크탑
    (KST 로컬시각)이 datetime.now()로 그대로 저장한 naive timestamp라 tz 변환 없이 비교한다."""
    if not eta_at:
        return ''
    import datetime as _dt
    now = _dt.datetime.now()
    overdue = eta_at < now
    tag = '⏰ 예상시간 초과 · ' if overdue else '⏳ 예상완료 '
    if eta_at.date() == now.date():
        return f"{tag}{eta_at.strftime('%H:%M')}"
    return f"{tag}{eta_at.strftime('%m/%d %H:%M')}"


def eq_issue_histories_for(issue_ids: list) -> dict:
    if not issue_ids:
        return {}
    rows = query(
        "SELECT issue_id, happened_at, stage, actor_name, memo FROM eq_issue_history "
        "WHERE issue_id = ANY(%s) ORDER BY happened_at", (list(issue_ids),))
    out = {}
    for r in rows:
        out.setdefault(r['issue_id'], []).append({
            'at': _to_kst(r['happened_at']).strftime('%m-%d %H:%M') if r.get('happened_at') else '',
            'stage': r.get('stage') or '',
            'icon': STAGE_ICON.get(r.get('stage'), '•'),
            'by': r.get('actor_name') or '',
            'memo': r.get('memo') or '',
        })
    return out


def samgyup_dates(today_iso):
    """다가오는 삼겹살데이(회사일정 유형='삼겹살데이', 오늘 이후) — Supabase."""
    rows = query("SELECT event_date, content, admin FROM company_events "
                 "WHERE ev_type='삼겹살데이' AND event_date >= %s ORDER BY event_date", (today_iso,))
    return [{'date': r['event_date'].isoformat() if r['event_date'] else '',
             'content': r.get('content') or '삼겹살데이', 'note': r.get('admin') or ''} for r in rows]
