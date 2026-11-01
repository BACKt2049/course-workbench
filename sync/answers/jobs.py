"""后台答案任务协调；HTTP、命令行和同步流水线共用。"""
from copy import deepcopy
import json
import re
import secrets
import threading
import time
from .repository import AnswerRepository
from .service import AnswerService
from .zhs_provider import ZhsHomeworkProvider
from zhs_client import ZhsClient, PlatformError, account_key, saved_state


def repository(account=None):
    import store
    key = account or account_key(saved_state())
    if not key:
        raise PlatformError('need_login', '请先连接智慧树账号')
    return AnswerRepository(store.ROOT / 'answers', key)


def scope_of(body):
    scope = {}
    for field in ('courseId', 'homeworkId'):
        if body.get(field) is not None and str(body[field]):
            value = str(body[field])
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
                raise ValueError('课程或作业ID无效')
            scope[field] = value
    if scope.get('homeworkId') and not scope.get('courseId'):
        raise ValueError('刷新单份作业时必须指定课程')
    return scope


class AnswerJobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = {}

    def _allocate(self, account, scope):
        key = account + ':' + json.dumps(scope, sort_keys=True)
        with self.lock:
            jid = self.active.get(key)
            if jid and self.jobs[jid]['state'] == 'running':
                return self.jobs[jid], False
            jid = secrets.token_hex(8)
            job = {'id': jid, 'account': account, 'scopeKey': key,
                   'state': 'running', 'step': '等待答案同步', 'stage': 'answers',
                   'current': 0, 'total': 0, 'counts': {}, 'result': None,
                   'error': '', 'at': time.time(), 'done': threading.Event()}
            self.jobs[jid] = job
            self.active[key] = jid
            finished = sorted((j for j in self.jobs.values() if j['state'] != 'running'), key=lambda j: j['at'])
            for old in finished[:-20]:
                self.jobs.pop(old['id'], None)
            return job, True

    def public(self, job):
        with self.lock:
            return deepcopy({k: v for k, v in job.items() if k not in ('account', 'scopeKey', 'done', 'at')})

    def lookup(self, jid, account):
        with self.lock:
            job = self.jobs.get(jid)
            return self.public(job) if job and job['account'] == account else None

    def _execute(self, job, scope, client=None, captured=None, details=None, progress=None):
        def update(value):
            with self.lock:
                job.update(value)
            if progress:
                progress(value)
        def refresh(active_client):
            service = AnswerService(ZhsHomeworkProvider(active_client, captured, details),
                                    repository(job['account']))
            return service.refresh(scope, update)
        try:
            if client is not None:
                result = refresh(client)
            else:
                # 在实际执行任务的线程里创建与释放Playwright对象。
                with ZhsClient() as own_client:
                    if own_client.account_key != job['account']:
                        raise PlatformError('need_login', '账号已切换，请重新刷新')
                    result = refresh(own_client)
            with self.lock:
                job.update(result=result, state='partial' if result['partial'] else 'done', step='答案同步完成')
        except Exception as error:
            need_login = isinstance(error, PlatformError) and error.kind == 'need_login'
            message = str(error) if isinstance(error, (PlatformError, ValueError)) else '答案处理失败，请稍后重试'
            with self.lock:
                job.update(state='error', error=message, step='答案同步未完成',
                           result={'ok': False, 'partial': True, 'needLogin': need_login,
                                   'errors': [{'message': message}], 'processed': job['current'],
                                   'counts': job['counts']})
        finally:
            with self.lock:
                if self.active.get(job['scopeKey']) == job['id']:
                    self.active.pop(job['scopeKey'], None)
                job['done'].set()

    def start(self, scope=None):
        scope = scope_of(scope or {})
        repo = repository()
        job, created = self._allocate(repo.account, scope)
        if created:
            threading.Thread(target=self._execute, args=(job, scope), daemon=True).start()
        return {'ok': True, 'jobId': job['id'], 'reused': not created, 'state': job['state']}

    def inline(self, client, captured=None, details=None, progress=None, scope=None):
        scope = scope_of(scope or {})
        job, created = self._allocate(client.account_key, scope)
        if created:
            self._execute(job, scope, client, captured, details, progress)
        else:
            while not job['done'].wait(0.5):
                if progress:
                    value = self.public(job)
                    progress({k: value[k] for k in ('step', 'stage', 'current', 'total', 'counts')})
        return self.public(job)['result']


JOBS = AnswerJobs()
