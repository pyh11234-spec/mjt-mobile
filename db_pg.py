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


# ── 사내 설문·경조사 (허브가 생성, 직원이 모바일로 응답·참여 — 같은 Supabase 공유) ──
def surveys_for(emp_id: str):
    """이 직원 대상 진행중 설문(대상 매칭) + 응답여부 + 문항(보기 리스트)."""
    if not emp_id:
        return []
    emp = query_one("SELECT dept, factory, biz_entity FROM employees WHERE UPPER(emp_id)=%s",
                    (emp_id.strip().upper(),))
    if not emp:
        return []
    rows = query("SELECT id, title, description, anonymous, target_type, target_value "
                 "FROM surveys WHERE status='진행중' ORDER BY created_at DESC")
    out = []
    for s in rows:
        tt, tv = (s.get('target_type') or 'all'), (s.get('target_value') or '')
        if tt == 'dept' and (emp.get('dept') or '') != tv:
            continue
        if tt == 'factory' and (emp.get('factory') or '') != tv:
            continue
        if tt == 'biz' and (emp.get('biz_entity') or '') != tv:
            continue
        s['responded'] = query_one(
            "SELECT 1 FROM survey_responses WHERE survey_id=%s AND emp_id=%s LIMIT 1",
            (s['id'], emp_id)) is not None
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
