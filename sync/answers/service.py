"""答案用例；客户端与仓库由调用方注入。"""
from .models import LABELS
from .repository import account_lock
from zhs_client import PlatformError


class AnswerService:
    def __init__(self, provider, repository):
        self.provider = provider
        self.repository = repository

    def refresh(self, scope=None, on_progress=None):
        scope = scope or {}
        update = on_progress or (lambda progress: None)
        report = {'ok': True, 'partial': False, 'processed': 0,
                  'counts': {state: 0 for state in LABELS}, 'errors': [],
                  'courses': [], 'coveredCourses': 0}
        update({'stage': 'answers', 'step': '正在发现课程', 'current': 0, 'total': 0})
        with account_lock(self.repository.account):
            courses = self.provider.courses(self.repository.courses())
            cid = str(scope.get('courseId') or '')
            if cid and not any(str(c['courseId']) == cid for c in courses):
                courses.append({'courseId': cid, 'courseName': cid})
            self.repository.save_courses(courses)
            if cid:
                courses = [c for c in courses if str(c['courseId']) == cid]
            report['courses'] = courses
            report['coveredCourses'] = len(courses)
            pending = []
            for course in courses:
                try:
                    rows = self.provider.homework(course)
                    hid = str(scope.get('homeworkId') or '')
                    pending += [(course, row) for row in rows if not hid or str(row['homeworkId']) == hid]
                    if hid and not any(str(row['homeworkId']) == hid for row in rows):
                        report['errors'].append({'courseId': course['courseId'], 'message': '指定作业不在该课程列表中'})
                except PlatformError as error:
                    if error.kind == 'need_login':
                        raise
                    report['errors'].append({'courseId': course['courseId'], 'message': str(error)})
            total = len(pending)
            for course, homework in pending:
                entry = self.provider.fetch(course, homework)
                self.repository.put(entry)
                report['processed'] += 1
                report['counts'][entry['state']] += 1
                update({'stage': 'answers', 'step': '正在获取答案',
                        'current': report['processed'], 'total': total,
                        'counts': dict(report['counts'])})
                if entry['state'] == 'need_login':
                    report['errors'].append({'courseId': entry['courseId'],
                                             'homeworkId': entry['homeworkId'], 'message': entry['message']})
                    break
                if entry['state'] == 'error':
                    report['errors'].append({'courseId': entry['courseId'],
                                             'homeworkId': entry['homeworkId'], 'message': entry['message']})
            report['partial'] = bool(report['errors'])
            report['ok'] = not report['partial']
        return report
