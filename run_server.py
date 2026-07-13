# -*- coding: utf-8 -*-
"""web_app(식수 모바일) 서버 실행기 — .env 로드 후 waitress로 서비스.
MJT서버PC에서 pythonw로 서비스 실행(창 없음). 포트는 .env의 PORT(기본 8732).
Render의 gunicorn 대신 Windows 서버에선 waitress 사용.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

# .env 로드(web_app app.py는 자체 load_dotenv 없음 → 여기서 로드)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

port = int(os.environ.get("PORT", "8732"))

from app import app
try:
    from waitress import serve
    serve(app, host="0.0.0.0", port=port)
except ImportError:
    # waitress 미설치 시 최후 폴백(개발용)
    app.run(host="0.0.0.0", port=port, debug=False)
