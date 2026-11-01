# -*- coding: utf-8 -*-
"""智慧树作业 → ddl[]。

智慧树是重前端 SPA，作业数据多半以内嵌 JSON 下发而不是渲染好的表格，
所以先扒 script 里的 JSON，再退回 DOM 文本。

没有真实样本时这一层必然要校准一次：先把条目写进去并带 note，
宁可让人一眼看出「这是猜的」，也不要静默丢数据。
"""

import hashlib
import json
import re

from . import _html as H

KEYS_TITLE = ("title", "name", "homeworkName", "examName", "workName", "courseName")
KEYS_DUE = ("endTime", "deadline", "endDate", "expireTime", "end_time", "deadLine",
            "submitEndTime", "closeTime")
KEYS_COURSE = ("courseName", "course_name", "subjectName", "parentName")


def _stable_id(*parts):
    raw = "|".join([str(p or "") for p in parts]).encode("utf-8")
    return "zhs_" + hashlib.md5(raw).hexdigest()[:10]


def _scripts(html):
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html or "", re.S | re.I):
        body = m.group(1)
        if len(body) < 120:
            continue
        if not re.search(r"作业|homework|exam|work", body, re.I):
            continue
        yield body


def _try_json(body):
    for pat in (r"=\s*(\{.*\})\s*;?\s*$", r"JSON\.parse\(\s*'(\{.*\})'\s*\)", r"=\s*(\[.*\])\s*;?\s*$"):
        m = re.search(pat, body, re.S)
        if not m:
            continue
        raw = m.group(1)
        for candidate in (raw, raw.replace("\\'", "'").replace('\\"', '"')):
            try:
                return json.loads(candidate)
            except Exception:
                continue
    return None


def _walk(node, hits, depth=0):
    if depth > 8:
        return
    if isinstance(node, dict):
        title = next((node[k] for k in KEYS_TITLE if isinstance(node.get(k), str) and len(node[k]) > 1), None)
        due = next((node[k] for k in KEYS_DUE if node.get(k)), None)
        if title and due:
            hits.append(node)
        for v in node.values():
            _walk(v, hits, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, hits, depth + 1)


def _norm_due(v):
    s = str(v or "").strip()
    if re.fullmatch(r"\d{10,13}", s):
        import time
        n = int(s)
        if n > 1e12:
            n //= 1000
        return time.strftime("%Y-%m-%dT%H:%M", time.localtime(n))
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[T\s](\d{1,2}):(\d{2})", s)
    if m:
        return "%s-%02d-%02dT%02d:%s" % (m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5))
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return "%s-%02d-%02dT23:59" % (m.group(1), int(m.group(2)), int(m.group(3)))
    return ""


def _from_json(html):
    hits = []
    for body in _scripts(html):
        obj = _try_json(body)
        if obj is None:
            m = re.search(r"(\{.*\}|\[.*\])", body, re.S)
            if not m:
                continue
            try:
                obj = json.loads(m.group(1))
            except Exception:
                continue
        _walk(obj, hits)
    out = []
    for node in hits:
        title = next((node[k] for k in KEYS_TITLE if isinstance(node.get(k), str) and len(node[k]) > 1), "")
        due = _norm_due(next((node[k] for k in KEYS_DUE if node.get(k)), ""))
        course = next((node[k] for k in KEYS_COURSE if isinstance(node.get(k), str) and node.get(k) != title), "")
        if not title or not due:
            continue
        out.append({
            "id": _stable_id(title, due),
            "title": title,
            "courseName": course,
            "type": "作业",
            "due": due,
            "minutes": 90,
            "done": False,
            "note": "来自智慧树页面数据，提交要求请以站内为准",
        })
    return out


_DOM_ITEM = re.compile(
    r"<li[^>]*>.*?(?:作业|考试).*?</li>|<div[^>]*class=\"[^\"]*(?:homework|work|task)[^\"]*\"[^>]*>.*?</div>",
    re.S | re.I)


def _from_dom(html):
    out = []
    for m in _DOM_ITEM.finditer(html or ""):
        text = H.strip_tags(m.group(0))
        if not text or len(text) < 6:
            continue
        dm = re.search(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}[^\u4e00-\u9fa5]{0,3}\d{0,2}:?\d{0,2})", text)
        due = _norm_due(dm.group(1)) if dm else ""
        if not due:
            continue
        title = text.split(dm.group(1))[0].strip()[:60] or text[:40]
        out.append({
            "id": _stable_id(title, due),
            "title": title,
            "courseName": "",
            "type": "作业",
            "due": due,
            "minutes": 90,
            "done": False,
            "note": "从页面文本推断，请核对",
        })
    return out


API_BLOCK = re.compile(r"<script>window\.__API_\d+__ = ([\s\S]*?);</script>")


def parse_api(blob):
    """从页面捕获的 JSON 接口里挖作业/考试。

    实测结构（学生首页「即将截止」接口）：
        {"rt":[{"type":1,"title":"作业答案汇总","courseName":"形式化方法-02741",
                "endTime":1778068200000,"status":3,…}]}
    type 1 = 作业，2 = 考试；endTime 是毫秒时间戳。
    这是最可靠的一条路 —— 比解析渲染后的 DOM 稳定得多。
    """
    items, seen = [], set()
    import time as _t
    cutoff = _t.time() - 7 * 86400      # 半年前的历史作业不要，刚过期的留着提醒
    for m in API_BLOCK.finditer(blob or ""):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        rt = obj.get("rt") if isinstance(obj, dict) else None
        if not isinstance(rt, list):
            continue
        for x in rt:
            if not isinstance(x, dict):
                continue
            title = (x.get("title") or "").strip()
            end = x.get("endTime") or x.get("deadline")
            if not title or not end:
                continue
            due = _norm_due(end)
            if not due:
                continue
            try:
                from datetime import datetime as _dt
                if _dt.strptime(due, "%Y-%m-%dT%H:%M").timestamp() < cutoff:
                    continue
            except Exception:
                pass
            course = (x.get("courseName") or "").strip()
            kind = "考试" if str(x.get("type")) == "2" else "作业"
            key = (title, due)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "id": _stable_id(title, due),
                "sourceRef": {"provider": "zhs", "kind": "exam" if kind == "考试" else "homework",
                              "courseId": str(x.get("courseId") or ""),
                              "homeworkId": str(x.get("homeworkId") or x.get("id") or "")},
                "title": title,
                "courseName": course,
                "type": kind,
                "due": due,
                "minutes": 90,
                "done": False,
                "note": "",
            })
    return {"items": items, "unparsed": []}


def parse(html):
    items = _from_json(html)
    if not items:
        items = _from_dom(html)
    unparsed = []
    if not items:
        unparsed.append({
            "kind": "ddl",
            "reason": "没从页面里认出作业条目（智慧树前端结构需要按真实页面校准）",
            "sample": H.strip_tags((html or "")[:800]),
        })
    return {"items": items, "unparsed": unparsed}
