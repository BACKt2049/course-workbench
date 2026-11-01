"""HTTP接口适配：鉴权由工作台服务统一执行。"""
import re
import urllib.parse
from .exporters import brief, export_records, filename
from .models import LABELS
from .jobs import JOBS, AnswerJobs, repository, scope_of
from zhs_client import PlatformError

def dispatch(handler, method, path, query):
    def first(name):
        return query.get(name, [''])[0]
    try:
        if method == 'POST' and path == '/api/answers/refresh':
            return handler._json(JOBS.start(handler._body()))
        repo = repository()
        if method == 'GET' and path == '/api/answers':
            state = first('state')
            if state and state not in LABELS:
                raise ValueError('答案状态无效')
            all_records = repo.listing()
            counts = {s: sum(r['state'] == s for r in all_records) for s in LABELS}
            entries = repo.listing(first('courseId'), first('q'), state)
            return handler._json({'ok': True, 'accountKey': repo.account,
                                  'records': [brief(r) for r in entries], 'courses': repo.courses(),
                                  'counts': counts, 'total': len(all_records),
                                  'coverage': '已发现课程的全部作业（含已完成）；未发现课程不计入覆盖范围'})
        match = re.fullmatch(r'/api/answers/jobs/([0-9a-f]+)', path)
        if method == 'GET' and match:
            job = JOBS.lookup(match[1], repo.account)
            return handler._json(job) if job else handler._err(404, '任务不存在或属于其他账号')
        match = re.fullmatch(r'/api/answers/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)', path)
        if method == 'GET' and match:
            entry = repo.get(match[1], match[2])
            return handler._json({'ok': True, 'record': entry}) if entry else handler._err(404, '尚未缓存这份作业，请先同步')
        if method == 'POST' and path == '/api/answers/export':
            body = handler._body()
            format_name = body.get('format') or 'txt'
            records = repo.listing(body.get('courseId'), body.get('q') or '', body.get('state'))
            if 'keys' in body:
                if not isinstance(body['keys'], list):
                    raise ValueError('导出选择格式错误')
                records = [r for r in records if r['key'] in body['keys']]
            if not records:
                raise ValueError('没有可导出的作业')
            content, mime = export_records(records, format_name)
            name = urllib.parse.quote(filename(records, format_name))
            return handler._send(200, content, mime, {'Content-Disposition': "attachment; filename*=UTF-8''" + name})
        return handler._err(404, '没有这个答案操作')
    except PlatformError as error:
        if method == 'GET' and path == '/api/answers':
            return handler._json({'ok': True, 'needLogin': True, 'records': [], 'courses': [], 'counts': {}, 'total': 0})
        return handler._err(401 if error.kind == 'need_login' else 502, str(error), needLogin=error.kind == 'need_login')
    except ValueError as error:
        return handler._err(400, str(error))
    except OSError:
        return handler._err(500, '无法读写答案缓存，请检查数据目录权限')
