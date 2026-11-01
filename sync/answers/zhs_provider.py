"""智慧树数据适配；平台字段和接口仅在此层出现。"""
from datetime import datetime
from .models import record
from .normalize import availability, discover_courses, normalize_questions
from zhs_client import LIST_URL, PlatformError


class ZhsHomeworkProvider:
    def __init__(self, client, captured=None, details=None):
        self.client = client
        self.captured = captured
        self.details = details or {}

    def courses(self, known):
        merged = {str(c['courseId']): c for c in known}
        for course in discover_courses(self.captured):
            merged[course['courseId']] = course
        reminders = self.client.get(LIST_URL).get('rt')
        if not isinstance(reminders, list):
            raise PlatformError('parse', '课程提醒列表结构异常')
        for course in discover_courses(reminders):
            merged[course['courseId']] = course
        return list(merged.values())

    def homework(self, course):
        result, seen = [], set()
        page = 1
        while True:
            payload = self.client.get('/homeworkUser/List', {
                'courseId': course['courseId'], 'key': '',
                'pageIndex': page, 'pageSize': 50, 'completeTab': 0})
            rt = payload.get('rt')
            if not isinstance(rt, dict) or not isinstance(rt.get('list'), list):
                raise PlatformError('parse', '作业列表结构异常')
            rows = rt['list']
            before = len(result)
            for raw in rows:
                hid = raw.get('homeworkId') or raw.get('id')
                if hid is None:
                    raise PlatformError('parse', '作业缺少平台ID')
                hid = str(hid)
                if hid in seen:
                    continue
                seen.add(hid)
                row = dict(raw, homeworkId=hid)
                try:
                    row['due'] = datetime.fromtimestamp(int(raw.get('endTime') or 0)/1000).astimezone().isoformat(timespec='minutes') if raw.get('endTime') else ''
                except (ValueError, TypeError, OverflowError, OSError):
                    row['due'] = ''
                result.append(row)
            total = rt.get('rowCount')
            try:
                total = int(total) if total is not None else None
            except (ValueError, TypeError):
                raise PlatformError('parse', '作业总数格式异常')
            if total is not None and len(result) >= total:
                break
            if not rows or (total is None and len(rows) < 50):
                if total is not None and len(result) < total:
                    raise PlatformError('parse', '分页提前结束，本次未取得完整作业列表')
                break
            if len(result) == before:
                raise PlatformError('parse', '平台重复返回同一页作业')
            page += 1
            if page > 1000:
                raise PlatformError('parse', '作业分页数量异常')
        return result

    def fetch(self, course, homework):
        entry = record(course, homework, self.client.account_key)
        params = {'homeworkId': entry['homeworkId'], 'uuid': self.client.uuid}
        seed = self.details.get(entry['homeworkId']) or {}
        if seed.get('files'):
            entry['attachments'] = seed['files']
        try:
            if entry['paperId'] is None:
                rt = self.client.get('/homeworkUserDir/Info', dict(
                    params, courseId=entry['courseId'])).get('rt')
                if not isinstance(rt, dict) or rt.get('paperId') is None:
                    raise PlatformError('parse', '无法确认作业是否有试卷')
                entry['paperId'] = rt['paperId']
            if str(entry['paperId']) == '0':
                entry.update(state='no_paper', message='目录型作业，请查看任务要求和附件')
            else:
                response = self.client.paper(entry['homeworkId'])
                entry['questions'] = normalize_questions(response.get('rt'))
                state, count = availability(entry['questions'])
                entry.update(state=state, questionCount=len(entry['questions']), answeredCount=count)
                if state == 'partial':
                    entry['message'] = '部分题目缺少正确标记或不属于选择、判断题'
        except ValueError as error:
            entry.update(state='error', errorKind='parse', message=str(error))
        except PlatformError as error:
            state = error.kind if error.kind in ('closed', 'need_login') else 'error'
            entry.update(state=state, errorKind=error.kind, message=str(error))
            if state == 'closed':
                # 成绩卡的对错只展示为成绩，绝不交给选项解析器。
                try:
                    rt = self.client.get('/homeworkUserPaper/getAnswerCard', params).get('rt') or {}
                    entry['gradeCard'] = [
                        {k: q.get(k) for k in ('no', 'isCorrect', 'isDone', 'questionScore')}
                        for g in rt.get('groupDtoList') or [] for q in g.get('questionDtos') or []]
                except PlatformError:
                    pass
        if not entry.get('attachments'):
            try:
                from zhs_detail import _norm_files
                rt = self.client.get('/homeworkDir/homeworkDirGet2', params).get('rt') or {}
                entry['attachments'] = _norm_files(rt.get('fileList'))
            except PlatformError:
                entry['attachments'] = []
        return entry
