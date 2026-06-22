"""web_app 전용 메일 발송기 (smtplib, 표준 라이브러리만).

Render 환경변수에서 발송 계정을 읽는다(코드/깃에 비밀번호 없음):
- MAIL_USER       : 보내는 Gmail 주소 (예: mjt.dispatch@gmail.com)
- MAIL_APP_PW     : 16자리 Gmail 앱 비밀번호 (공백 자동 제거)
- MAIL_FROM_NAME  : 표시 이름 (기본 'MJT 통합관리 자동발송')

데스크탑 mail_sender.py와 동일하게 도메인으로 SMTP 서버를 자동선택한다.
web_app은 상위 폴더 모듈을 import할 수 없으므로(Render는 web_app/만 배포) 독립 구현.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

# 도메인 → (host, port). 587=STARTTLS, 465=SSL
_SMTP_BY_DOMAIN = {
    'gmail.com':   ('smtp.gmail.com', 587),
    'naver.com':   ('smtp.naver.com', 587),
    'daum.net':    ('smtp.daum.net', 465),
    'hanmail.net': ('smtp.daum.net', 465),
    'nate.com':    ('smtp.mail.nate.com', 465),
    'outlook.com': ('smtp.office365.com', 587),
    'hotmail.com': ('smtp.office365.com', 587),
}


def _cfg() -> dict:
    user = (os.environ.get('MAIL_USER') or '').strip()
    pw = (os.environ.get('MAIL_APP_PW') or '').replace(' ', '').strip()
    name = (os.environ.get('MAIL_FROM_NAME') or 'MJT 통합관리 자동발송').strip()
    host, port = '', 0
    if '@' in user:
        dom = user.split('@', 1)[1].lower()
        host, port = _SMTP_BY_DOMAIN.get(dom, ('smtp.gmail.com', 587))
    return {'user': user, 'pw': pw, 'name': name, 'host': host, 'port': port}


def is_configured() -> bool:
    c = _cfg()
    return bool(c['user'] and c['pw'] and c['host'])


def send(to_addr: str, subject: str, body: str) -> None:
    """단일 발송(실패 시 예외)."""
    c = _cfg()
    if not is_configured():
        raise RuntimeError('메일 발송 계정 미설정(MAIL_USER/MAIL_APP_PW)')
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = formataddr((str(Header(c['name'], 'utf-8')), c['user']))
    msg['To'] = to_addr
    if c['port'] == 465:
        with smtplib.SMTP_SSL(c['host'], c['port'], timeout=30) as s:
            s.login(c['user'], c['pw'])
            s.sendmail(c['user'], [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(c['host'], c['port'], timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(c['user'], c['pw'])
            s.sendmail(c['user'], [to_addr], msg.as_string())


def send_bulk(items):
    """items=[(to, subject, body), ...]. 반환(ok, fail, errs)."""
    ok = fail = 0
    errs = []
    for to_addr, subj, body in items:
        try:
            send(to_addr, subj, body)
            ok += 1
        except Exception as e:
            fail += 1
            errs.append(f'{to_addr}: {e}')
    return ok, fail, errs
