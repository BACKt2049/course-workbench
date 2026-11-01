"""按账号隔离的本地仓库，不依赖HTTP或平台请求。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import threading
import uuid
import durable_json

_locks = {}
_lock_guard = threading.Lock()


def account_lock(key):
    with _lock_guard:
        return _locks.setdefault(key, threading.RLock())


class AnswerRepository:
    def __init__(self, base, account):
        if not re.fullmatch(r'[a-f0-9]{24}', account or ''):
            raise ValueError('无效账号标识')
        self.root = Path(base) / account
        self.account = account
        self.lock = account_lock(account)

    def _path(self, course, homework):
        for value in (course, homework):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(value)):
                raise ValueError('课程或作业ID无效')
        return self.root / str(course) / (str(homework) + '.json')

    @staticmethod
    def _read(path, default=None):
        return durable_json.read(path,default)

    @staticmethod
    def _write(path, value):
        durable_json.write(path,value)

    def courses(self):
        return self._read(self.root / 'courses.json', [])

    def save_courses(self, courses):
        with self.lock:
            merged = {str(c['courseId']): c for c in self.courses()}
            merged.update({str(c['courseId']): c for c in courses})
            value = sorted(merged.values(), key=lambda c: c.get('courseName', ''))
            self._write(self.root / 'courses.json', value)
            return value

    def put(self, entry):
        if entry.get('accountKey') != self.account:
            raise ValueError('答案账号与仓库不一致')
        path = self._path(entry['courseId'], entry['homeworkId'])
        with self.lock:
            old = self._read(path, {})
            value = deepcopy(entry)
            previous = old.get('lastSuccess')
            if value['state'] == 'available' or (
                value.get('answeredCount') and not previous):
                previous = {k: deepcopy(value.get(k)) for k in (
                    'questions', 'questionCount', 'answeredCount', 'checkedAt', 'state')}
            if previous:
                value['lastSuccess'] = previous
            self._write(path, value)
        return self.get(entry['courseId'], entry['homeworkId'])

    def get(self, course, homework):
        value = self._read(self._path(course, homework))
        if value is None:
            return None
        history = value.pop('lastSuccess', None)
        value['historical'] = False
        value['answersAt'] = value.get('checkedAt')
        if history and not value.get('answeredCount'):
            value.update(questions=history['questions'], historical=True,
                         answersAt=history['checkedAt'], historicalCount=history['answeredCount'])
        return value

    def listing(self, course=None, query='', state=None):
        result = []
        for path in self.root.glob('*/*.json'):
            if course and path.parent.name != str(course):
                continue
            entry = self.get(path.parent.name, path.stem)
            if not entry or (state and entry.get('state') != state):
                continue
            if query.casefold() not in (entry.get('title', '') + ' ' + entry.get('courseName', '')).casefold():
                continue
            result.append(entry)
        return sorted(result, key=lambda r: (r.get('courseName', ''), r.get('due', ''), r['homeworkId']))
