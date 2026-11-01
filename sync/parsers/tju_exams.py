# -*- coding: utf-8 -*-
"""天大教务「我的考试」→ exams[]，附带课程类别清单。

真实表头（实测）：
    课程序号 | 课程名称 | 考试类别 | 考试日期 | 批次 | 考试安排 |
    考试地点 | 考场座位号 | 考试情况 | 其它说明

学期初这张表里「考试日期 / 考试地点」是「时间未安排 / 地点未安排」，
这不是解析失败，是学校还没排考。这种情况返回 0 条 + 一条 unparsed 说明，
前端会显示「学校尚未发布考试安排」，而不是塞一堆没时间的假考试。
"""

import hashlib
import re

from . import _html as H

HEAD_HINTS = {
    # 注意「课程序号」也含"课程"二字，这里必须精确到「课程名称」，否则整列错位
    "courseName": ("课程名称",),
    "kind": ("考试类别", "考核方式", "考试形式", "考核形式"),
    "time": ("考试日期", "考试时间"),
    "batch": ("批次",),
    "arrange": ("考试安排",),
    "place": ("考试地点", "考场"),
    "seat": ("座位",),
    "status": ("考试情况", "状态"),
    "note": ("其它说明", "备注"),
}

UNSET = ("未安排", "时间未安排", "地点未安排", "待定", "-", "—")


def _unset(text):
    return text.strip() in ('-', '—') or any(x in text for x in UNSET if x not in ('-', '—'))


def _stable_id(*parts):
    raw = "|".join([str(p or "") for p in parts]).encode("utf-8")
    return "tju_exam_" + hashlib.md5(raw).hexdigest()[:10]


def _find_table(html, needles):
    """挑出同时含「课程名称」和指定列名的表格，有多张时取行数最多的。

    页面上有两张长得很像的表：
      · 课程清单：课程序号 | 课程名称 | 课程类别 | 开课院系 | 学分
      · 考试安排：课程序号 | 课程名称 | 考试类别 | 考试日期 | … | 考场座位号
    另外这些表嵌在 eams 的布局大表里，外层表也含「考试时间」字样，
    所以判定条件要足够严（比如同时出现「考场座位号」），并按行数取最大。
    """
    best, best_rows = "", -1
    for m in re.finditer(r"<table[^>]*>([\s\S]*?)</table>", html or "", re.I):
        seg = m.group(0)
        if "课程名称" not in seg:
            continue
        if not any(n in seg for n in needles):
            continue
        rows = len(re.findall(r"<tr", seg, re.I))
        if rows > best_rows:
            best, best_rows = seg, rows
    return best


def _map_header(head_html):
    idx = {}
    for i, th in enumerate(re.findall(r"<t[hd][^>]*>([\s\S]*?)</t[hd]>", head_html, re.I)):
        t = H.strip_tags(th)
        for key, hints in HEAD_HINTS.items():
            if key in idx:
                continue
            if any(h in t for h in hints):
                idx[key] = i
    return idx


def _norm_dt(text):
    t = (text or "").strip()
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m2 = re.search(r"(\d{1,2})[-/月](\d{1,2})", t)
        if not m2:
            return ""
        from datetime import datetime
        y, mo, d = datetime.now().year, int(m2.group(1)), int(m2.group(2))
    tm = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", t)
    base = "%04d-%02d-%02d" % (y, mo, d)
    return base + (" %02d:%02d" % (int(tm.group(1)), int(tm.group(2))) if tm else " 09:00")


def course_list(html):
    """从「我的考试」页取出 {课程名: 课程类别}，给课表补 category 用。"""
    seg = _find_table(html, ("课程类别", "开课院系"))
    if not seg:
        return {}
    head = re.search(r"<thead[\s\S]*?</thead>", seg, re.I)
    idx = _map_header(head.group(0) if head else seg)
    cname, ckind = idx.get("courseName", 1), idx.get("kind", 2)
    out = {}
    body = re.search(r"<tbody[\s\S]*?</tbody>", seg, re.I)
    for row in H.rows(body.group(0) if body else seg):
        tds = [_clean(x) for x in H.cells(row)]
        if len(tds) <= max(cname, ckind):
            continue
        name = tds[cname]
        if name and len(name) > 1:
            out[name.replace("*", "")] = tds[ckind] or ""
    return out


def _clean(cell):
    return re.sub(r"\s+", " ", H.strip_tags(cell)).strip()


def parse(html):
    seg = _find_table(html, ("考试日期", "考试时间", "考场座位号"))
    if not seg:
        return {"items": [], "unparsed": [{
            "kind": "exams",
            "reason": "没找到考试表（可能登录态失效或页面改版）",
            "sample": H.strip_tags((html or "")[:600]),
        }]}

    head = re.search(r"<thead[\s\S]*?</thead>", seg, re.I)
    idx = _map_header(head.group(0) if head else seg)
    body = re.search(r"<tbody[\s\S]*?</tbody>", seg, re.I)
    rows = H.rows(body.group(0) if body else seg)

    items, pending = [], 0
    for row in rows:
        tds = [_clean(x) for x in H.cells(row)]
        if not tds:
            continue
        get = lambda k, d="": (tds[idx[k]] if idx.get(k) is not None and idx[k] < len(tds) else d)
        name = get("courseName")
        if not name or name in ("课程名称",):
            continue
        time_txt = get("time")
        place = get("place")
        seat = get("seat")
        time_iso = _norm_dt(time_txt)
        if not time_iso or _unset(time_txt):
            pending += 1
            continue
        items.append({
            "id": _stable_id(name, time_iso),
            "courseName": name.replace("*", ""),
            "time": time_iso,
            "place": "" if _unset(place) else place,
            "seat": "" if _unset(seat) else seat,
            "type": get("kind") or "考试",
            "note": get("note"),
        })

    unparsed = []
    if not items:
        if pending:
            unparsed.append({
                "kind": "exams",
                "reason": "学校还没发布考试安排：%d 门课在列表里，但考试日期都是「时间未安排」。"
                          "等教务排考后点一次「全部更新」就会自动出现。" % pending,
                "sample": "",
            })
        else:
            unparsed.append({
                "kind": "exams",
                "reason": "考试表里没有可用的行",
                "sample": H.strip_tags(seg[:600]),
            })
    return {"items": items, "unparsed": unparsed}
