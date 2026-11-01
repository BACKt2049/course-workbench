# -*- coding: utf-8 -*-
"""天大教务（beangle/struts2）课表 → courses[]。

真实结构（登录后渲染出来的）：
    <table id="manualArrangeCourseTable">
      <thead><tr><th>节次/周次</th><th>星期一</th>…<th>星期日</th></tr></thead>
      <tbody>
        <tr>
          <td>第一节 08:30-09:15</td>
          <td id="TD0_0" rowspan="2" class="infoTitle"
              title="设计模式 (李罡);(4-11 周,45楼B206);图像处理 (李亮);(12-15 周,45楼B206)">…</td>
        </tr>

两个关键点让解析变得确定，不用猜：
  1. td 的 id 是 TD{星期索引×12}_{序号}，直接给出星期；
  2. title 属性是「课程名 (教师);(周次,教室)」的重复序列，格子里塞几门课都能拆开。

课表页是 AJAX 填充的，必须先由浏览器渲染过再交给这里（见 cas_qr.fetch_pages）。
"""

import hashlib
import re

from . import _html as H


def _stable_id(*parts):
    raw = "|".join([str(p or "") for p in parts]).encode("utf-8")
    return "tju_" + hashlib.md5(raw).hexdigest()[:10]


def _norm_category(name, raw):
    """前端只认 必修/选修/实验 三档（决定左侧色条），这里做归一。
    学校原值可能是「专业核心」「专业选修」「课程设计」等。"""
    s = "%s %s" % (raw or "", name or "")
    if "实验" in s:
        return "实验"
    if "选修" in s:
        return "选修"
    return "必修"


def _table_slice(html, marker="manualArrangeCourseTable"):
    i = html.find(marker)
    if i < 0:
        return ""
    start = html.rfind("<table", 0, i)
    end = html.find("</table>", i)
    if start < 0 or end < 0:
        return ""
    return html[start:end + 8]


def _weekday_from_id(td_id):
    m = re.match(r"TD(\d+)_", td_id or "")
    if not m:
        return 0
    return int(m.group(1)) // 12 + 1


def _parse_weeks(text):
    """'4-11 周' / '单5-19 周' / '双4-8 周' / '10 周' → (weeks, parity)"""
    t = (text or "").strip()
    parity = "每周"
    if t.startswith("单") or "单周" in t:
        parity = "单周"
    elif t.startswith("双") or "双周" in t:
        parity = "双周"
    m = re.search(r"(\d{1,2})\s*[-~－]\s*(\d{1,2})", t)
    if m:
        return "%s-%s" % (m.group(1), m.group(2)), parity
    m = re.search(r"(\d{1,2})", t)
    if m:
        return "%s-%s" % (m.group(1), m.group(1)), parity
    return "", parity


def _split_slots(title):
    """把 title 拆成 [(课程名, 教师, 周次, 教室), …]。"""
    segs = [s.strip() for s in re.split(r"[;；]", title or "") if s.strip()]
    out, cur = [], None
    for s in segs:
        if s.startswith("(") or s.startswith("（"):
            inner = s.strip("()（） ").strip()
            parts = [x.strip() for x in re.split(r"[,，]", inner)]
            if cur is not None:
                cur["weeks_text"] = parts[0] if parts else ""
                cur["room"] = parts[1] if len(parts) > 1 else ""
                out.append(cur)
                cur = None
            continue
        if cur is not None:
            out.append(cur)
        m = re.match(r"^(.*?)\s*[（(]([^）)]*)[）)]\s*$", s)
        if m:
            cur = {"name": m.group(1).strip(), "teacher": m.group(2).strip()}
        else:
            cur = {"name": s, "teacher": ""}
    if cur is not None:
        out.append(cur)
    return out


