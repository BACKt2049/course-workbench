"""答案投影与导出，不负责抓取或存储。"""
import json
import re
from .models import LABELS, now


def brief(entry):
    return {k: v for k, v in entry.items() if k not in ('questions', 'gradeCard')}


def text_answer(question):
    return '、'.join(question.get('answer') or []) or '未知'


def export_records(records, format_name='txt'):
    if format_name == 'json':
        return json.dumps({'schemaVersion': 1, 'exportedAt': now(), 'records': records},
                          ensure_ascii=False, indent=2).encode('utf-8'), 'application/json; charset=utf-8'
    if format_name not in ('txt', 'md'):
        raise ValueError('仅支持txt、md和json格式')
    lines = []
    for entry in records:
        title = entry.get('title') or entry['homeworkId']
        lines += [('## ' if format_name == 'md' else '') + title,
                  '课程：' + entry.get('courseName', ''),
                  '状态：' + LABELS.get(entry['state'], entry['state']),
                  '最近检查：' + (entry.get('checkedAt') or '未检查'),
                  '答案获取：' + (entry.get('answersAt') or '无'),
                  '来源：智慧树平台返回的选项标记']
        if entry.get('historical'):
            lines += ['注意：以下为本地历史缓存，不代表本次仍可获取。']
        questions = entry.get('questions') or []
        if not questions:
            lines += [entry.get('message') or '尚未取得答案']
        for position, question in enumerate(questions, 1):
            lines.append('%s. 原题号%s：%s' % (position, question.get('no'), text_answer(question)))
            if format_name == 'md':
                lines += ['   ' + (question.get('content') or '平台未提供题干，请参见附件')]
                for option in question.get('options') or []:
                    mark = ' ←正确' if option.get('correct') is True else ''
                    lines.append('   %s. %s%s' % (option['label'], option.get('text') or '（无选项文本）', mark))
        lines += ['']
    return ('\n'.join(lines).rstrip() + '\n').encode('utf-8'), 'text/plain; charset=utf-8' if format_name == 'txt' else 'text/markdown; charset=utf-8'


def filename(records, format_name):
    name = ('%s_%s' % (records[0]['homeworkId'], records[0]['title'])) if len(records) == 1 else '全部答案'
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', name).strip(' .')[:100] or '答案'
    return name + '.' + format_name
