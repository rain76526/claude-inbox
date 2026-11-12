#!/usr/bin/env python3
"""收件匣：讓 Claude Code 的 agent 把要你決定的事集中到一個網頁，你回答後答案自動回到 agent 手上。

只用 Python 標準庫（3.9 以上）。資料放在 $CLAUDE_INBOX_HOME（預設 ~/.claude-inbox）。
"""
import argparse
import ipaddress
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WINDOWS = os.name == 'nt'
if WINDOWS:
    import msvcrt
else:
    import fcntl

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'page.html'
DEFAULT_PORT = 7799
MAX_BODY = 64 * 1024
MAX_TEXT = 4000
ORPHAN_SECONDS = 30 * 60  # 提問的 session 30 分鐘沒來拿，同專案其他 session 可以拿


# ---------- 跨平台 ----------

def lock_file(fh, timeout=None):
    """對整個檔案上互斥鎖。Windows 用 msvcrt 鎖第 0 個位元組，不受檔案長度與指標影響。
    timeout 是秒數，None 表示一直等；等不到丟 TimeoutError。"""
    if not WINDOWS and timeout is None:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return
    fd = fh.fileno()
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            if WINDOWS:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('等不到檔案鎖')
            time.sleep(0.05)


def unlock_file(fh):
    if not WINDOWS:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return
    fd = fh.fileno()
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class FileLock:
    """鎖獨立的 .lock 檔，不鎖資料檔本身：Windows 的 msvcrt 鎖會擋住別人讀資料檔的第 0 個位元組。
    （來自 PR #1，Dave Tseng）"""

    def __init__(self, path, timeout=None):
        self.path = path
        self.timeout = timeout

    def __enter__(self):
        self.fh = open(self.path, 'a+b')
        try:
            lock_file(self.fh, self.timeout)
        except BaseException:
            self.fh.close()
            raise
        return self

    def __exit__(self, *exc):
        try:
            unlock_file(self.fh)
        finally:
            self.fh.close()


def replace_file(tmp, path):
    """os.replace；Windows 上目標剛好被別的行程開著時會 PermissionError，稍等重試。"""
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not WINDOWS or attempt == 49:
                raise
            time.sleep(0.05)


def same_path(path):
    """路徑正規化：git 回傳正斜線、Windows 磁碟代號大小寫不一，統一成 realpath（保留大小寫，供顯示）。"""
    return os.path.realpath(path)


def path_key(path):
    """比對用：Windows 路徑不分大小寫。"""
    return os.path.normcase(path or '')


# ---------- 路徑與設定 ----------

def home():
    root = Path(os.environ.get('CLAUDE_INBOX_HOME') or Path.home() / '.claude-inbox')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / 'questions').mkdir(mode=0o700, exist_ok=True)
    return root


def read_config(path):
    config = {'host': '127.0.0.1', 'port': DEFAULT_PORT, 'token': ''}
    if path.exists():
        config.update(json.loads(path.read_text(encoding='utf-8')))
    return config


def load_config():
    path = home() / 'config.json'
    config = read_config(path)
    if not config.get('token'):
        with FileLock(home() / 'config.lock'):
            config = read_config(path)  # 搶到鎖後重讀，別人可能已經產生過
            if not config.get('token'):
                config['token'] = secrets.token_urlsafe(24)
                write_json(path, config)
    return config


def is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.')  # 預設權限 600
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2))
    replace_file(tmp, path)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def project_of(cwd):
    try:
        out = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return same_path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return same_path(str(cwd))


def base_url(config):
    host = 'localhost' if config['host'] == '127.0.0.1' else config['host']
    return 'http://%s:%s/' % (host, config['port'])


def page_url(config):
    return base_url(config) + ('?t=' + config['token'] if config.get('token') else '')


# ---------- 題目與事件 ----------