def _parse_beangle(html, categories=None):
    seg = _table_slice(html)
    if not seg:
        return []
    body = re.search(r"<tbody[\s\S]*?</tbody>", seg, re.I)
    body = body.group(0) if body else seg

    # 先按行收集节次时间，行号用于计算跨节结束时间
    rows = re.split(r"<tr[^>]*>", body, flags=re.I)[1:]
    row_times = []          # [(start, end)]
    row_cells = []          # [ [(td_html), …] ]
    for tr in rows:
        tds = re.findall(r"<td([^>]*)>([\s\S]*?)</td>", tr, re.I)
        if not tds:
            continue
        first_txt = H.strip_tags(tds[0][1])
        m = re.search(r"(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})", first_txt)
        row_times.append((m.group(1), m.group(2)) if m else ("", ""))
        row_cells.append(tds)

    categories = categories or {}
    items = []
    for ri, tds in enumerate(row_cells):
        for attrs, inner in tds[1:]:
            if "infoTitle" not in (attrs or "") and "title=" not in (attrs or ""):
                continue
            mt = re.search(r'title="([^"]*)"', attrs or "")
            if not mt:
                continue
            title = H.strip_tags(mt.group(1)).strip()
            if not title:
                continue
            mid = re.search(r'id="([^"]*)"', attrs or "")
            weekday = _weekday_from_id(mid.group(1) if mid else "")
            if not weekday:
                continue
            mr = re.search(r'rowspan="(\d+)"', attrs or "")
            span = int(mr.group(1)) if mr else 1
            start = row_times[ri][0] if ri < len(row_times) else ""
            end_idx = min(ri + span - 1, len(row_times) - 1)
            end = row_times[end_idx][1] if end_idx >= 0 else ""

            for slot in _split_slots(title):
                name = (slot.get("name") or "").replace("*", "").strip()
                if len(name) < 2:
                    continue
                weeks, parity = _parse_weeks(slot.get("weeks_text"))
                items.append({
                    "id": _stable_id(name, weekday, start, weeks),
                    "name": name,
                    "weekday": weekday,
                    "category": _norm_category(name, categories.get(name)),
                    "start": start, "end": end,
                    "room": slot.get("room", ""),
                    "teacher": slot.get("teacher", ""),
                    "weeks": weeks, "parity": parity,
                    "note": "",
                })
    return items


# ---------------- 下面两条是旧格式兜底，留着不碍事 ----------------

def _from_table(html):
    out = []
    for row in H.rows(html):
        tds = H.cells(row)
        if len(tds) < 8:
            continue
        head0 = H.strip_tags(tds[0])
        if "节次" in head0:
            continue
        m = re.search(r"(\d{1,2})", head0)
        row_sec = int(m.group(1)) if m else None
        for col in range(1, 8):
            cell = tds[col]
            if H.is_empty_cell(cell):
                continue
            lines = H.to_lines(cell)
            if not lines:
                continue
            name, teacher, room, weeks, parity = "", "", "", "", "每周"
            rest = []
            for ln in lines:
                if not weeks:
                    w, p = H.parse_weeks(ln)
                    if w:
                        weeks, parity = w, p
                        continue
                if not room:
                    r = H.find_room(ln)
                    if r:
                        room = r
                        continue
                rest.append(ln)
            if rest:
                name = rest[0]
                teacher = H.find_teacher(rest[1:])
            if not name or len(name) < 2:
                continue
            start, end = "", ""
            s0 = s1 = row_sec
            msec = re.search(r"第?\s*(\d{1,2})\s*[-~－]\s*(\d{1,2})\s*节", " ".join(lines))
            if msec:
                s0, s1 = int(msec.group(1)), int(msec.group(2))
            if s0 in H.JIE:
                start = H.JIE[s0][0]
                end = H.JIE.get(s1, H.JIE[s0])[1]
            out.append({
                "id": _stable_id(name, col, start),
                "name": name, "weekday": col,
                "category": H.guess_category(name, " ".join(lines)),
                "start": start, "end": end,
                "room": room, "teacher": teacher,
                "weeks": weeks or "", "parity": parity, "note": "",
            })
    return out


def _sample(html):
    s = re.sub(r"data:[a-z/+]+;base64,[A-Za-z0-9+/=]{80,}", "[base64]", html or "")
    return s[:1500]


def parse(html, categories=None):
    items = _parse_beangle(html, categories)
    unparsed = []
    if not items:
        items = _from_table(html)
    if not items:
        unparsed.append({
            "kind": "courses",
            "reason": "没解析出课程：可能登录态失效拿到空壳页，或课表结构变了",
            "sample": _sample(html),
        })
        return {"items": [], "unparsed": unparsed}

    # 同一门课可能排在多个时段，这里是不同条目，不做合并；
    # 只按 (课程名, 星期, 开始时间, 周次) 去重，防止重复渲染。
    seen, out = set(), []
    for c in items:
        k = (c["name"], c["weekday"], c["start"], c["weeks"])
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return {"items": out, "unparsed": unparsed}
