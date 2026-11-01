# -*- coding: utf-8 -*-
"""解析层共用的 HTML 小工具。

刻意不引 BeautifulSoup：这一层只用得到标签剥离、表格切分和文本归一，
标准库足够，少一个依赖就少一处装不上的可能。
"""

import html as _html
import re

_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>|</p>|</div>|</li>", re.I)
_WS = re.compile(r"[ \t\u00a0]+")


def strip_tags(s):
    return _html.unescape(_TAG.sub(" ", s or "")).strip()


def to_lines(cell):
    """单元格 → 行列表。课表里「课程名/教师/教室/周次」是靠 <br> 分行的。"""
    s = _BR.sub("\n", cell or "")
    s = _html.unescape(_TAG.sub("", s))
    out = []
    for ln in s.split("\n"):
        ln = _WS.sub(" ", ln).strip()
        if ln:
            out.append(ln)
    return out


def rows(html):
    return re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S | re.I)


def cells(row_html):
    return re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html or "", re.S | re.I)


def is_empty_cell(cell):
    t = strip_tags(cell)
    return len(t) < 2 or t in ("-", "—", "无", "&nbsp;", "　")


def norm_time(s):
    """把 8:00 / 08.00 / 8：00 统一成 08:00。"""
    m = re.search(r"(\d{1,2})\s*[:：.]\s*(\d{2})", s or "")
    if not m:
        return ""
    return "%02d:%02d" % (int(m.group(1)), int(m.group(2)))


def find_weekday(text):
    m = re.search(r"星期([一二三四五六日天])", text or "")
    if m:
        return "一二三四五六日".index("日" if m.group(1) == "天" else m.group(1)) + 1
    m = re.search(r"周([一二三四五六日天])", text or "")
    if m:
        return "一二三四五六日".index("日" if m.group(1) == "天" else m.group(1)) + 1
    return 0


def parse_weeks(text):
    """返回 (weeks, parity)。周次形态五花八门，只要认得出来就先记下。"""
    t = text or ""
    parity = "每周"
    if "单" in t and "双" not in t:
        parity = "单周"
    elif "双" in t:
        parity = "双周"
    m = re.search(r"(\d{1,2})\s*[-~－]\s*(\d{1,2})\s*周", t)
    if m:
        return "%s-%s" % (m.group(1), m.group(2)), parity
    m = re.search(r"(\d{1,2})\s*周", t)
    if m:
        return "%s-%s" % (m.group(1), m.group(1)), parity
    return "", parity


ROOM_RE = re.compile(
    r"(教[一二三四五六七八九十\d]{1,3}\s*[A-Za-z]?\s*[-－]?\s*\d{1,4}"
    r"|[A-Za-z]?\d{1,2}\s*[-－#]\s*\d{2,4}"
    r"|[\u4e00-\u9fa5]{1,4}楼\s*[A-Za-z]?\d{1,4}"
    r"|实验(?:室|中心)\s*[A-Za-z]?\d{1,3})")

NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")


def find_room(text):
    m = ROOM_RE.search(text or "")
    return m.group(1).replace("－", "-").replace(" ", "") if m else ""


def find_teacher(candidates):
    """在若干候选中挑一个最像姓名的：2-4 个汉字、不含教室/周次特征。"""
    for c in candidates:
        c = (c or "").strip()
        if not c or not NAME_RE.match(c):
            continue
        if any(k in c for k in ("周", "楼", "室", "课", "班", "组")):
            continue
        if ROOM_RE.search(c):
            continue
        return c
    return ""


def guess_category(name, text=""):
    s = (name or "") + (text or "")
    if "实验" in s or "上机" in s:
        return "实验"
    if any(k in s for k in ("选修", "任选", "限选", "通识", "公选")):
        return "选修"
    return "必修"


JIE = {
    1: ("08:00", "08:45"), 2: ("08:55", "09:40"), 3: ("10:00", "10:45"), 4: ("10:55", "11:40"),
    5: ("14:00", "14:45"), 6: ("14:55", "15:40"), 7: ("16:00", "16:45"), 8: ("16:55", "17:40"),
    9: ("19:00", "19:45"), 10: ("19:55", "20:40"), 11: ("20:50", "21:35"), 12: ("21:45", "22:30"),
}
