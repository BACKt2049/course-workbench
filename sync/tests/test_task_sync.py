"""Regression cases for lost completion, incremental sync and legacy migration."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_sync
import store
import browser_login
from parsers import zhs_work, tju_exams, tju_courses


def task(account='account-a', course='course-a', hid='hw-a', **values):
    ref = dict(provider='zhs', accountKey=account, courseId=course, homeworkId=hid, kind='homework')
    return dict(id=task_sync.stable_id(ref), sourceRef=ref, src='zhs',
                title='设计模式习题二', courseName='设计模式', due='2030-11-01T23:59',
                done=False, **values)


class TaskSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        for name, value in dict(ROOT=self.root, DATA=self.root/'data', STATE=self.root/'state',
                                RAW=self.root/'raw', LATEST=self.root/'data/latest.json').items():
            self.stack.enter_context(patch.object(store, name, value))
        for folder in (store.DATA, store.STATE, store.RAW):
            folder.mkdir()

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_completed_task_survives_reminder_omission(self):
        completed = task(); completed['done'] = True
        result, _ = task_sync.reconcile([completed], [task(hid='hw-b')])
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0]['done'])

    def test_rename_and_extension_keep_identity_and_completion(self):
        old = task(); old.update(done=True, note='my note')
        incoming = task(); incoming.update(title='新标题', due='2030-12-01T23:59')
        result, _ = task_sync.reconcile([old], [incoming])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], old['id'])
        self.assertEqual(result[0]['title'], '新标题')
        self.assertEqual(result[0]['note'], 'my note')
        self.assertTrue(result[0]['done'])

    def test_identical_refresh_is_idempotent(self):
        first, _ = task_sync.reconcile([], [task()])
        second, stats = task_sync.reconcile(first, [task()])
        self.assertEqual(first, second)
        self.assertEqual(stats['unchanged'], 1)

    def test_cross_account_and_course_do_not_merge(self):
        tasks = [task(), task(account='account-b'), task(course='course-b')]
        result, _ = task_sync.reconcile([], tasks)
        self.assertEqual(len(result), 3)
        self.assertEqual(len({x['id'] for x in result}), 3)

    def test_unique_legacy_title_can_bind_after_extension(self):
        old = dict(id='legacy-id', src='zhs', title='设计模式习题二', courseName='设计模式', done=True, due='2020-01-01')
        result, _ = task_sync.reconcile([old], [task()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], 'legacy-id')
        self.assertTrue(result[0]['done'])
        self.assertEqual(result[0]['sourceRef']['homeworkId'], 'hw-a')
        old['type'] = '考试'
        exam = task(); exam['sourceRef']['kind'] = 'exam'
        result, _ = task_sync.reconcile([old], [exam])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['sourceRef']['kind'], 'exam')
        sparse = task(); sparse['sourceRef']['accountKey'] = ''
        merged, _ = task_sync.reconcile([task()], [sparse])
        self.assertEqual(merged[0]['sourceRef']['accountKey'], 'account-a')

    def test_ambiguous_legacy_titles_are_not_merged(self):
        old = [dict(id=k, src='zhs', title='设计模式习题二', courseName='设计模式') for k in ('a','b')]
        result, _ = task_sync.reconcile(old, [task()])
        self.assertEqual(len(result), 3)

    def test_old_title_hash_collision_does_not_merge_courses(self):
        old = dict(id='same-old-id', title='作业一', courseName='课程甲', src='zhs', done=True)
        new = task(course='course-b'); new.update(title='作业一', courseName='课程乙', legacyIds=['same-old-id'])
        result, _ = task_sync.reconcile([old], [new])
        self.assertEqual(len(result), 2)
        self.assertFalse(result[1]['done'])

    def test_platform_submission_completes_only_automatic_task(self):
        result, _ = task_sync.reconcile([task()], [task(submissionStatus='submitted')])
        self.assertTrue(result[0]['done'])
        explicit = task(workStatus='todo', workStatusExplicit=True, workStatusOrigin='user', workStatusUpdatedAt=10)
        result, _ = task_sync.reconcile([explicit], [task(submissionStatus='submitted')])
        self.assertFalse(result[0]['done'])
        self.assertEqual(result[0]['submissionStatus'], 'submitted')

    def test_reopen_beats_older_completed_snapshot(self):
        old = task(); old['done'] = True
        reopen = task(workStatus='todo', workStatusExplicit=True, workStatusOrigin='user', workStatusUpdatedAt=20)
        result, _ = task_sync.reconcile([old], [reopen], local=True)
        result, _ = task_sync.reconcile(result, [old], local=True)
        self.assertFalse(result[0]['done'])

    def test_unknown_numeric_status_is_not_guessed(self):
        self.assertEqual(task_sync.submission({'status':4,'isComplete':'0'})['submissionStatus'], 'unknown')
        self.assertEqual(task_sync.submission({'statusName':'已批改'})['submissionStatus'], 'graded')

    def test_deleted_task_is_not_recreated(self):
        result, _ = task_sync.reconcile([], [task()], deleted=[task()])
        self.assertEqual(result, [])

    def test_workspace_completion_reaches_latest_and_survives_sync(self):
        old = task(); store.write_json(store.LATEST, {'ddl':[old]})
        old['done'] = True
        workspace = dict(settings={}, courses=[], notes=[], ddl=[old])
        store.write_json(store.DATA/'workspace.json', workspace)
        store.merge_latest('zhs', {'ddl':[task(hid='hw-b')]}, replace=True)
        latest = store.read_latest()['ddl']
        self.assertEqual(len(latest), 2)
        self.assertTrue(latest[0]['done'])
        restored = store.save_workspace(workspace)
        self.assertEqual(len(restored['ddl']), 2)
        self.assertTrue(store.read_latest()['ddl'][0]['done'])

    def test_stale_workspace_does_not_undo_new_source_fields(self):
        old = task()
        current = task(submissionStatus='submitted'); current['title'] = '最新要求'
        store.write_json(store.LATEST, {'ddl':[current]})
        saved = store.save_workspace(dict(settings={}, courses=[], notes=[], ddl=[old]))
        self.assertEqual(saved['ddl'][0]['title'], '最新要求')
        self.assertTrue(saved['ddl'][0]['done'])

    def test_parser_identity_not_derived_from_title_or_due(self):
        raw = dict(id=11, courseId=22, title='作业一', endTime='2030-11-01 23:59', courseName='课程甲')
        def parse(row, account='a'):
            blob='<script>window.__API_0__ = '+json.dumps({'rt':[row]})+';</script>'
            return zhs_work.parse_api(blob, account_key=account)['items'][0]
        original = parse(raw)
        changed = parse(dict(raw, title='更新标题', endTime='2030-12-01 23:59'))
        self.assertEqual(original['id'], changed['id'])
        self.assertNotEqual(original['id'], parse(raw, 'b')['id'])

    def test_no_replacement_or_duplicates_in_other_collections(self):
        store.merge_latest('tju', {'courses':[{'id':'a','name':'旧标题'}]}, replace=True)
        store.merge_latest('tju', {'courses':[{'id':'b','name':'其他课'}]}, replace=True)
        store.merge_latest('tju', {'courses':[{'id':'a','name':'新标题'}]}, replace=True)
        courses = store.read_latest()['courses']
        self.assertEqual(len(courses), 2)
        self.assertEqual(courses[0]['name'], '新标题')

    def test_hyphen_dates_and_room_names_are_not_unset(self):
        html='<table><tr><th>课程名称</th><th>考试日期</th><th>考试地点</th></tr><tr><td>设计模式</td><td>2030-11-01 09:00</td><td>A-101</td></tr></table>'
        rows = tju_exams.parse(html)['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['place'], 'A-101')

    def test_rendered_course_rowspan_is_not_changed_by_fallback_fix(self):
        html='<table id="manualArrangeCourseTable"><tbody><tr><td>第一节 08:30-09:15</td><td id="TD12_0" rowspan="2" title="设计模式 (教师甲);(4-11 周,A101)"></td></tr><tr><td>第二节 09:25-10:10</td></tr></tbody></table>'
        result=tju_courses.parse(html)['items']
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]['weekday'],result[0]['start'],result[0]['end']), (2,'08:30','10:10'))

    def test_duplicate_login_reuses_session_and_cancel_expires_it(self):
        with patch.object(browser_login,'SESSIONS',{}), patch.object(browser_login.threading,'Thread') as worker:
            first=browser_login.start('zhs')
            self.assertIs(browser_login.start('zhs'), first)
            self.assertEqual(worker.call_count, 1)
            browser_login.cancel('zhs')
            self.assertEqual(first.public()['state'], 'expired')
            self.assertIsNot(browser_login.start('zhs'), first)

    def test_qr_expiry_starts_when_image_is_ready(self):
        session=browser_login.Session('zhs')
        session.created_at-=70
        import time
        session.set(ready_at=time.time(), state='qr_ready')
        self.assertGreaterEqual(session.public()['remain'], 178)


if __name__ == '__main__':
    unittest.main()
