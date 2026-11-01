# -*- coding: utf-8 -*-
"""智慧树学习进度 → progress[]。

进度通常长这样：{'courseName': 'xxx', 'progress': 62, 'studyHour': 3.5}。
同样是先扒 JSON 再退 DOM，认不出来就进 unparsed。
"""

import hashlib
import json
import re

from . import _html as H

KEYS_NAME = ("courseName", "course_name", "name", "subjectName")
KEYS_PCT = ("progress", "percent", "schedule", "rate", "studyProgress", "videoProgress")
KEYS_WATCH = ("watchTime", "studyTime", "studyHour", "watched", "videoTime", "totalTime")


def _stable_id(*parts):
    raw = "|".join([str(p or "") for p in parts]).encode("utf-8")
    return "zhs_p_" + hashlib.md5(raw).hexdigest()[:10]


def _walk(node, hits, depth=0):
    if depth > 8:
        return
    if isinstance(node, dict):
        name = next((node[k] for k in KEYS_NAME if isinstance(node.get(k), str) and len(node[k]) > 1), None)
        pct = next((node[k] for k in KEYS_PCT if node.get(k) is not None), None)
        if name and pct is not None:
            hits.append(node)
        for v in node.values():
            _walk(v, hits, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, hits, depth + 1)


def _num(v):
    try:
        f = float(str(v).replace("%", "").strip())
        return round(f, 1)
    except Exception:
        return None


def _from_json(html):
    hits = []
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html or "", re.S | re.I):
        body = m.group(1)
        if len(body) < 120 or not re.search(r"progress|课程|course", body, re.I):
            continue
        cand = None
        mm = re.search(r"(\{.*\}|\[.*\])", body, re.S)
        if mm:
            try:
                cand = json.loads(mm.group(1))
            except Exception:
                cand = None
        if cand is not None:
            _walk(cand, hits)
    out = []
    for node in hits:
        name = next((node[k] for k in KEYS_NAME if isinstance(node.get(k), str) and len(node[k]) > 1), "")
        pct = _num(next((node[k] for k in KEYS_PCT if node.get(k) is not None), None))
        if not name or pct is None:
            continue
        watch = next((node[k] for k in KEYS_WATCH if node.get(k) is not None), None)
        out.append({
            "id": _stable_id(name),
            "courseName": name,
            "percent": max(0, min(100, int(pct))),
            "watched": _num(watch) if watch is not None else None,
            "total": None,
            "updatedAt": "",
            "note": "",
        })
    return out


FIXED_WORDS = {"翻转课", "共享课", "AI智课", "智慧课程", "查看全部", "加入课程",
               "群文件管理", "进行中", "已完成", "选课", "课程退课", "加载更多中"}


def _from_dom(html):
    """退路：在渲染后的文本里找「课程名 … 进度:27.1%」。

    智慧树学生端卡片文本化之后是这样：
        翻转课 查看全部 加入课程 群文件管理 翻转课 架构驱动的软件开发实践 于智郅 进度: 0%
    所以按「进度:x%」切段，再从每段里剥掉固定词和末尾的教师名。
    """
    text = H.strip_tags(html or "")
    parts = re.split(r"进度\s*[:：]\s*(\d{1,3}(?:\.\d+)?)\s*%", text)
    out, seen = [], set()
    # parts 交替：片段, 数字, 片段, 数字 …
    for i in range(0, len(parts) - 1, 2):
        seg = parts[i]
        pct = _num(parts[i + 1])
        if pct is None or pct > 100:
            continue
        toks = seg.split()
        if not toks:
            continue
        # 末尾一个 token 是教师名（多人时是「李罡,张怡」这种）
        if re.fullmatch(r"[\u4e00-\u9fa5]{2,4}(?:[,、][\u4e00-\u9fa5]{2,4})*", toks[-1]):
            toks = toks[:-1]
        # 找最后一个固定词，取它右边的部分
        for j in range(len(toks) - 1, -1, -1):
            if toks[j] in FIXED_WORDS:
                toks = toks[j + 1:]
                break
        name = " ".join(toks).strip(" ·-")
        if len(name) < 2 or name in seen:
            continue
        seen.add(name)
        out.append({
            "id": _stable_id(name),
            "courseName": name, "percent": int(round(pct)),
            "watched": None, "total": None, "updatedAt": "",
            "note": "",
        })
    return out


def parse(html):
    items = _from_json(html)
    if not items:
        items = _from_dom(html)
    unparsed = []
    if not items:
        unparsed.append({
            "kind": "progress",
            "reason": "没找到学习进度数据（可能在异步接口里，需要按真实页面校准）",
            "sample": H.strip_tags((html or "")[:600]),
        })
    return {"items": items, "unparsed": unparsed}
