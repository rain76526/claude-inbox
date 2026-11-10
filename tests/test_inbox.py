"""收件匣單元測試：python3 -m unittest discover tests"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'inbox'))
import inbox  # noqa: E402

REAL_ENSURE_SERVER = inbox.ensure_server  # Base 會把它換成替身，這裡先留一份真的


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'proj'
        self.project.mkdir()
        self.env = mock.patch.dict(os.environ, {'CLAUDE_INBOX_HOME': str(Path(self.tmp.name) / 'home'),
                                                'CLAUDE_CODE_SESSION_ID': 'sess-1'})
        self.env.start()
        self.cwd = os.getcwd()
        os.chdir(self.project)
        self.no_server = mock.patch.object(inbox, 'ensure_server', return_value=True)
        self.no_server.start()

    def tearDown(self):
        self.no_server.stop()
        os.chdir(self.cwd)
        self.env.stop()
        self.tmp.cleanup()

    def ask(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['ask'] + list(argv))
        return out.getvalue().split('已放進收件匣：')[1].split()[0]

    def answer(self, qid, choice='A', text=''):
        questions = {q['id']: q for q in inbox.load_questions()}
        events = inbox.validate_answers({'answers': [{'question_id': qid, 'choice': choice, 'text': text}]}, questions)
        with inbox.EventLog() as log:
            log.append(events)
        return events[0]


class AskAndAnswer(Base):
    def test_ask_writes_question_with_session_and_project(self):
        qid = self.ask('--title', '要哪個？', '--option', 'A=甲', '--option', 'B=乙', '--recommend', 'B')
        q = inbox.load_questions()[0]
        self.assertEqual(q['id'], qid)
        self.assertEqual(q['session'], 'sess-1')
        self.assertEqual(q['project'], os.path.realpath(str(self.project)))
        self.assertEqual([o['recommended'] for o in q['options']], [False, True])

    def test_bad_option_and_bad_recommend_rejected(self):
        with self.assertRaises(SystemExit):
            self.ask('--title', 't', '--option', 'A')
        with self.assertRaises(SystemExit):
            self.ask('--title', 't', '--option', 'A=甲', '--recommend', 'Z')

    def test_validate_rejects_unknown_choice_empty_answer_and_long_text(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        qs = {q['id']: q for q in inbox.load_questions()}
        for bad in ({'question_id': qid, 'choice': 'Z'}, {'question_id': qid},
                    {'question_id': qid, 'text': 'x' * 4001}, {'question_id': 'nope', 'choice': 'A'}):
            with self.assertRaises(ValueError):
                inbox.validate_answers({'answers': [bad]}, qs)

    def test_info_question_only_accepts_ack(self):
        qid = self.ask('--info', '--title', '知會')
        qs = {q['id']: q for q in inbox.load_questions()}
        self.assertEqual(inbox.validate_answers({'answers': [{'question_id': qid}]}, qs)[0]['choice'], 'ack')
        with self.assertRaises(ValueError):
            inbox.validate_answers({'answers': [{'question_id': qid, 'choice': 'A'}]}, qs)


class Delivery(Base):
    def test_answer_goes_to_asking_session_once(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid, 'A', '補充')
        self.assertEqual(inbox.take('sess-2', str(self.project)), [])  # 別的 session 拿不到
        pairs = inbox.take('sess-1', str(self.project))
        self.assertEqual(len(pairs), 1)
        self.assertIn('使用者選：A 甲', inbox.format_answers(pairs))
        self.assertIn('補充：補充', inbox.format_answers(pairs))
        self.assertEqual(inbox.take('sess-1', str(self.project)), [])  # 只送一次

    def test_asking_session_gets_answer_even_from_another_directory(self):
        sub_dir = self.project / 'a' / 'b'
        sub_dir.mkdir(parents=True)
        os.chdir(sub_dir)  # 非 git 資料夾：出題與 hook 的 cwd 不同，專案根就不同
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid)
        self.assertEqual(len(inbox.take('sess-1', str(self.project))), 1)

    def test_other_project_cannot_take_over_even_old_answers_or_empty_sessions(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        event = self.answer(qid)
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        later = datetime.fromisoformat(event['at']).timestamp() + inbox.ORPHAN_SECONDS + 1
        with mock.patch.object(inbox.time, 'time', return_value=later):
            self.assertEqual(inbox.take('sess-2', str(other)), [])  # 過了 30 分鐘也只限同專案
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': ''}):
            anon = self.ask('--title', '匿名', '--option', 'A=甲')
        self.answer(anon)
        self.assertEqual(inbox.take('', str(other)), [])  # 空 session 對空 session 不算同一個

    def test_other_project_never_gets_it(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid)
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        self.assertEqual(inbox.take('sess-2', str(other)), [])  # 別的專案、別的 session

    def test_orphan_answer_goes_to_same_project_after_30_minutes(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        event = self.answer(qid)
        self.assertEqual(inbox.take('sess-2', str(self.project)), [])
        later = datetime.fromisoformat(event['at']).timestamp() + inbox.ORPHAN_SECONDS + 1
        with mock.patch.object(inbox.time, 'time', return_value=later):
            self.assertEqual(len(inbox.take('sess-2', str(self.project))), 1)

    def test_hook_reads_stdin_and_prints_answers(self):
        qid = self.ask('--title', '問題標題', '--option', 'A=甲')
        self.answer(qid)
        stdin = io.StringIO(json.dumps({'session_id': 'sess-1', 'cwd': str(self.project)}))
        with mock.patch.object(sys, 'stdin', stdin), redirect_stdout(io.StringIO()) as out:
            inbox.main(['hook'])
        self.assertIn('問題標題', out.getvalue())
        with mock.patch.object(sys, 'stdin', io.StringIO('{}')), redirect_stdout(io.StringIO()) as out:
            inbox.main(['hook'])
        self.assertEqual(out.getvalue(), '')  # 沒有新回覆時什麼都不印

    def test_hook_ignores_utf8_bom_on_stdin(self):
        # Windows PowerShell 5.1 透過管線送 stdin 會先加 UTF-8 BOM（來自 PR #1）
        qid = self.ask('--title', 'BOM 題', '--option', 'A=甲')
        self.answer(qid)
        stdin = io.StringIO('﻿' + json.dumps({'session_id': 'sess-1', 'cwd': str(self.project)}))
        with mock.patch.object(sys, 'stdin', stdin), redirect_stdout(io.StringIO()) as out:
            inbox.main(['hook'])
        self.assertIn('BOM 題', out.getvalue())

    def test_locks_use_separate_lock_files_not_data_files(self):
        # 鎖獨立的 .lock 檔：Windows 的 msvcrt 鎖會擋住別人讀資料檔的第一個位元組（來自 PR #1）
        locked = []
        real_lock = inbox.lock_file

        def spy(fh):
            name = fh.name if isinstance(fh.name, str) else 'events.jsonl'  # os.fdopen 的 name 是 fd
            locked.append(os.path.basename(name))
            real_lock(fh)

        with mock.patch.object(inbox, 'lock_file', spy):
            inbox.load_config()
            qid = self.ask('--title', 't', '--option', 'A=甲')
            with inbox.EventLog() as log:
                log.append([{'event_id': 'e1', 'kind': 'answered', 'question_id': qid,
                             'choice': 'A', 'text': '', 'at': inbox.now_iso()}])
                # 持鎖期間，別的讀者仍能直接讀 events.jsonl
                self.assertIn('e1', inbox.events_path().read_text(encoding='utf-8'))
        self.assertIn('config.lock', locked)
        self.assertIn('events.lock', locked)
        self.assertNotIn('events.jsonl', locked)

    def test_close_hides_question_from_list(self):
        qid = self.ask('--title', '要收的題')
        with redirect_stdout(io.StringIO()):
            inbox.main(['close', qid])
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['list'])
        self.assertNotIn('要收的題', out.getvalue())
        self.assertTrue(inbox.build_state()['questions'][0]['closed'])


class Board(Base):
    def test_set_creates_then_merges_only_given_fields(self):
        inbox.set_line('inbox-board', {'task': '看板', 'status': '進行中', 'progress': '寫規格'})
        line = inbox.read_board()['lines'][0]
        self.assertEqual((line['id'], line['status'], line['task']), ('inbox-board', 'active', '看板'))
        self.assertEqual(line['session_id'], 'sess-1')  # 自動帶入
        self.assertEqual(line['project'], os.path.realpath(str(self.project)))
        self.assertTrue(line['updated_at'] and line['last_active'])
        inbox.set_line('inbox-board', {'progress': '實作中', 'status': 'blocked', 'blocker': '等回覆'})
        line = inbox.read_board()['lines'][0]
        self.assertEqual((line['task'], line['progress'], line['status'], line['blocker']),
                         ('看板', '實作中', 'blocked', '等回覆'))
        inbox.set_line('inbox-board', {'blocker': ''})
        self.assertEqual(inbox.read_board()['lines'][0]['blocker'], '')
        self.assertEqual(len(inbox.read_board()['lines']), 1)

    def test_set_does_not_overwrite_auto_fields_of_existing_line(self):
        inbox.set_line('a', {'task': 't'})
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        os.chdir(other)
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'sess-2'}):
            inbox.set_line('a', {'progress': 'p'})
        line = inbox.read_board()['lines'][0]
        self.assertEqual(line['project'], os.path.realpath(str(self.project)))
        self.assertEqual(line['session_id'], 'sess-1')

    def test_remove_line(self):
        inbox.set_line('a', {'task': 't'})
        inbox.set_line('b', {'task': 't'})
        inbox.remove_line('a')
        self.assertEqual([l['id'] for l in inbox.read_board()['lines']], ['b'])
        with self.assertRaises(inbox.BoardError):
            inbox.remove_line('a')

    def test_validation_rejects_bad_id_status_long_text_and_too_many_lines(self):
        for bad_id in ('', 'a b', 'x' * 65, '../x'):
            with self.assertRaises(inbox.BoardError):
                inbox.set_line(bad_id, {'task': 't'})
        with self.assertRaises(inbox.BoardError):
            inbox.set_line('a', {'status': '亂寫'})
        with self.assertRaises(inbox.BoardError):
            inbox.set_line('a', {'progress': 'x' * 501})
        with mock.patch.object(inbox, 'MAX_LINES', 2):
            inbox.set_line('a', {})
            inbox.set_line('b', {})
            with self.assertRaises(inbox.BoardError):
                inbox.set_line('c', {})
            inbox.set_line('a', {'task': '改既有的還是可以'})

    def test_corrupt_board_is_never_overwritten(self):
        inbox.home()
        path = Path(os.environ['CLAUDE_INBOX_HOME']) / 'board.json'
        path.write_text('{not json', encoding='utf-8')
        with self.assertRaises(inbox.BoardError):
            inbox.set_line('a', {'task': 't'})
        with self.assertRaises(inbox.BoardError):
            inbox.remove_line('a')
        self.assertEqual(path.read_text(encoding='utf-8'), '{not json')
        with self.assertRaises(inbox.BoardError):
            inbox.read_board()

    def test_missing_board_reads_empty_and_uses_board_lock(self):
        self.assertEqual(inbox.read_board(), {'version': 1, 'lines': []})
        locked = []
        real_lock = inbox.lock_file

        def spy(fh):
            locked.append(os.path.basename(fh.name))
            real_lock(fh)

        with mock.patch.object(inbox, 'lock_file', spy):
            inbox.set_line('a', {})
        self.assertEqual(locked, ['board.lock'])


class Server(Base):
    def start(self, host='127.0.0.1', token=''):
        port = free_port()
        inbox.Handler.config = {'host': host, 'port': port, 'token': token}
        httpd = ThreadingHTTPServer((host, port), inbox.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return port

    def req(self, port, path, data=None, headers=None):
        h = {'Content-Type': 'application/json'}
        h.update(headers or {})
        r = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                   data=json.dumps(data).encode() if data is not None else None, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_answer_round_trip(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        port = self.start()
        self.assertEqual(self.req(port, '/')[0], 200)
        code, body = self.req(port, '/api/answer', {'answers': [{'question_id': qid, 'choice': 'A'}]})
        self.assertEqual((code, json.loads(body)), (200, {'stored': 1}))
        state = json.loads(self.req(port, '/api/state')[1])
        self.assertEqual(state['questions'][0]['answers'][0]['choice'], 'A')

    def test_bad_host_origin_and_invalid_answer_rejected(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        port = self.start()
        self.assertEqual(self.req(port, '/api/state', headers={'Host': 'evil.example:80'})[0], 403)
        self.assertEqual(self.req(port, '/api/answer', {'answers': [{'question_id': qid, 'choice': 'A'}]},
                                  headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.req(port, '/api/answer', {'answers': [{'question_id': qid, 'choice': 'Z'}]})[0], 400)
        self.assertEqual(inbox.read_events(), [])

    def raw_post(self, port, path, body, headers):
        """用原始 socket 送 POST，回傳狀態碼；連線被 RST 時會丟 ConnectionError。"""
        head = ['POST %s HTTP/1.1' % path, 'Host: 127.0.0.1:%d' % port, 'Connection: close']
        head += ['%s: %s' % kv for kv in headers.items()]
        with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
            s.sendall(('\r\n'.join(head) + '\r\n\r\n').encode())
            time.sleep(0.1)  # 本文晚到：服務若沒等本文就回應關線，Windows 會送 RST，用戶端收不到回應
            s.sendall(body)
            time.sleep(0.1)
            data = b''
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        self.assertTrue(data, '服務沒回任何東西就關線')
        return int(data.split(b' ', 2)[1])

    def test_post_errors_reply_cleanly_after_reading_body(self):
        # 錯誤回應前先讀完本文：Windows 關閉有未讀資料的連線會送 RST，用戶端收不到狀態碼（來自 PR #1）
        port = self.start()
        body = json.dumps({'answers': [{'question_id': 'x', 'text': 'y' * 60000}]}).encode()
        base = {'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        self.assertEqual(self.raw_post(port, '/api/nope', body, base), 404)
        self.assertEqual(self.raw_post(port, '/api/answer', body, dict(base, Origin='http://evil.example')), 403)
        self.assertEqual(self.raw_post(port, '/api/answer', body, dict(base, **{'Content-Type': 'text/plain'})), 415)
        self.assertEqual(self.raw_post(port, '/api/answer', b'', dict(base, **{'Content-Length': 'abc'})), 413)

    def test_bind_permission_error_explains_reserved_port_on_windows(self):
        boom = PermissionError(13, 'WinError 10013')
        with mock.patch.object(inbox, 'WINDOWS', True), \
                mock.patch.object(inbox, 'Server', side_effect=boom), \
                redirect_stdout(io.StringIO()) as out, self.assertRaises(PermissionError):
            inbox.serve({'host': '127.0.0.1', 'port': 7799, 'token': ''})
        self.assertIn('excludedportrange', out.getvalue())
        self.assertIn('config.json', out.getvalue())
        self.assertFalse((Path(os.environ['CLAUDE_INBOX_HOME']) / 'server.pid').exists())

    def test_token_required_when_configured(self):
        port = self.start(token='secret-token')
        self.assertEqual(self.req(port, '/api/state')[0], 401)
        self.assertEqual(self.req(port, '/api/state', headers={'X-Inbox-Token': 'secret-token'})[0], 200)

    def test_other_program_returning_200_is_not_taken_for_inbox(self):
        from http.server import BaseHTTPRequestHandler

        class Fake(BaseHTTPRequestHandler):
            body = b'<html>spa fallback</html>'

            def do_GET(self):
                body = Fake.body
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        port = free_port()
        httpd = ThreadingHTTPServer(('127.0.0.1', port), Fake)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.assertFalse(inbox.server_alive({'host': '127.0.0.1', 'port': port, 'token': ''}))
        for body in (b'{}', b'["questions"]', b'"questions"'):
            Fake.body = body
            self.assertFalse(inbox.server_alive({'host': '127.0.0.1', 'port': port, 'token': ''}))
        Fake.body = b'<html>spa fallback</html>'
        real = self.start()
        self.assertTrue(inbox.server_alive({'host': '127.0.0.1', 'port': real, 'token': ''}))

    def test_background_server_log_never_contains_token(self):
        import signal
        port = free_port()
        inbox.home()
        (Path(os.environ['CLAUDE_INBOX_HOME']) / 'config.json').write_text(json.dumps({'port': port}))
        config = inbox.load_config()
        pid_file = Path(os.environ['CLAUDE_INBOX_HOME']) / 'server.pid'

        try:
            with mock.patch.object(inbox, 'ensure_server', REAL_ENSURE_SERVER):
                self.assertTrue(inbox.ensure_server(config))
            log = (Path(os.environ['CLAUDE_INBOX_HOME']) / 'server.log').read_text(encoding='utf-8')
            self.assertIn('收件匣服務已啟動', log)
            self.assertNotIn(config['token'], log)
        finally:  # 必須在測試本體清：tearDown 會先刪掉暫存資料夾與 PID 檔
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGTERM)
                except (OSError, ValueError):
                    pass
                time.sleep(0.5)  # Windows：等行程放掉 server.log，暫存資料夾才刪得掉

    @unittest.skipIf(os.name == 'nt', 'Windows 沒有 POSIX 權限位元；改靠使用者設定檔資料夾的 ACL')
    def test_loopback_default_also_gets_private_token_and_folder(self):
        old = os.umask(0o022)  # 在寬鬆的 umask 下測，權限被放寬時才會變紅
        self.addCleanup(os.umask, old)
        config = inbox.load_config()
        path = Path(os.environ['CLAUDE_INBOX_HOME']) / 'config.json'
        self.assertTrue(config['token'])
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), '0o700')
        self.assertEqual(inbox.load_config()['token'], config['token'])  # 第二次不重產
        qid = self.ask('--title', 't')
        self.answer(qid, choice='', text='x')
        self.assertEqual(oct(inbox.events_path().stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct((path.parent / 'questions').stat().st_mode & 0o777), '0o700')

    def test_non_loopback_host_gets_token_generated(self):
        path = Path(os.environ['CLAUDE_INBOX_HOME']) / 'config.json'
        inbox.home()
        path.write_text(json.dumps({'host': '100.64.0.1'}))
        config = inbox.load_config()
        self.assertTrue(config['token'])
        if os.name != 'nt':
            self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertIn('?t=' + config['token'], inbox.page_url(config))


if __name__ == '__main__':
    unittest.main()
