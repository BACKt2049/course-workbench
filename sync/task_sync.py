"""Task identity and non-destructive reconciliation for the JSON storage format.

Platform observations and explicit local decisions are separate. This module is
pure so migration and sync can share the same rules without opening user files.
"""
from copy import deepcopy
import hashlib
import json

LOCAL_FIELDS = ('note', 'minutes', 'courseId', 'localUpdatedAt')
STATES = {'unknown', 'not_submitted', 'submitted', 'graded', 'returned', 'withdrawn'}


def reference(task):
    ref = task.get('sourceRef') or {}
    return tuple(str(value or '') for value in (
        ref.get('provider') or task.get('src'), ref.get('accountKey'),
        ref.get('courseId'), ref.get('kind') or ('exam' if task.get('type') == '考试' else 'homework'),
        ref.get('homeworkId') or ref.get('externalId')))


def source_key(task):
    key = reference(task)
    return key if key[0] and key[2] and key[4] else None


def stable_id(ref):
    raw = json.dumps(reference({'sourceRef': ref}), ensure_ascii=False)
    return 'zhs_' + hashlib.sha256(raw.encode()).hexdigest()[:20]


def compatible(a, b):
    x, y = reference(a), reference(b)
    if (not x[2] or not y[2]) and a.get('courseName') and b.get('courseName') and a['courseName'] != b['courseName']:
        return False
    return all(not x[i] or not y[i] or x[i] == y[i] for i in (0, 1, 2, 3, 4))


def aliases(task):
    return {str(x) for x in [task.get('id'), *(task.get('legacyIds') or [])] if x}


def find_match(tasks, task):
    # Check scope even when an old title-derived ID collides across accounts.
    candidates = [i for i, old in enumerate(tasks) if compatible(old, task)]
    key = source_key(task)
    exact = [i for i in candidates if key and source_key(tasks[i]) == key]
    if len(exact) == 1:
        return exact[0]
    by_id = [i for i in candidates if aliases(tasks[i]) & aliases(task)]
    if len(by_id) == 1:
        return by_id[0]
    # Legacy data has no platform ID: only bind a unique course/title match.
    legacy = [i for i in candidates
              if (not source_key(tasks[i]) or not key)
              and task.get('title') == tasks[i].get('title')
              and task.get('courseName') and task.get('courseName') == tasks[i].get('courseName')]
    return legacy[0] if len(legacy) == 1 else None


def normalize(task):
    value = deepcopy(task)
    value.setdefault('legacyIds', [])
    value.setdefault('attachments', [])
    if 'workStatusExplicit' not in value:
        value['workStatusExplicit'] = bool(value.get('done'))
        value['workStatusOrigin'] = 'legacy' if value.get('done') else 'automatic'
    if value.get('workStatus') not in ('todo', 'in_progress', 'done'):
        value['workStatus'] = 'done' if value.get('done') else 'todo'
    if value.get('submissionStatus') not in STATES:
        value['submissionStatus'] = 'unknown'
    if not value['workStatusExplicit'] and value['submissionStatus'] in ('submitted', 'graded'):
        value['workStatus'] = 'done'
    value['done'] = value['workStatus'] == 'done'
    return value


def merge_one(old, incoming, local=False):
    old, incoming = normalize(old), normalize(incoming)
    result = deepcopy(old)
    result.update(incoming)
    result['id'] = old['id']
    result['legacyIds'] = sorted((aliases(old) | aliases(incoming)) - {str(old['id'])})
    result['sourceRef'] = dict(old.get('sourceRef') or {})
    result['sourceRef'].update({k: v for k, v in (incoming.get('sourceRef') or {}).items() if v})
    if not local:
        for field in LOCAL_FIELDS:
            if field in old:
                result[field] = old[field]
        # An empty reminder does not erase details or manually added attachments.
        for field in ('content', 'detailAt', 'paperId', 'enrich'):
            if field not in incoming and field in old:
                result[field] = old[field]
        if not incoming.get('attachments'):
            result['attachments'] = old.get('attachments') or []
        else:
            result['attachments'] = incoming['attachments'] + [
                a for a in old.get('attachments') or [] if a.get('manual') and a not in incoming['attachments']]
    else:
        # Workspace snapshots may be older than a completed background pull.
        for field in ('submissionStatus', 'submissionObservedAt', 'submissionEvidence',
                      'title', 'due', 'courseName', 'detailAt', 'paperId'):
            if field in old:
                result[field] = old[field]
        if not incoming.get('localUpdatedAt'):
            for field in ('content', 'attachments'):
                if field in old:
                    result[field] = old[field]
    decision = old
    if incoming['workStatusExplicit'] and (
            not old['workStatusExplicit'] or
            int(incoming.get('workStatusUpdatedAt') or 0) > int(old.get('workStatusUpdatedAt') or 0)):
        decision = incoming
    for field in ('workStatus', 'workStatusExplicit', 'workStatusOrigin', 'workStatusUpdatedAt'):
        if field in decision:
            result[field] = decision[field]
    return normalize(result)


def reconcile(existing, incoming, local=False, deleted=None):
    tasks = [normalize(x) for x in existing]
    stats = dict(added=0, updated=0, unchanged=0, conflicts=0, failed=0)
    for raw in incoming:
        item = normalize(raw)
        if not item.get('id'):
            stats['failed'] += 1
            continue
        match = find_match(tasks, item)
        if match is None:
            if any(x.get('id') == item['id'] for x in tasks):
                # Split a legacy collision without merging unrelated accounts.
                item['id'] = stable_id(item.get('sourceRef') or {})
                stats['conflicts'] += 1
            tasks.append(item)
            stats['added'] += 1
        else:
            merged = merge_one(tasks[match], item, local=local)
            changed = merged != tasks[match]
            tasks[match] = merged
            stats['updated' if changed else 'unchanged'] += 1
    tombstones = list(deleted or [])
    tasks = [x for x in tasks if not any(compatible(x, d) and aliases(x) & aliases(d) for d in tombstones)]
    if any(x.get('src') in ('tju', 'zhs') and not str(x.get('id', '')).startswith('demo_') for x in tasks):
        tasks = [x for x in tasks if not str(x.get('id', '')).startswith('demo_')]
    return tasks, stats


def submission(raw):
    """Only consume explicit, unambiguous evidence; numeric status is not guessed."""
    labels = {'已提交': 'submitted', '已完成': 'submitted', '已批改': 'graded',
              '已评分': 'graded', '未提交': 'not_submitted', '未交': 'not_submitted',
              '已退回': 'returned', '已撤回': 'withdrawn'}
    for field in ('statusName', 'statusText', 'submitStatusName'):
        if raw.get(field) in labels:
            return {'submissionStatus': labels[raw[field]], 'submissionEvidence': field}
    if raw.get('submitDate') or raw.get('submitTime'):
        return {'submissionStatus': 'submitted', 'submissionEvidence': 'submitDate/submitTime'}
    for field in ('isSubmitted', 'submitted'):
        if isinstance(raw.get(field), bool):
            return {'submissionStatus': 'submitted' if raw[field] else 'not_submitted',
                    'submissionEvidence': field}
    return {'submissionStatus': 'unknown'}
