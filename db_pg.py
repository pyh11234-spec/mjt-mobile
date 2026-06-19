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


def chinese_menus(active_only: bool = True) -> list:
    sql = "SELECT name,price FROM chinese_menus" + (" WHERE active=TRUE" if active_only else "") + " ORDER BY name"
    return [{'name': r['name'], 'price': r['price'] or 0} for r in query(sql)]


def wkend_plan(from_date: str, limit: int = 6) -> list:
    """from_date 이후 운영일(mode가 '없음'/NULL 아님). checkin.html 호환 dict."""
    rows = query("SELECT meal_date,mode,support,notice FROM weekend_settings "
                 "WHERE meal_date >= %s AND mode IS NOT NULL AND mode <> '없음' "
                 "ORDER BY meal_date LIMIT %s", (from_date, limit))
    out = []
    for r in rows:
        ds = _ymd(r['meal_date'])
        try:
            wd = '월화수목금토일'[_date.fromisoformat(ds).weekday()]
        except Exception:
            wd = ''
        out.append({'date': ds, 'weekday': wd, 'mode': r['mode'],
                    'support': r['support'] or 0, 'notice': r['notice'] or ''})
    return out


def wkend_setting(ds: str):
    r = query_one("SELECT mode,support,notice FROM weekend_settings WHERE meal_date=%s", (ds,))
    if not r:
        return None
    return {'mode': r['mode'], 'support': r['support'] or 0, 'notice': r['notice'] or ''}


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
                headcount=1, no_meal=False, memo='웹신청') -> bool:
    if has_weekend(ds, emp_id):
        return False
    execute("INSERT INTO weekend_meals (meal_date,emp_id,emp_name,dept,rank,mode,menu,price,"
            "co_pay,per_pay,headcount,no_meal,memo) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ds, emp_id, name, dept, rank, mode, menu, price, co_pay, per_pay, headcount, bool(no_meal), memo))
    return True


def add_guest(visit_date, mgr_id, mgr_name, mgr_dept, company='', guest_name='',
              reason='', person_count=1, source='사전') -> None:
    execute("INSERT INTO guests (visit_date,mgr_emp_id,mgr_name,mgr_dept,company,guest_name,"
            "reason,person_count,source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (visit_date, mgr_id, mgr_name, mgr_dept, company, guest_name, reason, person_count, source))
