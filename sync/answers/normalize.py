"""平台题目到领域记录的纯转换；不联网、不读磁盘。"""
import html
import re


def flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ('true', '1'):
            return True
        if value in ('false', '0'):
            return False
    return None


def plain(value):
    value = str(value or '')
    value = re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', value,
                   flags=re.I | re.S)
    value = re.sub(r'<br\s*/?>|</(?:p|div|li)>', '\n', value, flags=re.I)
    return html.unescape(re.sub(r'<[^>]*>', '', value)).strip()


def normalize_questions(rt):
    if not isinstance(rt, dict) or not isinstance(rt.get('groupDtoList'), list):
        raise ValueError('题目分组结构缺失')
    result = []
    for gi, group in enumerate(rt['groupDtoList']):
        if not isinstance(group, dict):
            raise ValueError('题目分组结构异常')
        questions = group.get('questionDtoList')
        if not isinstance(questions, list):
            raise ValueError('题目列表缺失；成绩卡不能用作选项答案')
        for qi, q in enumerate(questions):
            if not isinstance(q, dict):
                raise ValueError('题目结构异常')
            options = []
            for oi, option in enumerate(q.get('questionOptionList') or []):
                if not isinstance(option, dict):
                    raise ValueError('选项结构异常')
                label = str(option.get('label') or option.get('optionLabel') or '')
                if not re.fullmatch('[A-Z]', label):
                    label = chr(65 + oi) if oi < 26 else str(oi + 1)
                options.append({'id': str(option.get('questionOptionId') or option.get('id') or oi),
                                'label': label, 'position': oi,
                                'text': plain(option.get('content')),
                                'correct': flag(option.get('isCorrect'))})
            kind = str(q.get('questionName') or '')
            supported = bool(options) and not re.search('填空|简答|论述|主观|编程', kind)
            complete = supported and all(o['correct'] is not None for o in options) and any(
                o['correct'] is True for o in options)
            result.append({'id': str(q.get('questionId') or q.get('id') or '%s-%s' % (gi, qi)),
                           'groupId': str(group.get('groupId') or group.get('id') or gi),
                           'group': plain(group.get('groupName') or group.get('title')),
                           'no': q.get('no') if q.get('no') is not None else qi + 1,
                           'position': len(result), 'type': kind,
                           'content': plain(q.get('content')), 'score': q.get('questionScore'),
                           'options': options, 'state': 'available' if complete else (
                               'unknown' if supported else 'unsupported'),
                           'answer': [o['label'] for o in options if o['correct'] is True] if complete else []})
    if not result:
        raise ValueError('试卷返回空题目，不能认定答案完整')
    return result


def availability(questions):
    answered = sum(q['state'] == 'available' for q in questions)
    return ('available' if answered == len(questions) and answered else 'partial'), answered


def discover_courses(blob):
    """读取已捕获的课程卡片与提醒JSON，不把课程名当平台ID。"""
    import json
    found = {}
    def walk(node):
        if isinstance(node, dict):
            if node.get('courseId') and node.get('courseName'):
                cid = str(node['courseId'])
                found[cid] = {'courseId': cid, 'courseName': plain(node['courseName'])}
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    if isinstance(blob, (dict, list)):
        walk(blob)
    else:
        for match in re.finditer(r'<script>window\.__API_\d+__ = ([\s\S]*?);</script>', blob or ''):
            try:
                walk(json.loads(match[1]))
            except ValueError:
                continue
    return list(found.values())
