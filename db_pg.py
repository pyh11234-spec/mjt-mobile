"""
web_app용 PostgreSQL (Supabase) 연결 헬퍼.
환경변수 SUPABASE_DB_URL 필요 (render.com → Environment 에 등록).
"""
import os, threading
from contextlib import contextmanager

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
