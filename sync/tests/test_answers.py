"""答案中心回归：全部离线运行，不使用真实账号或平台请求。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import store
import pipeline
import ui_assets
from zhs_client import ZhsClient, PlatformError, account_key, normalize_storage_state
from answers.api import AnswerJobs, scope_of
from answers.models import record
from answers.normalize import flag, normalize_questions, availability, discover_courses
from answers.repository import AnswerRepository
from answers.service import AnswerService
from answers.zhs_provider import ZhsHomeworkProvider
from answers.exporters import export_records


STATE = {'cookies': [{'name': 'CASLOGC', 'value': json.dumps({'uuid': 'test-user-a'})}], 'origins': []}
ACCOUNT = account_key(STATE)
COURSE = {'courseId': 'c1', 'courseName': '设计模式'}


def paper(*questions):
    return {'groupDtoList': [{'id': 'g1', 'groupName': '选择题', 'questionDtoList': list(questions)}]}


def question(no='10-1', marks=('0', '1'), kind='单选题'):
    return {'questionId': str(no), 'no': no, 'questionName': kind, 'content': '<p>题干</p>',
            'questionOptionList': [{'content': '选项' + str(i), 'isCorrect': m} for i, m in enumerate(marks)]}


def entry(hid='h1', state='available'):
    value = record(COURSE, {'homeworkId': hid, 'title': '习题二', 'paperId': 12}, ACCOUNT)
    value['state'] = state
    if state == 'available':
        value['questions'] = normalize_questions(paper(question()))
        value.update(questionCount=1, answeredCount=1)
    return value


class ParseTests(unittest.TestCase):
    def test_mark_types_and_missing(self):
        for value in (True, 1, '1', 'TRUE', ' true '):
            self.assertIs(flag(value), True)
        for value in (False, 0, '0', 'FALSE', ' false '):
            self.assertIs(flag(value), False)
        for value in (None, '', 'unknown', 2, [], {}):
            self.assertIsNone(flag(value))

    def test_single_multi_judgment_and_duplicate_numbers(self):
        rows = normalize_questions(paper(question(), question('10-2', (1, 0, True), '多选题'),
                                         question('10-2', ('false', 'true'), '判断题')))
        self.assertEqual([q['answer'] for q in rows], [['B'], ['A', 'C'], ['B']])
        self.assertEqual([q['no'] for q in rows], ['10-1', '10-2', '10-2'])
        self.assertEqual([q['position'] for q in rows], [0, 1, 2])
        self.assertEqual(availability(rows), ('available', 3))

    def test_missing_or_all_false_never_means_complete(self):
        rows = normalize_questions(paper(question(marks=(True, None)), question(marks=(0, '0'))))
        self.assertEqual(availability(rows), ('partial', 0))
        self.assertEqual([q['answer'] for q in rows], [[], []])

    def test_grade_card_and_empty_paper_rejected(self):
        for value in ({'groupDtoList': [{'questionDtos': [{'isCorrect': True}]}]}, paper()):
            with self.assertRaises(ValueError):
                normalize_questions(value)

    def test_explicit_labels_and_unsupported(self):
        q = question(marks=(1, 0))
        q['questionOptionList'][0]['label'] = 'C'
        rows = normalize_questions(paper(q, question('2', (1, 0), '填空题')))
        self.assertEqual(rows[0]['answer'], ['C'])
        self.assertEqual(rows[1]['state'], 'unsupported')
        self.assertEqual(rows[1]['answer'], [])

    def test_platform_group_id_is_preserved(self):
        value = paper(question()); value['groupDtoList'][0]['groupId'] = 'remote-group'
        self.assertEqual(normalize_questions(value)[0]['groupId'], 'remote-group')

    def test_courses_captured_without_pending(self):
        blob = '<script>window.__API_0__ = ' + json.dumps({'data': {'list': [COURSE]}}) + ';</script>'
        self.assertEqual(discover_courses(blob), [COURSE])


class ClientTests(unittest.TestCase):
    def test_legacy_null_cookie_fields_are_normalized_without_mutation(self):
        state = copy.deepcopy(STATE)
        state['cookies'][0].update(sameSite=None, expires=None, httpOnly=None, secure=None, path=None)
        cleaned = normalize_storage_state(state)
        self.assertEqual(cleaned['cookies'][0]['sameSite'], 'Lax')
        self.assertIs(cleaned['cookies'][0]['httpOnly'], False)
        self.assertEqual(cleaned['cookies'][0]['expires'], -1)
        self.assertEqual(cleaned['cookies'][0]['path'], '/')
        self.assertIsNone(state['cookies'][0]['expires'])

    def client(self, *responses):
        transport = Mock(spec=['fetch'])
        transport.fetch.side_effect = list(responses)
        return ZhsClient(STATE, transport), transport

    def response(self, payload=None, status=200):
        return Mock(status=status, text=Mock(return_value=json.dumps(payload)))

    def test_success_business_string_status_and_same_run_reuse(self):
        c, transport = self.client(self.response({'status': '200', 'rt': {}}))
        c.paper('1'); c.paper('1')
        self.assertEqual(transport.fetch.call_count, 1)

    def test_timeout_and_temporary_failure_retry_once(self):
        for first in (TimeoutError(), self.response(status=503)):
            c, transport = self.client(first, self.response({'status': 200, 'rt': {}}))
            self.assertEqual(c.get('/x')['status'], 200)
            self.assertEqual(transport.fetch.call_count, 2)

    def test_exhausted_timeout_cached_and_sanitized(self):
        c, transport = self.client(TimeoutError('secret-url'), TimeoutError('secret-url'))
        for _ in range(2):
            with self.assertRaises(PlatformError) as error:
                c.get('/x')
            self.assertEqual(error.exception.kind, 'network')
            self.assertNotIn('secret-url', str(error.exception))
        self.assertEqual(transport.fetch.call_count, 2)

    def test_business_closed_login_and_arbitrary_failure_distinct(self):
        for msg, expected in (('答案尚未公布', 'closed'), ('登录已过期', 'need_login'), ('服务异常', 'upstream')):
            c, transport = self.client(self.response({'status': 500, 'msg': msg}))
            with self.assertRaises(PlatformError) as error:
                c.get('/x')
            self.assertEqual(error.exception.kind, expected)
            self.assertEqual(transport.fetch.call_count, 1)

    def test_http_success_not_enough(self):
        c, _ = self.client(self.response({'rt': paper(question())}))
        with self.assertRaises(PlatformError) as error:
            c.get('/x')
        self.assertEqual(error.exception.kind, 'upstream')


class ProviderTests(unittest.TestCase):
    def test_paginate_51_including_completed(self):
        c = Mock(account_key=ACCOUNT, uuid='test')
        rows = [{'id': str(i), 'isComplete': 1} for i in range(51)]
        c.get.side_effect = [{'rt': {'list': rows[:50], 'rowCount': 51}}, {'rt': {'list': rows[50:], 'rowCount': 51}}]
        result = ZhsHomeworkProvider(c).homework(COURSE)
        self.assertEqual(len(result), 51)
        self.assertEqual(result[-1]['homeworkId'], '50')
        self.assertEqual(result[-1]['isComplete'], 1)
        self.assertEqual([call.args[1]['pageIndex'] for call in c.get.call_args_list], [1, 2])
        self.assertTrue(all(call.args[1]['completeTab'] == 0 for call in c.get.call_args_list))

    def test_incomplete_and_repeated_page_report_failure(self):
        for pages in ([{'rt': {'list': [], 'rowCount': 51}}],
                      [{'rt': {'list': [{'id': '1'}], 'rowCount': 51}}] * 2):
            c = Mock(); c.get.side_effect = pages
            with self.assertRaises(PlatformError):
                ZhsHomeworkProvider(c).homework(COURSE)

    def test_no_paper_avoids_question_endpoint(self):
        c = Mock(account_key=ACCOUNT, uuid='test')
        c.get.return_value = {'rt': {'fileList': []}}
        result = ZhsHomeworkProvider(c).fetch(COURSE, {'homeworkId': 'h1', 'paperId': 0})
        self.assertEqual(result['state'], 'no_paper')
        c.paper.assert_not_called()

    def test_closed_grade_card_does_not_make_answers(self):
        c = Mock(account_key=ACCOUNT, uuid='test')
        c.paper.side_effect = PlatformError('closed', '未公布')
        c.get.side_effect = [{'rt': {'groupDtoList': [{'questionDtos': [{'isCorrect': True, 'no': 1}]}]}}, {'rt': {}}]
        result = ZhsHomeworkProvider(c).fetch(COURSE, {'homeworkId': 'h1', 'paperId': 1})
        self.assertEqual(result['state'], 'closed')
        self.assertEqual(result['questions'], [])
        self.assertTrue(result['gradeCard'][0]['isCorrect'])

    def test_login_and_parse_errors_never_closed(self):
        c = Mock(account_key=ACCOUNT, uuid='test'); c.get.return_value = {'rt': {}}
        c.paper.side_effect = PlatformError('need_login', '请登录')
        provider = ZhsHomeworkProvider(c)
        self.assertEqual(provider.fetch(COURSE, {'homeworkId': 'h1', 'paperId': 1})['state'], 'need_login')
        c.paper.side_effect = None; c.paper.return_value = {'rt': {}}
        self.assertEqual(provider.fetch(COURSE, {'homeworkId': 'h1', 'paperId': 1})['state'], 'error')


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = AnswerRepository(Path(self.temp.name), ACCOUNT)

    def test_accounts_isolated_and_cross_account_write_rejected(self):
        self.repo.put(entry())
        other = AnswerRepository(Path(self.temp.name), 'a' * 24)
        self.assertEqual(other.listing(), [])
        with self.assertRaises(ValueError):
            other.put(entry())

    def test_failure_preserves_success_and_last_check(self):
        before = entry(); before['checkedAt'] = '2026-10-30T12:00:00+08:00'
        self.repo.put(before)
        for state in ('error', 'closed', 'need_login'):
            self.repo.put(entry(state=state))
            result = self.repo.get('c1', 'h1')
            self.assertEqual(result['state'], state)
            self.assertTrue(result['historical'])
            self.assertEqual(result['answersAt'], before['checkedAt'])
            self.assertEqual(result['questions'][0]['answer'], ['B'])

    def test_atomic_failure_keeps_old_file_no_temporary_left(self):
        self.repo.put(entry())
        with patch('answers.repository.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.repo.put(entry(state='error'))
        self.assertEqual(self.repo.get('c1', 'h1')['state'], 'available')
        self.assertEqual(list(Path(self.temp.name).rglob('*.tmp')), [])

    def test_invalid_paths_and_filters(self):
        self.repo.put(entry())
        for cid in ('../escape', 'C:\\x', '', 'a/b'):
            with self.assertRaises(ValueError):
                self.repo.get(cid, 'h1')
        self.assertEqual(len(self.repo.listing(query='设计')), 1)
        self.assertEqual(self.repo.listing(state='closed'), [])

    def test_service_partial_course_failure_and_continues(self):
        p = Mock(); p.courses.return_value = [COURSE, {'courseId': 'c2'}]
        p.homework.side_effect = [[{'homeworkId': 'h1'}], PlatformError('network', '超时')]
        p.fetch.return_value = entry()
        progress = []
        report = AnswerService(p, self.repo).refresh(on_progress=progress.append)
        self.assertTrue(report['partial'])
        self.assertEqual(report['processed'], 1)
        self.assertEqual(progress[-1]['current'], 1)
        self.assertEqual(self.repo.get('c1', 'h1')['state'], 'available')

    def test_three_export_formats_unknown_and_history(self):
        self.repo.put(entry()); self.repo.put(entry(state='closed'))
        historic = self.repo.get('c1', 'h1')
        for format_name in ('txt', 'md', 'json'):
            content, mime = export_records([historic, entry('h2', 'partial')], format_name)
            self.assertTrue(content)
            text = content.decode('utf-8')
            self.assertIn('2026-', text)
            self.assertIn('历史' if format_name != 'json' else 'historical', text)
        unknown = entry('h2', 'partial'); unknown['questions'] = normalize_questions(paper(question(marks=(None, None))))
        self.assertIn('未知', export_records([unknown], 'txt')[0].decode())


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for key, value in {'ROOT': self.base, 'STATE': self.base/'state', 'DATA': self.base/'data', 'LATEST': self.base/'data/latest.json'}.items():
            self.stack.enter_context(patch.object(store, key, value))
        store.STATE.mkdir(); store.DATA.mkdir()
        store.write_json(store.state_path('zhs'), STATE)

    def test_duplicate_refresh_and_account_scoped_jobs(self):
        jobs = AnswerJobs(); hold = threading.Event(); entered = threading.Event()
        def execute(job, scope):
            entered.set(); hold.wait(5)
        with patch.object(jobs, '_execute', side_effect=execute):
            first = jobs.start({}); self.assertTrue(entered.wait(2))
            second = jobs.start({})
            self.assertEqual(first['jobId'], second['jobId'])
            self.assertTrue(second['reused'])
            self.assertIsNone(jobs.lookup(first['jobId'], 'a'*24))
            hold.set()

    def test_scope_validation(self):
        for value in ({'homeworkId': 'h1'}, {'courseId': '../bad'}):
            with self.assertRaises(ValueError): scope_of(value)

    def test_browser_fetch_accepts_legacy_cookie_state(self):
        import zhs_qr
        state = copy.deepcopy(STATE)
        state['cookies'][0].update(sameSite=None, expires=None)
        store.write_json(store.state_path('zhs'), state)
        self.assertTrue(zhs_qr._clean_state_file())
        cookie = store.load_state('zhs')['cookies'][0]
        self.assertEqual(cookie['sameSite'], 'Lax')
        self.assertEqual(cookie['expires'], -1)

    def test_task_state_and_manual_files_kept_and_unique_link(self):
        task = {'id': 'old-id', 'src': 'zhs', 'courseName': '设计模式', 'title': '实验', 'due': '2026-11-01',
                'done': True, 'note': '我的笔记', 'minutes': 35, 'attachments': []}
        store.write_json(store.LATEST, {'ddl': [task]})
        incoming = dict(task, done=False, note='', minutes=90)
        store.merge_latest('zhs', {'ddl': [incoming]}, replace=True)
        detail = {'title': '实验', 'courseName': '设计模式', 'due': '2026-11-01',
                  'courseId': 'c1', 'homeworkId': 'h1', 'requirement': '提交代码', 'files': []}
        store.enrich_ddl({'h1': detail})
        result = store.read_latest()['ddl'][0]
        self.assertEqual((result['id'], result['done'], result['note'], result['minutes']), ('old-id', True, '我的笔记', 35))
        self.assertEqual(result['sourceRef']['homeworkId'], 'h1')
        task.pop('sourceRef', None); store.write_json(store.LATEST, {'ddl': [task]})
        store.enrich_ddl({'h1': detail, 'h2': dict(detail, homeworkId='h2')})
        self.assertNotIn('sourceRef', store.read_latest()['ddl'][0])

    def test_answer_failure_keeps_base_sync_saved(self):
        import zhs_qr
        fake_client = Mock(); fake_client.__enter__ = Mock(side_effect=PlatformError('network', '超时'))
        fake_client.__exit__ = Mock()
        parsed = {'items': [{'id': 'one', 'title': '本次任务'}], 'unparsed': []}
        with patch.object(zhs_qr, 'ensure_login', return_value=(True, '')), patch.object(zhs_qr, 'fetch_pages', return_value={'api': 'x'}), patch.object(pipeline.zhs_work, 'parse_api', return_value=parsed), patch('zhs_client.ZhsClient', return_value=fake_client):
            result = pipeline.pull(sites=('zhs',))
        self.assertTrue(result['partial'])
        self.assertEqual(store.read_latest()['ddl'][0]['id'], 'one')

    def test_http_guard_filters_export_and_account_switch(self):
        repo = AnswerRepository(store.ROOT/'answers', ACCOUNT); repo.save_courses([COURSE]); repo.put(entry())
        srv = server.LocalServer(('127.0.0.1', 0), server.Handler)
        host = '127.0.0.1:%s' % srv.server_port; server.ALLOWED_HOSTS.add(host)
        thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
        def req(path, body=None, token=True):
            request = urllib.request.Request('http://'+host+path, data=json.dumps(body).encode() if body is not None else None)
            if token: request.add_header('X-WB-Token', server.TOKEN)
            return urllib.request.urlopen(request, timeout=5)
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied: req('/api/answers', token=False)
            self.assertEqual(denied.exception.code, 401)
            with req('/api/answers?state=available') as response: self.assertEqual(len(json.load(response)['records']), 1)
            with req('/api/answers/c1/h1') as response: self.assertEqual(json.load(response)['record']['questions'][0]['answer'], ['B'])
            for fmt in ('txt', 'md', 'json'):
                with req('/api/answers/export', {'format': fmt, 'keys': ['zhs:c1:h1']}) as response:
                    self.assertIn('attachment;', response.headers['Content-Disposition']); self.assertTrue(response.read())
            other = copy.deepcopy(STATE); other['cookies'][0]['value'] = json.dumps({'uuid': 'test-user-b'})
            store.write_json(store.state_path('zhs'), other)
            with req('/api/answers') as response: self.assertEqual(json.load(response)['records'], [])
            with self.assertRaises(urllib.error.HTTPError) as missing: req('/api/answers/c1/h1')
            self.assertEqual(missing.exception.code, 404)
        finally:
            srv.shutdown(); srv.server_close(); thread.join(5); server.ALLOWED_HOSTS.discard(host)

    def test_shared_page_assembly_no_answer_cache_or_credentials(self):
        html = ui_assets.load_html()
        self.assertIn('answer-center-styles', html)
        self.assertIn('answer-center-script', html)
        self.assertIn('data-go="answers"', html)
        self.assertNotIn('test-user-a', html)
        self.assertGreater(html.index('<script id="answer-center-script">'), html.rfind('renderHome();'))


if __name__ == '__main__':
    unittest.main()