def load_questions():
    items = []
    for path in sorted((home() / 'questions').glob('*.json')):
        try:
            items.append(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            continue
    return items


def events_path():
    return home() / 'events.jsonl'


class EventLog:
    """events.jsonl 的讀寫，全部包在 events.lock 這把檔案鎖裡。"""

    def __enter__(self):
        self.lock = FileLock(home() / 'events.lock').__enter__()
        try:
            fd = os.open(events_path(), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            self.fh = os.fdopen(fd, 'a+', encoding='utf-8')
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        try:
            self.fh.close()
        finally:
            self.lock.__exit__(*exc)

    def read(self):
        self.fh.seek(0)
        out = []
        for line in self.fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def append(self, events):
        for event in events:
            self.fh.write(json.dumps(event, ensure_ascii=False) + '\n')
        self.fh.flush()
        os.fsync(self.fh.fileno())


def read_events():
    if not events_path().exists():
        return []
    with EventLog() as log:
        return log.read()


def build_state(events=None):
    questions = {q['id']: dict(q, answers=[], closed=False) for q in load_questions()}
    delivered = set()
    if events is None:
        events = read_events()
    for e in events:
        if e.get('kind') == 'delivered':
            delivered.add(e.get('ref'))
    for e in events:
        q = questions.get(e.get('question_id'))
        if not q:
            continue
        if e.get('kind') == 'answer':
            q['answers'].append(dict(e, delivered=e['event_id'] in delivered))
        elif e.get('kind') == 'closed':
            q['closed'] = True
    items = sorted(questions.values(), key=lambda q: q['created'])
    return {'questions': items}


def deliverable(session_id, cwd, events=None):
    """這個 session 該拿到、還沒拿過的回答。"""
    project = project_of(cwd)
    now = time.time()
    out = []
    for q in build_state(events)['questions']:
        asker = q.get('session')
        own = bool(asker) and asker == session_id  # 出題的就是這個 session：不管現在在哪個資料夾
        if not own and path_key(q.get('project')) != path_key(project):
            continue
        for a in q['answers']:
            if a['delivered']:
                continue
            age = now - datetime.fromisoformat(a['at']).timestamp()
            if own or not asker or age > ORPHAN_SECONDS:
                out.append((q, a))
    return out


def format_answers(pairs):
    lines = ['收件匣：使用者回覆了 %d 則。' % len(pairs)]
    for q, a in pairs:
        lines.append('')
        lines.append('【%s】%s' % (q['id'], q['title']))
        if a.get('choice') == 'ack':
            lines.append('使用者：我知道了')
        elif a.get('choice'):
            lines.append('使用者選：%s %s' % (a['choice'], a.get('choice_label', '')))
        if a.get('text'):
            lines.append('補充：' + a['text'])
    lines.append('')
    lines.append('照回覆繼續做；這件處理完可跑 inbox close <題號> 把它收起來。')
    return '\n'.join(lines)


def take(session_id, cwd):
    """取出回答並標成已送達；同一把鎖裡做，兩個 session 不會拿到同一則。"""
    with EventLog() as log:
        pairs = deliverable(session_id, cwd, log.read())
        log.append([{'event_id': str(uuid.uuid4()), 'kind': 'delivered', 'ref': a['event_id'],
                     'session': session_id, 'at': now_iso()} for _, a in pairs])
    return pairs


def validate_answers(payload, questions):
    answers = payload.get('answers') if isinstance(payload, dict) else None
    if not isinstance(answers, list) or not answers:
        raise ValueError('answers 必須是非空陣列')
    events = []
    for item in answers:
        q = questions.get(item.get('question_id')) if isinstance(item, dict) else None
        if not q:
            raise ValueError('找不到題目')
        choice = item.get('choice') or ''
        text = item.get('text') or ''
        if not isinstance(choice, str) or not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError('欄位格式不對')
        labels = {o['key']: o['label'] for o in q.get('options', [])}
        if q.get('kind') == 'info':
            if choice not in ('', 'ack'):
                raise ValueError('知會題只能按「我知道了」')
            choice = 'ack'
        elif choice and choice not in labels:
            raise ValueError('選項不存在')
        if not choice and not text.strip():
            raise ValueError('沒有選擇也沒有補充')
        events.append({'event_id': str(uuid.uuid4()), 'kind': 'answer', 'question_id': q['id'],
                       'choice': choice, 'choice_label': labels.get(choice, ''),
                       'text': text.strip(), 'at': now_iso()})
    return events


# ---------- 進度看板 ----------

STATUSES = {'active': '進行中', 'blocked': '卡住', 'review': '待驗收', 'done': '完成', 'idle': '閒置'}
TEXT_FIELDS = ('session', 'project', 'path', 'branch', 'task', 'progress', 'blocker', 'next')
MAX_FIELD = 500
MAX_LINES = 200
ACTIVE_THROTTLE_SECONDS = 60
STALE_MINUTES = 120
BOARD_HOOK_LOCK_SECONDS = 2
LINE_ID = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


class BoardError(ValueError):
    pass


def board_path():
    return home() / 'board.json'


def read_board():
    """讀 board.json；沒有檔案回空看板，壞掉就丟 BoardError（呼叫端不能拿空看板去覆蓋它）。"""
    path = board_path()
    if not path.exists():
        return {'version': 1, 'lines': []}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise BoardError('看板資料讀不到（%s）：%s' % (path, exc))
    if not isinstance(data, dict) or not isinstance(data.get('lines'), list):
        raise BoardError('看板資料格式不對：%s' % path)
    return data


def parse_status(value):
    if value in STATUSES:
        return value
    for key, label in STATUSES.items():
        if value == label:
            return key
    raise BoardError('狀態只能是：' + '／'.join('%s(%s)' % kv for kv in STATUSES.items()))


def git_branch(cwd):
    try:
        out = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--abbrev-ref', 'HEAD'],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ''


def update_board(change, timeout=None):
    """鎖 board.lock → 重讀 → change(lines) → 原子寫回。"""
    with FileLock(home() / 'board.lock', timeout):
        board = read_board()
        change(board['lines'])
        write_json(board_path(), board)
    return board


def set_line(line_id, fields):
    if not isinstance(line_id, str) or not LINE_ID.match(line_id):
        raise BoardError('工作線代號只能用英數、-、_、.，1 到 64 字：%r' % line_id)
    fields = dict(fields)
    if 'status' in fields:
        fields['status'] = parse_status(fields['status'])
    for key in TEXT_FIELDS:
        if key in fields and len(fields[key] or '') > MAX_FIELD:
            raise BoardError('%s 太長（上限 %d 字）' % (key, MAX_FIELD))
    stamp = now_iso()

    def change(lines):
        line = next((l for l in lines if l.get('id') == line_id), None)
        if line is None:
            if len(lines) >= MAX_LINES:
                raise BoardError('看板最多 %d 條工作線，先 remove 用不到的' % MAX_LINES)
            cwd = os.getcwd()
            line = {'id': line_id, 'session': '', 'session_id': os.environ.get('CLAUDE_CODE_SESSION_ID', ''),
                    'project': project_of(cwd), 'path': same_path(cwd), 'branch': git_branch(cwd),
                    'task': '', 'status': 'active', 'progress': '', 'blocker': '', 'next': ''}
            lines.append(line)
        line.update({k: v for k, v in fields.items() if k in TEXT_FIELDS + ('status',)})
        line['updated_at'] = line['last_active'] = stamp

    update_board(change)


def stale_active(line, now, seconds):
    try:
        return now - datetime.fromisoformat(line.get('last_active') or '').timestamp() >= seconds
    except ValueError:
        return True


def touch_active(session_id):
    """hook 用：更新已登記 session 的 last_active。不能拖慢 hook，任何問題都直接放棄。"""
    if not session_id or not board_path().exists():
        return
    now = time.time()
    try:
        lines = read_board()['lines']
        if not any(l.get('session_id') == session_id and stale_active(l, now, ACTIVE_THROTTLE_SECONDS)
                   for l in lines):
            return  # 沒登記，或 60 秒內剛更新過：不拿鎖、不寫檔
        stamp = now_iso()

        def change(lines):
            for l in lines:
                if l.get('session_id') == session_id:
                    l['last_active'] = stamp

        update_board(change, timeout=BOARD_HOOK_LOCK_SECONDS)
    except (BoardError, OSError, TimeoutError):
        pass


def board_view(questions, stale_minutes=None):
    """給網頁的看板：每條線加上 open_questions（同專案、未收起、未回答）與 stale。"""
    try:
        lines = read_board()['lines']
    except BoardError as exc:
        return {'board': [], 'board_error': str(exc)}
    try:
        stale_seconds = float(stale_minutes) * 60 if stale_minutes else STALE_MINUTES * 60
    except (TypeError, ValueError):
        stale_seconds = STALE_MINUTES * 60
    open_by_project = {}
    for q in questions:
        if not q['closed'] and not q['answers']:
            open_by_project.setdefault(path_key(q.get('project')), []).append(q['id'])
    now = time.time()
    out = []
    for l in lines:
        if not isinstance(l, dict):
            continue
        ids = open_by_project.get(path_key(l.get('project')), [])
        out.append(dict(l, open_questions=len(ids), open_question_ids=ids,
                        stale=l.get('status') not in ('done', 'idle') and stale_active(l, now, stale_seconds)))
    return {'board': out, 'board_error': ''}


def remove_line(line_id):
    def change(lines):
        keep = [l for l in lines if l.get('id') != line_id]
        if len(keep) == len(lines):
            raise BoardError('找不到工作線：' + line_id)
        lines[:] = keep

    update_board(change)


# ---------- 網頁服務 ----------

class Handler(BaseHTTPRequestHandler):
    config = None

    def log_message(self, fmt, *args):
        pass

    def allowed_hosts(self):
        port = self.config['port']
        hosts = {'%s:%s' % (self.config['host'], port)}
        if is_loopback(self.config['host']):
            hosts |= {'localhost:%s' % port, '127.0.0.1:%s' % port}
        return hosts

    def check(self, api):
        host = self.headers.get('Host', '')
        if host not in self.allowed_hosts():
            return self.send_text(403, 'Host 不符')
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + host:
            return self.send_text(403, 'Origin 不符')
        token = self.config.get('token')
        if api and token and not secrets.compare_digest(self.headers.get('X-Inbox-Token', ''), token):
            return self.send_text(401, '需要 token')
        return True

    def send_text(self, code, text, ctype='text/plain; charset=utf-8'):
        body = text.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/':
            if self.check(api=False):
                self.send_text(200, PAGE.read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        elif path == '/api/state':
            if self.check(api=True):
                state = build_state()
                state.update(board_view(state['questions'], self.config.get('board_stale_minutes')))
                self.send_text(200, json.dumps(state, ensure_ascii=False),
                               'application/json; charset=utf-8')
        else:
            self.send_text(404, 'not found')

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        # 先讀完本文再回錯誤：Windows 關閉還有未讀資料的連線會送 RST，
        # 用戶端收不到 403/404，只看到 WinError 10053/10054。（來自 PR #1，Dave Tseng）
        raw = self.rfile.read(length) if 0 < length <= MAX_BODY else None
        if self.path != '/api/answer':
            return self.send_text(404, 'not found')
        if not self.check(api=True):
            return
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.send_text(415, '只收 JSON')
        if raw is None:
            return self.send_text(413, '內容太大或是空的')
        try:
            payload = json.loads(raw.decode('utf-8'))
            questions = {q['id']: q for q in load_questions()}
            events = validate_answers(payload, questions)
        except ValueError as exc:
            return self.send_text(400, str(exc))
        with EventLog() as log:
            log.append(events)
        self.send_text(200, json.dumps({'stored': len(events)}), 'application/json')


class Server(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允許兩個行程同時綁同一個埠，會讓兩個服務並存；只在 POSIX 開
    allow_reuse_address = not WINDOWS


def serve(config):
    Handler.config = config
    try:
        server = Server((config['host'], config['port']), Handler)
    except PermissionError:
        if WINDOWS:  # WinError 10013：埠落在 Hyper-V／WinNAT 保留範圍，沒人在用也綁不到（來自 PR #1，Dave Tseng）
            print('綁不到埠 %s：可能在 Windows 保留範圍內（netsh interface ipv4 show '
                  'excludedportrange protocol=tcp）。請在 %s 設定其他 port。'
                  % (config['port'], home() / 'config.json'), flush=True)
        raise
    (home() / 'server.pid').write_text(str(os.getpid()))  # 綁埠成功才寫：搶輸的不會蓋掉活著的那個
    print('收件匣服務已啟動：' + base_url(config), flush=True)  # 服務的 log 不放 token
    server.serve_forever()


def server_alive(config):
    import urllib.request
    host = 'localhost' if is_loopback(config['host']) else config['host']
    req = urllib.request.Request('http://%s:%s/api/state' % (host, config['port']),
                                 headers={'X-Inbox-Token': config.get('token', '')})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            return resp.status == 200 and isinstance(body, dict) and 'questions' in body
    except Exception:
        return False


def ensure_server(config):
    if server_alive(config):
        return True
    log = open(home() / 'server.log', 'a', encoding='utf-8')
    if WINDOWS:  # 脫離呼叫者的主控台：hook／指令結束後服務繼續活著，也不跳出黑視窗
        detach = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                  | subprocess.CREATE_NO_WINDOW, 'close_fds': True}
    else:
        detach = {'start_new_session': True}
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve'],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **detach)
    log.close()
    for _ in range(20):
        time.sleep(0.2)
        if server_alive(config):
            return True
    return False


# ---------- 指令 ----------

def cmd_ask(args):
    options = []
    for raw in args.option or []:
        key, sep, label = raw.partition('=')
        if not sep or not key.strip() or not label.strip():
            sys.exit('選項格式是 A=說明文字：' + raw)
        options.append({'key': key.strip(), 'label': label.strip(),
                        'recommended': key.strip() == args.recommend})
    if args.recommend and not any(o['recommended'] for o in options):
        sys.exit('--recommend 指的選項不存在')
    body = Path(args.body_file).read_text(encoding='utf-8') if args.body_file else (args.body or '')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    q = {'id': 'q-%s-%s' % (stamp, secrets.token_hex(2)), 'kind': 'info' if args.info else 'ask',
         'title': args.title, 'body': body, 'options': options, 'deadline': args.deadline or '',
         'from': args.source or os.path.basename(project_of(os.getcwd())),
         'project': project_of(os.getcwd()), 'session': os.environ.get('CLAUDE_CODE_SESSION_ID', ''),
         'created': now_iso()}
    write_json(home() / 'questions' / (q['id'] + '.json'), q)
    config = load_config()
    up = ensure_server(config)
    print('已放進收件匣：%s' % q['id'])
    print('網址：' + page_url(config) if up else '服務沒起來，請看 %s' % (home() / 'server.log'))


def cmd_close(args):
    ids = {q['id'] for q in load_questions()}
    missing = [i for i in args.ids if i not in ids]
    if missing:
        sys.exit('找不到：' + ' '.join(missing))
    with EventLog() as log:
        log.append([{'event_id': str(uuid.uuid4()), 'kind': 'closed', 'question_id': i,
                     'at': now_iso()} for i in args.ids])
    print('已收起 %d 題' % len(args.ids))


def cmd_take(args):
    pairs = take(os.environ.get('CLAUDE_CODE_SESSION_ID', ''), os.getcwd())
    print(format_answers(pairs) if pairs else '收件匣：沒有新的回覆。')


def cmd_wait(args):
    deadline = time.time() + args.timeout
    session = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    while time.time() < deadline:
        if deliverable(session, os.getcwd()):
            pairs = take(session, os.getcwd())
            if pairs:
                print(format_answers(pairs))
                return
        time.sleep(3)
    print('收件匣：等了 %d 秒沒有新回覆。' % args.timeout)
    sys.exit(2)


def cmd_hook(args):
    """給 Claude Code hook 用：stdin 是 hook 的 JSON，stdout 會進 agent 的上下文。"""
    try:
        # Windows PowerShell 5.1 透過管線送 stdin 會先加 UTF-8 BOM（來自 PR #1，Dave Tseng）
        data = json.loads(sys.stdin.read().lstrip('﻿') or '{}')
    except ValueError:
        data = {}
    pairs = take(data.get('session_id', ''), data.get('cwd') or os.getcwd())
    if pairs:
        print(format_answers(pairs), flush=True)
    touch_active(data.get('session_id', ''))


def cmd_list(args):
    for q in build_state()['questions']:
        if q['closed'] and not args.all:
            continue
        status = '已收起' if q['closed'] else ('已回答' if q['answers'] else '等回答')
        print('%s  %s  %s  （%s）' % (q['id'], status, q['title'], q['from']))


def cmd_status(args):
    try:
        if args.action == 'set':
            fields = {k: getattr(args, k) for k in TEXT_FIELDS + ('status',) if getattr(args, k) is not None}
            set_line(args.id, fields)
            print('已更新工作線：' + args.id)
        elif args.action == 'remove':
            remove_line(args.id)
            print('已移除工作線：' + args.id)
        else:
            board = read_board()
            lines = [l for l in board['lines'] if args.all or l.get('status') != 'done']
            if args.json:
                return print(json.dumps(dict(board, lines=lines), ensure_ascii=False, indent=2))
            if not lines:
                return print('看板上沒有工作線。')
            order = list(STATUSES)
            # 同狀態內最新消息在上（updated_at 由新到舊；ISO 字串可直接比較）
            lines = sorted(lines, key=lambda l: l.get('updated_at') or l.get('last_active') or '', reverse=True)
            for l in sorted(lines, key=lambda l: order.index(l['status']) if l.get('status') in order else 99):
                print('%s｜%s｜%s｜%s｜%s｜%s｜%s｜%s' % (
                    l.get('id'), l.get('session') or '-', l.get('branch') or '-', l.get('task') or '-',
                    STATUSES.get(l.get('status'), l.get('status')), l.get('progress') or '-',
                    l.get('blocker') or '-', l.get('next') or '-'))
    except BoardError as exc:
        sys.exit(str(exc))


def cmd_stop(args):
    pidfile = home() / 'server.pid'
    if not pidfile.exists():
        return print('沒有記錄中的服務')
    try:
        os.kill(int(pidfile.read_text()), signal.SIGTERM)
        print('已停止')
    except (OSError, ValueError):  # Windows 找不到行程時是 OSError，不是 ProcessLookupError
        print('服務本來就沒在跑')
    pidfile.unlink()


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):  # Windows 管線預設 cp950，hook 的中文與路徑會壞
        if stream is not None and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8')
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog='inbox', description='Claude Code 收件匣')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('ask', help='放一題進收件匣')
    p.add_argument('--title', required=True)
    p.add_argument('--body')
    p.add_argument('--body-file')
    p.add_argument('--option', action='append', help='A=說明，可重複')
    p.add_argument('--recommend', help='推薦的選項代號')
    p.add_argument('--info', action='store_true', help='只是知會，使用者按「我知道了」')
    p.add_argument('--deadline')
    p.add_argument('--from', dest='source', help='顯示的來源名稱，預設是專案資料夾名')
    p.set_defaults(func=cmd_ask)
    p = sub.add_parser('take', help='取出這個 session 的新回覆')
    p.set_defaults(func=cmd_take)
    p = sub.add_parser('wait', help='等到有新回覆才結束')
    p.add_argument('--timeout', type=int, default=1800)
    p.set_defaults(func=cmd_wait)
    p = sub.add_parser('status', help='進度看板：回報／查看工作線狀態')
    actions = p.add_subparsers(dest='action', required=True)
    s = actions.add_parser('set', help='新增或更新一條工作線（只改有給的欄位）')
    s.add_argument('id')
    s.add_argument('--status', help='進行中／卡住／待驗收／完成／閒置（或 active/blocked/review/done/idle）')
    for name, text in (('session', 'session 名稱'), ('project', '專案根'), ('path', '工作路徑'),
                       ('branch', '分支'), ('task', '需求'), ('progress', '進度一句話'),
                       ('blocker', '卡點，給空字串可清掉'), ('next', '下一步')):
        s.add_argument('--' + name, help=text)
    s = actions.add_parser('list', help='列出工作線')
    s.add_argument('--json', action='store_true')
    s.add_argument('--all', action='store_true', help='連完成的也列')
    s = actions.add_parser('remove', help='移除一條工作線')
    s.add_argument('id')
    p.set_defaults(func=cmd_status)
    p = sub.add_parser('close', help='把處理完的題目收起來')
    p.add_argument('ids', nargs='+')
    p.set_defaults(func=cmd_close)
    p = sub.add_parser('list', help='列出題目')
    p.add_argument('--all', action='store_true')
    p.set_defaults(func=cmd_list)
    sub.add_parser('hook', help='給 Claude Code hook 用').set_defaults(func=cmd_hook)
    sub.add_parser('serve', help='前景執行網頁服務').set_defaults(func=lambda a: serve(load_config()))
    sub.add_parser('start', help='背景啟動網頁服務').set_defaults(
        func=lambda a: print(page_url(load_config()) if ensure_server(load_config()) else '啟動失敗'))
    sub.add_parser('stop', help='停止背景服務').set_defaults(func=cmd_stop)
    sub.add_parser('url', help='印出網址').set_defaults(func=lambda a: print(page_url(load_config())))
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
