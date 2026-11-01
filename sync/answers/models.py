from datetime import datetime

LABELS = {
    'unknown': '未检查', 'available': '答案完整', 'partial': '答案部分可得',
    'closed': '未公布', 'no_paper': '无试卷', 'need_login': '需要登录',
    'error': '获取失败',
}


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def record(course, homework, account):
    cid, hid = str(course['courseId']), str(homework['homeworkId'])
    return {'schemaVersion': 1, 'provider': 'zhs', 'accountKey': account,
            'key': 'zhs:%s:%s' % (cid, hid), 'courseId': cid,
            'homeworkId': hid, 'courseName': course.get('courseName') or cid,
            'title': homework.get('title') or hid,
            'paperId': homework.get('paperId'), 'isComplete': homework.get('isComplete'),
            'due': homework.get('due') or '', 'state': 'unknown',
            'checkedAt': now(), 'questions': [], 'questionCount': 0,
            'answeredCount': 0, 'message': '',
            'sourceUrl': 'https://hiexam.zhihuishu.com/'}
