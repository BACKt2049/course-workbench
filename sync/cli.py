# -*- coding: utf-8 -*-
"""命令行入口。

    python sync/cli.py serve            起本地服务（默认 8787，自动开浏览器）
    python sync/cli.py up tju           在天大生成二维码 → state/qr_tju.png，扫完自动拉取
    python sync/cli.py up zhs           同上，智慧树（会开一个浏览器窗口）
    python sync/cli.py pull all         直接拉一次（需要已有登录态）
    python sync/cli.py status           看登录态、最近拉取、各表条数
    python sync/cli.py snapshot         生成只读快照
"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if getattr(sys,'frozen',False):
    import runtime_config
    sys.excepthook = runtime_config.report_failure

if __name__ == '__main__' and (len(sys.argv) == 1 or sys.argv[1] == 'desktop'):
    import runtime_config
    selected = runtime_config.prepare_desktop()
    if selected is None:
        sys.exit(0)
    os.environ['WB_DATA'] = str(selected)

import store  # noqa: E402

# 在任何 playwright 导入之前：优先用 exe 旁边自带的浏览器内核
store.prepare_browser()


def cmd_serve(a):
    import server
    server.serve(port=a.port, open_browser=not a.no_open, auto=a.auto,
                 lan=getattr(a, 'lan', False) and not getattr(a, "no_lan", False))


def cmd_desktop(a):
    """桌面模式：独立窗口，不是浏览器标签页。"""
    import launcher
    return launcher.run(port=getattr(a, "port", None))


def cmd_up(a):
    import pipeline
    import browser_login
    site = a.site
    print("\n=== %s 扫码登录 ===" % store.SITE_NAME[site])
    sess = browser_login.start(site)
    out = store.STATE / ("qr_%s.png" % site)
    wrote = False
    deadline = time.time() + 200
    while time.time() < deadline:
        st = sess.public()
        if st["hasImage"] and not wrote:
            out.write_bytes(sess.png)
            wrote = True
            print("[✓] 二维码已写入：%s" % out)
            print("    %s" % st["hint"])
        if st["state"] in ("done", "expired", "error"):
            print("    [%s] %s" % (st["state"], st.get("error") or st.get("note") or ""))
            break
        if not wrote and st["state"] == "waiting_manual":
            print("    [!] %s" % (st.get("note") or "没截到二维码"))
        time.sleep(2)
    if sess.snap()["state"] != "done":
        if not wrote:
            print("[x] 没能生成二维码，详见 sync/state/events.log")
        return
    print("[✓] 登录成功，开始拉取…")
    try:
        if site == "tju":
            r = pipeline.pull_tju()
            print("    课程 %d 门，考试 %d 条" % (r["counts"]["courses"], r["counts"]["exams"]))
        else:
            r = pipeline.pull_zhs()
            print("    作业 %d 条，进度 %d 条" % (r["counts"]["ddl"], r["counts"]["progress"]))
        if r["unparsed"]:
            print("    [!] %d 条待确认（原文在 sync/raw/）" % len(r["unparsed"]))
    except Exception as e:
        print("[x] 拉取失败：%s" % e)


def cmd_pull(a):
    import pipeline
    sites = ("tju", "zhs") if a.site == "all" else (a.site,)
    r = pipeline.pull(sites=sites)
    for site, info in r.get("sites", {}).items():
        name = store.SITE_NAME.get(site, site)
        if info.get("ok"):
            c = info.get("counts", {})
            print("[✓] %s：%s" % (name, "，".join("%s %d" % (k, v) for k, v in c.items())))
        elif info.get("needLogin"):
            print("[!] %s：需要重新扫码（%s）" % (name, info.get("note")))
        else:
            print("[x] %s：%s" % (name, info.get("error")))


def cmd_status(a):
    s = store.status()
    print("\n数据生成于：%s" % (s.get("generatedAt") or "从未拉取"))
    for site, info in s["sites"].items():
        flag = "已登录" if info["loggedIn"] else "未登录"
        print("- %s：%s" % (info["label"], flag))
        sess = info.get("session") or {}
        if info["loggedIn"]:
            if sess.get("expiresAt"):
                left = sess.get("daysLeft")
                if left is not None and left > 0:
                    print("    登录态有效至 %s，还剩 %s 天"
                          % (sess["expiresAt"][:16].replace("T", " "), left))
                else:
                    print("    登录态已过期，需要重新扫码")
            elif sess.get("note"):
                print("    " + sess["note"])
        if info.get("keepalive"):
            print("    最近保活 %s" % info["keepalive"][:16].replace("T", " "))
        if info.get("lastPull"):
            ok = "成功" if info.get("lastPullOk") else "失败"
            print("    上次拉取 %s（%s）%s" % (info["lastPull"], ok, info.get("lastNote") or ""))
    print("条数：" + "，".join("%s %d" % (k, v) for k, v in s["counts"].items()))
    print("\n最近事件：")
    for e in store.recent_events(10):
        print("  %s  %-10s %s" % (e["at"], e["kind"], (e["detail"] or "")[:90]))


def cmd_snapshot(a):
    import json
    data = store.read_latest()
    import ui_assets
    html = ui_assets.load_html()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    extra = ('<script id="wb-snapshot" type="application/json">%s</script>'
             '<meta name="wb-snapshot-at" content="%s">') % (payload, data.get("generatedAt") or store.now_iso())
    import server
    out = server.inject(html, "snapshot", "", extra)
    f = store.DATA / ("snapshot-%s.html" % stamp)
    f.write_text(out, encoding="utf-8")
    print("[✓] 快照：%s" % f)
    print("    这份文件不含 token 与登录态，可以直接发布。")


def cmd_logout(a):
    store.clear_state(a.site)
    print("[✓] 已清除 %s 的登录态" % store.SITE_NAME.get(a.site, a.site))


def main():
    ap = argparse.ArgumentParser(description="校园课程工作台 · 本地同步")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="起本地服务")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--no-lan", action="store_true", help="只允许本机访问（兼容旧命令，现为默认）")
    p.add_argument("--lan", action="store_true", help="显式允许局域网访问")
    p.add_argument("--auto", action="store_true", help="开启每天 08:00 / 20:00 自动刷新")

    p = sub.add_parser("desktop", help="桌面窗口模式（独立窗口，无地址栏）")
    p.add_argument("--port", type=int, default=None)

    p = sub.add_parser("up", help="生成二维码并等待扫码")
    p.add_argument("site", choices=["tju", "zhs"])

    p = sub.add_parser("pull", help="拉取数据")
    p.add_argument("site", nargs="?", default="all", choices=["tju", "zhs", "all"])

    sub.add_parser("status", help="查看状态")
    sub.add_parser("snapshot", help="生成只读快照")

    p = sub.add_parser("logout", help="清除登录态")
    p.add_argument("site", choices=["tju", "zhs"])

    p = sub.add_parser('answers', help='刷新智慧树答案（原探测命令仍可用）')
    p.add_argument('--course-id', type=int, action='append')
    p.add_argument('--export')
    p.add_argument('--download', action='store_true')
    p.add_argument('--quiet', action='store_true')

    a = ap.parse_args()
    if not a.cmd:
        # 双击 exe / 不带参数时：弹独立窗口（无地址栏、无标签页）
        class _Default(object):
            port = None
        sys.exit(cmd_desktop(_Default()) or 0)

    # 走到这里说明是命令行用法。console=False 打包后没有控制台，
    # 得临时挂一个，否则用户在终端里看不到任何输出。
    import launcher
    launcher.ensure_console()

    if a.cmd == 'answers':
        import zhs_answer_probe
        values = []
        for course in a.course_id or []:
            values += ['--course-id', str(course)]
        if a.export:
            values += ['--export', a.export]
        if a.download:
            values.append('--download')
        if a.quiet:
            values.append('--quiet')
        sys.exit(zhs_answer_probe.main(values))

    {"serve": cmd_serve, "desktop": cmd_desktop,
     "up": cmd_up, "pull": cmd_pull,
     "status": cmd_status, "snapshot": cmd_snapshot, "logout": cmd_logout}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        error = traceback.format_exc()
        import runtime_config
        log = runtime_config.settings_path().parent / 'startup.log'
        try:
            log.parent.mkdir(parents=True,exist_ok=True)
            with log.open('a',encoding='utf-8') as stream:
                stream.write(time.strftime('%Y-%m-%d %H:%M:%S')+'\n'+error+'\n')
        except OSError:
            pass
        if os.name == 'nt' and (len(sys.argv)==1 or sys.argv[1]=='desktop'):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'启动暂时失败，数据文件已保留。请重新打开程序；若仍失败，请查看诊断日志：\n'+str(log),'课程工作台启动恢复',0x10)
        else:
            raise
