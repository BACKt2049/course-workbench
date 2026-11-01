# -*- coding: utf-8 -*-
"""解析器与状态机的离线测试：不联网、不需要登录态。

    python sync/tests/test_parsers.py

假页面是按正方 eams 与智慧树的常见结构手写的 —— 真实 DOM 一定会有出入，
所以真正要守住的不变量是：**认不出来时进 unparsed，而不是写一份空数据**。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from parsers import tju_courses, tju_exams, zhs_progress, zhs_work  # noqa: E402

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s %s" % (name, detail))


COURSE_HTML = """
<table>
<tr><td>节次</td><td>星期一</td><td>星期二</td><td>星期三</td><td>星期四</td>
    <td>星期五</td><td>星期六</td><td>星期日</td></tr>
<tr><td>1</td><td></td>
    <td>高等数学 A<br>王强<br>教一 101<br>1-16周</td>
    <td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>2</td><td></td>
    <td>高等数学 A<br>王强<br>教一 101<br>1-16周</td>
    <td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>5</td><td></td><td></td><td></td>
    <td>操作系统实验<br>张伟<br>实验楼 305<br>3-15周(单)</td>
    <td></td><td></td><td></td></tr>
</table>
"""

EXAM_HTML = """
<table>
<tr><td>课程名称</td><td>考试时间</td><td>考场</td><td>座位号</td><td>考核形式</td></tr>
<tr><td>操作系统</td><td>2026-11-02 09:00-11:00</td><td>教三 201</td><td>A12</td><td>闭卷</td></tr>
<tr><td>数据结构</td><td>11月5日 14:00-16:00</td><td>教二 108</td><td>B07</td><td>闭卷</td></tr>
</table>
"""

ZHS_WORK_HTML = """
<html><body><div id="app"></div>
<script>window.__INITIAL_STATE__ = {"data":{"homeworkList":[
  {"homeworkName":"操作系统实验三","courseName":"操作系统","endTime":"2026-10-31 23:00"},
  {"homeworkName":"数据结构编程题","courseName":"数据结构","endTime":1762279200000}
]}};</script>
</body></html>
"""

ZHS_PROGRESS_HTML = """
<html><body>
<script>var initData = {"courseList":[
  {"courseName":"操作系统","progress":62,"studyHour":3.5},
  {"courseName":"数据结构","progress":"41%"}
]};</script>
</body></html>
"""

EMPTY_HTML = "<html><body><p>登录已失效</p></body></html>"

print("\n[1] 正方课表")
r = tju_courses.parse(COURSE_HTML)
items = r["items"]
check("解析出 2 门课", len(items) == 2, len(items))
tue = [c for c in items if c["weekday"] == 2]
check("周二有课", len(tue) == 1)
if tue:
    c = tue[0]
    check("课名正确", c["name"].startswith("高等数学"), c["name"])
    check("节次换算 1-2 节", (c["start"], c["end"]) == ("08:00", "09:40"), (c["start"], c["end"]))
    check("教室", "教一" in c["room"], c["room"])
    check("教师", c["teacher"] == "王强", c["teacher"])
    check("周次", c["weeks"] == "1-16", c["weeks"])
exp = [c for c in items if c["category"] == "实验"]
check("实验课归类为实验", len(exp) == 1)
if exp:
    check("单周识别", exp[0]["parity"] == "单周", exp[0]["parity"])
    check("周次 3-15", exp[0]["weeks"] == "3-15", exp[0]["weeks"])

print("\n[2] 正方考试")
r = tju_exams.parse(EXAM_HTML)
items = r["items"]
check("解析出 2 场", len(items) == 2, len(items))
if items:
    a = items[0]
    check("课程名", a["courseName"] == "操作系统", a["courseName"])
    check("时间归一", a["time"] == "2026-11-02 09:00", a["time"])
    check("考场", "教三" in a["place"], a["place"])
    check("座位", a["seat"] == "A12", a["seat"])
    check("形式", a["type"] == "闭卷", a["type"])
if len(items) > 1:
    check("中文日期也能归一", items[1]["time"].endswith("14:00"), items[1]["time"])

print("\n[3] 智慧树作业")
r = zhs_work.parse(ZHS_WORK_HTML)
items = r["items"]
check("解析出 2 条作业", len(items) == 2, len(items))
if items:
    check("标题", items[0]["title"] == "操作系统实验三", items[0]["title"])
    check("截止时间", items[0]["due"] == "2026-10-31T23:00", items[0]["due"])
    check("课程名", items[0]["courseName"] == "操作系统", items[0]["courseName"])
if len(items) > 1:
    check("毫秒时间戳转成日期", bool(items[1]["due"]), items[1]["due"])

print("\n[4] 智慧树进度")
r = zhs_progress.parse(ZHS_PROGRESS_HTML)
items = r["items"]
check("解析出 2 门进度", len(items) == 2, len(items))
if items:
    check("百分比取整", items[0]["percent"] == 62, items[0]["percent"])
if len(items) > 1:
    check("字符串百分比", items[1]["percent"] == 41, items[1]["percent"])

print("\n[5] 认不出来时必须走 unparsed，不能返回空数据冒充成功")
for name, mod in (("课表", tju_courses), ("考试", tju_exams),
                  ("作业", zhs_work), ("进度", zhs_progress)):
    r = mod.parse(EMPTY_HTML)
    check("%s：items 为空且 unparsed 有内容" % name,
          len(r["items"]) == 0 and len(r["unparsed"]) >= 1,
          (len(r["items"]), len(r["unparsed"])))

print("\n[6] 天大扫码状态机（离线部分）")
import cas_qr  # noqa: E402

s = cas_qr.start() if False else None  # 不联网，直接构造
sess = cas_qr.Session("tju")
check("初始状态 qr_ready", sess.state == "qr_ready")
check("二维码内容指向 CAS", "appScanLogin" in sess.qr_text and sess.uuid in sess.qr_text, sess.qr_text[:60])
check("默认未过期", not sess.expired())
sess.created_at -= cas_qr.QR_TTL + 1
check("超时后判为过期", sess.expired())
pub = cas_qr.poll(sess)
check("过期后 poll 返回 expired", pub["state"] == "expired", pub["state"])
pub2 = cas_qr.poll(sess)
check("过期后不再发请求（幂等）", pub2["state"] == "expired")

print("\n[7] 二维码生成")
png = cas_qr.qr_png("https://sso.tju.edu.cn/cas/appScanLogin?uuid=abc&service=x")
check("产出 PNG 字节", png is not None and png[:8] == b"\x89PNG\r\n\x1a\n", (png or b"")[:8])

print("\n" + "=" * 46)
print("通过 %d 项，失败 %d 项" % (passed, failed))
sys.exit(1 if failed else 0)
