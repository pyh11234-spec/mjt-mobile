"""
web_app용 PostgreSQL (Supabase) 연결 헬퍼.
환경변수 SUPABASE_DB_URL 필요 (render.com → Environment 에 등록).
"""
import os, threading
from contextlib import contextmanager
from datetime import date as _date

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


# ══════════════════════════════════════════════════════════════════
# 식수(급식) 데이터 — Supabase SSoT.  (데스크탑 meal_repo와 동일 역할)
# 읽기는 기존 템플릿/소비처가 위치색인을 쓰므로 Sheets와 동일한 컬럼 순서의
# list 로 반환한다(소비처 무수정). 쓰기는 테이블에 직접 INSERT.
# ══════════════════════════════════════════════════════════════════
def _hms(v) -> str:
    return v.strftime('%H:%M:%S') if hasattr(v, 'strftime') else (str(v) if v else '')


def _ymd(v) -> str:
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


def has_weekend(ds: str, emp_id: str) -> bool:
    return query_one("SELECT 1 FROM weekend_meals WHERE meal_date=%s AND emp_id=%s LIMIT 1", (ds, emp_id)) is not None


def add_weekend(ds, emp_id, name, dept, rank, mode, menu, price=0, co_pay=0, per_pay=0,
                headcount=1, no_meal=False, slot='', vendor='', memo='웹신청') -> bool:
    if has_weekend(ds, emp_id):
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
