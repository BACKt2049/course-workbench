# -*- coding: utf-8 -*-
"""端到端：造一份同步数据 → 用 Playwright 打开本地页面 → 走前端合并逻辑 → 断言。

前置：先起服务（python sync/cli.py serve --no-open）。
注意：会往 sync/data/latest.json 写测试数据，跑完自动清空。

不依赖真实登录态，验证的是「服务 → 前端 → 视图」这条链路本身。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "sync"))

import store  # noqa: E402

# ---- 1. 造数据（模拟教务与智慧树的解析结果） ----
courses = [
    {"id": "tju_c1", "name": "操作系统", "weekday": 5, "category": "必修", "start": "08:00", "end": "09:40",
     "room": "教三 402", "teacher": "张伟", "weeks": "1-16", "parity": "每周", "note": ""},
    {"id": "tju_c2", "name": "数据结构", "weekday": 5, "category": "必修", "start": "10:00", "end": "11:40",
     "room": "教二 201", "teacher": "李娜", "weeks": "1-16", "parity": "每周", "note": ""},
    {"id": "tju_c3", "name": "编译原理", "weekday": 1, "category": "必修", "start": "14:00", "end": "15:40",
     "room": "教一 305", "teacher": "陈明", "weeks": "1-16", "parity": "每周", "note": ""},
]
exams = [
    {"id": "ex1", "courseName": "操作系统", "time": "2026-11-02 09:00", "place": "教三 201", "seat": "A12", "type": "闭卷"},
    {"id": "ex2", "courseName": "数据结构", "time": "2026-11-05 14:00", "place": "教二 108", "seat": "B07", "type": "闭卷"},
    {"id": "ex3", "courseName": "编译原理", "time": "2026-11-19 09:00", "place": "教一 402", "seat": "C21", "type": "开卷"},
]
ddls = [
    {"id": "z1", "title": "操作系统实验三", "courseName": "操作系统", "type": "作业",
     "due": "2026-10-31T23:00", "minutes": 90, "done": False,
     "note": "来自智慧树页面数据，提交要求请以站内为准"},
    {"id": "z2", "title": "数据结构编程题", "courseName": "数据结构", "type": "作业",
     "due": "2026-11-04T22:00", "minutes": 60, "done": False,
     "note": "来自智慧树页面数据，提交要求请以站内为准"},
]
progress = [
    {"id": "p1", "courseName": "操作系统", "percent": 62, "watched": 3.5, "total": None, "updatedAt": "", "note": ""},
    {"id": "p2", "courseName": "数据结构", "percent": 41, "watched": 2.0, "total": None, "updatedAt": "", "note": ""},
]

store.merge_latest("tju", {"courses": courses, "exams": exams}, replace=True)
store.merge_latest("zhs", {"ddl": ddls, "progress": progress}, replace=True)

info = json.loads((store.STATE / "server.json").read_text(encoding="utf-8"))
URL = "http://127.0.0.1:%d/" % info["port"]

out = []
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1080, "height": 1250})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle")
    pg.wait_for_timeout(800)
    out.append("页面标题: %s" % pg.title())
    out.append("同步卡可见: %s" % pg.is_visible("#syncCard"))
    out.append("状态行数: %d" % pg.locator(".sync-row").count())

    # 走前端真实的合并逻辑（与点「全部更新」后执行的同一段代码）
    merged = pg.evaluate("""async () => {
        const d = await wbApi('/api/latest');
        DB = mergeSync(DB, d, true);
        return { courses: DB.courses.length, exams: DB.exams.length,
                 ddl: DB.ddl.length, srcs: [...new Set(DB.courses.map(c => c.src))],
                 hasCoursesKey: Array.isArray(d.courses) };
    }""")
    out.append("合并后: %s" % json.dumps(merged, ensure_ascii=False))

    pg.evaluate("go('exams')")
    pg.wait_for_timeout(600)
    diag = pg.evaluate("""() => ({
        hidden: document.getElementById('view-exams').hidden,
        items: document.querySelectorAll('#examList .tl-item').length,
        htmlLen: document.getElementById('examList').innerHTML.length,
        head: document.getElementById('examList').innerHTML.slice(0, 160),
        examsInDB: (DB.exams || []).length,
        statsHtml: document.getElementById('examStats').innerHTML.slice(0, 80)
    })""")
    out.append("诊断: %s" % json.dumps(diag, ensure_ascii=False))
    out.append("考试视图可见: %s" % pg.is_visible("#view-exams"))
    out.append("考试条数: %d" % pg.locator("#examList .tl-item").count())
    if diag["items"]:
        out.append("首条考试: %s" % pg.locator("#examList .tl-item .name").first.inner_text(timeout=3000))
        out.append("考试标签: %s" % pg.locator("#examList .exam-tag").first.inner_text(timeout=3000))
    pg.screenshot(path=str(ROOT / ".s_exams.png"))

    pg.evaluate("go('ddl')")
    pg.wait_for_timeout(400)
    out.append("DDL 条数: %d" % pg.locator("#board .ddl").count())
    pg.screenshot(path=str(ROOT / ".s_ddl2.png"))

    pg.evaluate("go('home')")
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(ROOT / ".s_home2.png"))

    out.append("页面 JS 错误: %s" % (errs or "无"))
    b.close()

store.write_json(store.LATEST, {"courses": [], "exams": [], "ddl": [], "progress": [], "unparsed": [], "generatedAt": ""})
(ROOT / ".e2e.txt").write_text("\n".join(out), encoding="utf-8")
print("\n".join(out))
