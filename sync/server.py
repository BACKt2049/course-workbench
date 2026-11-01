# -*- coding: utf-8 -*-
"""只监听 127.0.0.1 的本地服务：托管工作台 + 提供扫码与拉取接口。

设计取舍：
- 用标准库 ThreadingHTTPServer 而不是 FastAPI —— 单用户、十条路由、
  Playwright 的 sync API 与 async 框架天然打架，少一层依赖少一处装不上。
- 所有 /api 都要 token（页头 X-WB-Token 或 ?t=），因为 <img> 带不了自定义头，
  二维码图片走查询串。
- 校验 Host 与 Origin，防 DNS rebinding 和跨站调用。
"""

import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import store
import attachments
from app_info import APP_VERSION

# 尽早切到自带内核（若存在），必须在任何 playwright 使用之前
BROWSER_DIR = store.prepare_browser()

ROOT = Path(__file__).resolve().parent
HTML_NAME = "校园课程工作台.html"
TOKEN = secrets.token_hex(16)
PORT = int(os.environ.get("WB_PORT", "8787"))
STOP_REQUESTED = threading.Event()
ACTIVE_SERVER = None
PAGE_HTML = None

SESSIONS = {}          # site -> 扫码会话对象
SESS_LOCK = threading.Lock()
JOBS = {}              # jobId -> {state, step, result, error, at}
JOB_LOCK = threading.Lock()

ALLOWED_HOSTS = set()
ORIGIN_OK = set()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def inject(html, mode="local", token=None, extra=""):
    """把运行模式与 token 塞进页面，前端据此决定显示同步卡还是只读提示。"""
    head = (
        '<meta name="wb-mode" content="%s">\n'
        '<meta name="wb-api" content="http://127.0.0.1:%d">\n'
        '<meta name="wb-token" content="%s">\n'
        '<meta name="wb-version" content="%s">\n'
    ) % (mode, PORT, token or "", APP_VERSION)
    html = re.sub(r"<meta name=\"wb-(mode|api|token|version)\"[^>]*>\s*", "", html)
    return html.replace("</head>", head + extra + "</head>", 1)


class Job(object):
    def __init__(self, jid):
        self.id = jid
        self.state = "running"
        self.step = "准备中"
        self.result = None
        self.error = ""
        self.at = time.time()

    def public(self):
        return {"id": self.id, "state": self.state, "step": self.step,
                "result": self.result, "error": self.error,
                "progress": getattr(self, 'progress', {})}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WorkbenchSync/1.0"

    # ---------- 基础设施 ----------

    def log_message(self, fmt, *args):
        # 设 WB_REQLOG=1 时把每条请求写进 req.log，排查「页面打不开」用
        if os.environ.get("WB_REQLOG"):
            try:
                line = "%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args)
                with open(str(store.ROOT / "req.log"), "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass
        return

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, _json_bytes(obj))

    def _err(self, code, msg, **kw):
        payload = {"ok": False, "error": msg}
        payload.update(kw)
        self._json(payload, code)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        return host in ALLOWED_HOSTS

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.lower() in ORIGIN_OK

    def _token_ok(self, query):
        tok = self.headers.get("X-WB-Token") or (query.get("t", [""])[0])
        return tok == TOKEN

    def _guard(self, query, need_token=True):
        if not self._host_ok():
            self._err(403, "Host 不被允许")
            return False
        if not self._origin_ok():
            self._err(403, "跨站请求被拒绝")
            return False
        if need_token and not self._token_ok(query):
            self._err(401, "token 无效，请从本机打开的页面访问")
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        if path == '/api/answers' or path.startswith('/api/answers/'):
            if not self._guard(q):
                return
            from answers.api import dispatch
            return dispatch(self, 'GET', path, q)

        if path in ("/", "/index.html"):
            return self._page()
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not path.startswith("/api/"):
            return self._static(path)

        if path == '/api/bootstrap':
            if not self._guard(q,need_token=False):
                return
            return self._json({'ok':True,'token':TOKEN,'version':APP_VERSION})

        if path == '/api/health':
            if not self._guard(q):
                return
            health = {'ok':True,'version':APP_VERSION,'dataDir':str(store.ROOT.resolve()),
                      'pageReady':bool(PAGE_HTML),'pid':os.getpid(),'browser':BROWSER}
            if q.get('deep',[''])[0] == '1':
                ok,note = check_browser()
                health.update(deepBrowserCheck=ok,deepBrowserNote=note)
            return self._json(health)

        if path == '/api/storage':
            if not self._guard(q):
                return
            import data_safety
            return self._json({'ok':True,'path':str(store.ROOT.resolve()),
                               'backup':data_safety.information(store.ROOT),
                               'backupError':BACKUP_ERROR,'version':APP_VERSION})

        if path == '/api/workspace':
            if not self._guard(q):
                return
            return self._json({'ok':True,'workspace':store.read_json(store.DATA/'workspace.json',None)})

        if path == "/api/status":
            if not self._guard(q):
                return
            st = store.status()
            st["recent"] = store.recent_events(8)
            st["browser"] = BROWSER
            st["ok"] = True
            return self._json(st)

        if path == "/api/setup":
            if not self._guard(q):
                return
            return self._json({"ok": True, "browser": BROWSER})

        if path in ("/api/attachments", "/api/attachments/file"):
            if not self._guard(q):
                return
            try:
                if path == "/api/attachments":
                    return self._json(attachments.listing())
                file = attachments.path_for(q.get("name", [""])[0])
                if not file.is_file():
                    return self._err(404, "文件不存在，请刷新")
                ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                inline = ctype in ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain")
                disposition = ("inline" if inline else "attachment") + "; filename*=UTF-8''" + urllib.parse.quote(file.name)
                return self._send(200, file.read_bytes(), ctype,
                                  {"Content-Disposition": disposition})
            except ValueError as e:
                return self._err(400, str(e))
            except OSError as e:
                return self._err(500, "读取附件失败：" + str(e)[:120])

        if path == "/api/latest":
            if not self._guard(q):
                return
            return self._json(store.read_latest())

        m = re.match(r"^/api/login/(tju|zhs)/qr/([0-9a-f]+)\.png$", path)
        if m:
            site, sid = m.group(1), m.group(2)
            if not self._host_ok() or not self._token_ok(q):
                return self._err(403, "拒绝访问二维码")
            with SESS_LOCK:
                sess = SESSIONS.get(site)
            if not sess or getattr(sess, "sid", "") != sid:
                return self._err(404, "会话不存在或已过期")
            png = getattr(sess, "png", None)
            if not png:
                return self._err(404, "二维码尚未生成")
            return self._send(200, png, "image/png")

        m = re.match(r"^/api/login/(tju|zhs)/poll$", path)
        if m:
            if not self._guard(q):
                return
            site = m.group(1)
            sid = q.get("sid", [""])[0]
            with SESS_LOCK:
                sess = SESSIONS.get(site)
            if not sess or getattr(sess, "sid", "") != sid:
                return self._json({"ok": True, "state": "expired", "error": "会话已失效，请重新生成"})
            st = sess.public()
            st["ok"] = True
            return self._json(st)

        m = re.match(r"^/api/pull/([0-9a-f]+)$", path)
        if m:
            if not self._guard(q):
                return
            with JOB_LOCK:
                job = JOBS.get(m.group(1))
            if not job:
                return self._err(404, "任务不存在")
            return self._json(job.public())

        if path == "/api/logout":
            if not self._guard(q):
                return
            site = q.get("site", [""])[0]
            if site in ("tju", "zhs"):
                store.clear_state(site)
                return self._json({"ok": True})
            return self._err(400, "site 参数不对")

        if path.startswith("/snapshot/"):
            return self._snapshot_file(path)

        return self._err(404, "没有这个接口")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        if path in ('/api/workspace','/api/storage/backup','/api/storage/open','/api/shutdown'):
            if not self._guard(q):
                return
            if path == '/api/workspace':
                body = self._body()
                value = body.get('workspace')
                if not isinstance(value,dict) or not isinstance(value.get('settings'),dict) or any(not isinstance(value.get(k),list) for k in ('courses','ddl','notes')):
                    return self._err(400,'工作台数据格式不完整，已保留现有数据')
                if (any(not isinstance(x,dict) or not isinstance(x.get('id'),str) for x in value['ddl'])
                    or not isinstance(value.get('deletedTasks',[]),list)
                    or any(not isinstance(x,dict) for x in value.get('deletedTasks',[]))):
                    return self._err(400,'任务数据格式不正确，已保留现有数据')
                try:
                    value = store.save_workspace(value)
                    return self._json({'ok':True,'workspace':value})
                except (ValueError,TypeError):
                    return self._err(400,'任务数据格式不正确，已保留现有数据')
                except OSError as error:
                    return self._err(507,'数据保存失败，请检查目录权限和磁盘空间：'+str(error)[:100])
            if path == '/api/storage/open':
                os.startfile(str(store.ROOT))
                return self._json({'ok':True})
            if path == '/api/storage/backup':
                import data_safety
                try:
                    return self._json(data_safety.backup(store.ROOT,reason='手动备份',force=True))
                except OSError as error:
                    return self._err(507,'备份失败：'+str(error)[:160])
            if path == '/api/shutdown':
                if self.client_address[0] not in ('127.0.0.1','::1'):
                    return self._err(403,'只能在本机重启程序')
                self._json({'ok':True})
                STOP_REQUESTED.set()
                def stop():
                    if ACTIVE_SERVER:
                        ACTIVE_SERVER.shutdown()
                        ACTIVE_SERVER.server_close()
                threading.Thread(target=stop,daemon=True).start()
                return

        if path.startswith('/api/answers/'):
            if not self._guard(q):
                return
            from answers.api import dispatch
            return dispatch(self, 'POST', path, q)

        if path.startswith("/api/attachments/"):
            if not self._guard(q):
                return
            try:
                action = path.rsplit("/", 1)[-1]
                if action == "upload":
                    length = int(self.headers.get("Content-Length") or -1)
                    file = attachments.upload(q.get("name", [""])[0], self.rfile, length,
                                              replace=q.get("replace", [""])[0] == "1",
                                              task_id=q.get("taskId", [None])[0])
                    return self._json({"ok": True, "file": file})
                body = self._body()
                if action == "open":
                    os.startfile(str(attachments.root()))
                    return self._json({"ok": True})
                if action == "rename":
                    file = attachments.rename(body.get("name"), body.get("newName"))
                    return self._json({"ok": True, "file": file})
                if action == "delete":
                    attachments.delete(body.get("name"))
                    return self._json({"ok": True})
                return self._err(404, "没有这个附件操作")
            except (ValueError, FileExistsError, FileNotFoundError) as e:
                self.close_connection = True
                return self._err(400, str(e))
            except OSError as e:
                self.close_connection = True
                return self._err(500, "附件操作失败：" + str(e)[:120])

        if path == "/api/pull":
            if not self._guard(q):
                return
            body = self._body()
            sites = body.get("targets") or ["tju", "zhs"]
            which = body.get("what")
            return self._start_job(sites, which)

        if path == "/api/snapshot":
            if not self._guard(q):
                return
            return self._make_snapshot()

        if path == "/api/setup/browser":
            if not self._guard(q):
                return
            if store.BROWSER_BUNDLED:
                return self._json({'ok':True,'browser':BROWSER})
            if BROWSER.get("state") != "installing":
                threading.Thread(target=_install_browser_thread, daemon=True).start()
            return self._json({"ok": True, "browser": BROWSER})

        m = re.match(r"^/api/login/(tju|zhs)/cancel$", path)
        if m:
            if not self._guard(q):
                return
            import browser_login
            browser_login.cancel(m.group(1))
            return self._json({'ok':True})

        m = re.match(r"^/api/login/(tju|zhs)/start$", path)
        if m:
            if not self._guard(q):
                return
            site = m.group(1)
            try:
                import browser_login
                sess = browser_login.start(site)
            except Exception as e:
                store.append_event("error", "起扫码会话失败 %s" % e, site=site)
                return self._err(500, "起扫码会话失败：%s" % str(e)[:160])
            with SESS_LOCK:
                SESSIONS[site] = sess
            st = sess.public()
            st["ok"] = True
            st["qrUrl"] = "/api/login/%s/qr/%s.png?t=%s" % (site, sess.sid, TOKEN)
            return self._json(st)

        if path == "/api/open-browser":
            if not self._guard(q):
                return
            body = self._body()
            return self._err(501, "该功能已并入扫码流程")

        return self._err(404, "没有这个接口")

    # ---------- 页面与静态 ----------

    def _page(self):
        global PAGE_HTML
        import ui_assets
        if PAGE_HTML is None:
            try:
                PAGE_HTML = ui_assets.load_html()
            except (OSError,ValueError):
                return self._err(503,'页面资源暂不可用，请重新打开程序以自动恢复；已保存的数据不受影响。')
        html = inject(PAGE_HTML, "local", TOKEN)
        return self._send(200, html, MIME[".html"])

    def _static(self, path):
        # 只放开 data/ 下的快照与 sync/state 里的二维码图片
        cand = None
        if path.startswith("/files/"):
            rel = urllib.parse.unquote(path[len("/files/"):])
            cand = (store.DATA / rel).resolve()
            if store.DATA.resolve() not in cand.parents and cand != store.DATA.resolve():
                return self._err(403, "路径越界")
        if not cand or not cand.exists() or not cand.is_file():
            return self._err(404, "没有这个文件")
        ctype = MIME.get(cand.suffix.lower()) or mimetypes.guess_type(str(cand))[0] or "application/octet-stream"
        return self._send(200, cand.read_bytes(), ctype)

    def _snapshot_file(self, path):
        name = Path(urllib.parse.unquote(path)).name
        f = store.DATA / name
        if not f.exists():
            return self._err(404, "快照不存在")
        return self._send(200, f.read_bytes(), MIME[".html"])

    # ---------- 业务 ----------

    def _start_job(self, sites, which):
        jid = secrets.token_hex(8)
        job = Job(jid)
        with JOB_LOCK:
            JOBS[jid] = job
        # 只留最近 20 个任务
        if len(JOBS) > 20:
            for k in sorted(JOBS, key=lambda x: JOBS[x].at)[:-20]:
                JOBS.pop(k, None)

        def run():
            import pipeline
            job.step = "正在连接站点"
            try:
                def progress(value):
                    job.step = value.get('step', job.step)
                    job.progress = value
                res = pipeline.pull(sites=sites, which=which, progress=progress)
                job.result = res
                job.state = "done" if res.get("ok") and not res.get('partial') else "partial"
                job.step = "完成"
            except Exception as e:
                job.state = "error"
                job.error = str(e)[:200]
                job.step = "失败"
                store.append_event("error", "pull job 异常 %s" % e)

        threading.Thread(target=run, daemon=True).start()
        return self._json({"ok": True, "jobId": jid, "state": "running"})

    def _make_snapshot(self):
        """把当前数据烤进 HTML，token 与凭证一律不进快照。"""
        import ui_assets
        html = ui_assets.load_html()
        data = store.read_latest()
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        extra = (
            '<script id="wb-snapshot" type="application/json">%s</script>'
            '<meta name="wb-snapshot-at" content="%s">'
        ) % (payload, data.get("generatedAt") or store.now_iso())
        out_html = inject(html, "snapshot", "", extra)
        name = "snapshot-%s.html" % stamp
        f = store.DATA / name
        f.write_text(out_html, encoding="utf-8")
        store.append_event("snapshot", name)
        return self._json({"ok": True, "file": name, "url": "/snapshot/" + name,
                           "counts": store.counts()})


def _lan_ip():
    """取本机在局域网里的地址，给手机访问用。取不到就返回空。"""
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        try:
            s.close()
        except Exception:
            pass


class LocalServer(ThreadingHTTPServer):
    """Windows 上 SO_REUSEADDR 会让「端口已被占用」也绑定成功，
    结果是请求被别的程序接走、我们的服务静默失效（用户看到的是别人的 404）。
    这里关掉它，让冲突直接抛错，好换端口。"""
    allow_reuse_address = False
    daemon_threads = True


def _bind_free(start, bind):
    """从 start 开始找一个能真正绑上的端口，返回 (server, port)。"""
    last = None
    for p in range(start, start + 20):
        try:
            return LocalServer((bind, p), Handler), p
        except OSError as e:
            last = e
            continue
    raise RuntimeError("端口 %d–%d 都被占用了：%s" % (start, start + 19, last))


def build_allowed(port):
    ALLOWED_HOSTS.update({"127.0.0.1:%d" % port, "localhost:%d" % port})
    ORIGIN_OK.update({"http://127.0.0.1:%d" % port, "http://localhost:%d" % port})
    ip = _lan_ip()
    if ip:
        # 手机用 http://<局域网IP>:8787 访问，Host/Origin 也要认得
        ALLOWED_HOSTS.add("%s:%d" % (ip, port))
        ORIGIN_OK.add("http://%s:%d" % (ip, port))
    return ip


AUTO_HOURS = ("08:00", "20:00")
KEEPALIVE_EVERY = 6 * 3600      # 每 6 小时敲一次，让登录态走滑动续期

# 浏览器内核状态：分发给别人时对方机器上通常没有 chromium，
# 扫码登录会直接失败，所以这里做检测 + 一键安装。
BROWSER = {"state": "unknown", "note": "", "at": 0,
           "bundled":bool(store.BROWSER_BUNDLED),
           "source": "内置内核" if store.BROWSER_BUNDLED else "自带内核" if BROWSER_DIR else "系统缓存"}
if store.BROWSER_BUNDLED:
    BROWSER.update(state='ready',at=time.time())

BACKUP_ERROR = ''


def _backup_loop():
    global BACKUP_ERROR
    import data_safety
    while not STOP_REQUESTED.is_set():
        try:
            data_safety.backup(store.ROOT,reason='定时自动备份')
            BACKUP_ERROR = ''
        except OSError as error:
            BACKUP_ERROR = str(error)[:180]
        STOP_REQUESTED.wait(3600)


def check_browser():
    """能不能真的拉起来一个 chromium。返回 (ok, note)。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = store.launch_chromium(p, headless=True, args=["--disable-gpu", "--no-sandbox"])
            b.close()
        return True, ""
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            return False, "missing"
        return False, msg[:200]


def _browser_probe_async():
    if store.BROWSER_BUNDLED:
        return
    BROWSER["state"] = "checking"
    ok, note = check_browser()
    BROWSER.update(state="ready" if ok else ("missing" if note == "missing" else "failed"),
                   note="" if ok else note, at=time.time())
    if not ok:
        store.append_event("browser", "浏览器内核不可用：%s" % (note or "")[:120])


def _install_browser_thread():
    import subprocess
    BROWSER.update(state="installing", note="正在下载浏览器内核（约 150MB，2–5 分钟）", at=time.time())
    store.append_event("browser", "开始安装 chromium")
    try:
        from playwright._impl._driver import compute_driver_executable
        node, cli = compute_driver_executable()
        try:
            from playwright._impl._driver import get_driver_env
            env = get_driver_env()
        except Exception:
            env = None
        proc = subprocess.run([str(node), str(cli), "install", "chromium"],
                              env=env, capture_output=True, text=True, timeout=1800)
        if proc.returncode == 0:
            ok, note = check_browser()
            BROWSER.update(state="ready" if ok else "failed",
                           note="" if ok else note, at=time.time())
            store.append_event("browser", "安装完成" if ok else "安装后仍不可用")
        else:
            tail = (proc.stderr or proc.stdout or "")[-300:]
            BROWSER.update(state="failed", note=tail, at=time.time())
            store.append_event("error", "安装 chromium 失败 %s" % tail[:120])
    except Exception as e:
        BROWSER.update(state="failed", note=str(e)[:300], at=time.time())
        store.append_event("error", "安装 chromium 异常 %s" % e)


def keepalive_all():
    """轻量保活：不拉数据，只让两站的会话各走一次校验。

    这是「登录态能撑很久」的关键 —— 多数 SSO 是滑动过期，
    定期访问会刷新 cookie 的 expires；放着不动反而先过期。
    """
    for site in ("tju", "zhs"):
        if not store.has_state(site):
            continue
        try:
            if site == "tju":
                import cas_qr
                ok, note = cas_qr.keepalive()
            else:
                import zhs_qr
                ok, note = zhs_qr.keepalive_light()
            store.append_event("keepalive", ("成功 " if ok else "失败 ") + (note or ""), site=site)
        except Exception as e:
            store.append_event("error", "保活异常 %s" % e, site=site)


def auto_loop():
    """定时任务：早八晚八各拉一次；其余时间每 6 小时保活一次。"""
    from datetime import datetime
    import pipeline
    store.append_event("auto", "定时任务已启动：拉取 08:00 / 20:00，保活每 6 小时")
    last_alive = 0
    while True:
        try:
            now = datetime.now()
            slot = now.strftime("%H:%M")
            if slot in AUTO_HOURS:
                meta = store.read_json(store.STATE / "meta.json", {}) or {}
                tag = now.strftime("%Y-%m-%d ") + slot
                if (meta.get("auto") or {}).get("lastRun") != tag:
                    sites = tuple(s for s in ("tju", "zhs") if store.has_state(s))
                    if sites:
                        pipeline.pull(sites=sites)
                        store.append_event("auto", "已刷新 %s" % ",".join(sites))
                    meta.setdefault("auto", {})["lastRun"] = tag
                    store.write_json(store.STATE / "meta.json", meta)
            if time.time() - last_alive > KEEPALIVE_EVERY:
                keepalive_all()
                last_alive = time.time()
        except Exception as e:
            store.append_event("error", "定时任务异常 %s" % e)
        time.sleep(40)


def start_server(port=None, auto=False, lan=False, quiet=False):
    """只做启动、不阻塞，返回 (srv, port)。

    桌面窗口版用这个：窗口跑在主线程，服务跑在后台线程。
    serve() 是它的阻塞包装，行为保持不变。
    """
    global PORT, ACTIVE_SERVER, PAGE_HTML
    import ui_assets
    import data_safety
    data_safety.seed_recovery(store.ROOT)
    PAGE_HTML = ui_assets.load_html()
    STOP_REQUESTED.clear()
    requested = port or PORT
    bind = "0.0.0.0" if lan else "127.0.0.1"
    srv, actual = _bind_free(requested, bind)
    ACTIVE_SERVER = srv
    PORT = actual
    ip = build_allowed(PORT)
    url = "http://127.0.0.1:%d/" % PORT
    # 端口与 token 落盘，方便 CLI 与调试脚本复用（仅本机可读）
    store.write_json(store.STATE / "server.json",
                     {"port": PORT, "token": TOKEN, "startedAt": store.now_iso(),
                      'version':APP_VERSION,'pid':os.getpid(),'executable':sys.executable,
                      'dataDir':str(store.ROOT.resolve())})
    if not quiet:
        print("=" * 56)
        print("  校园课程工作台 · 本地服务已启动")
        if actual != requested:
            print("  提示：端口 %d 被占用，已自动改用 %d" % (requested, actual))
        print("  打开：%s" % url)
        if lan and ip:
            print("  手机 / 平板：http://%s:%d/  （同一 WiFi 即可）" % (ip, PORT))
        else:
            print("  只监听 127.0.0.1（手机要访问请加 --lan）")
        print("  凭证目录：%s（不要外发）" % store.STATE)
        if auto:
            print("  定时刷新：每天 08:00 / 20:00")
        print("=" * 56)
    if auto:
        threading.Thread(target=auto_loop, daemon=True).start()
    if not store.BROWSER_BUNDLED:
        threading.Thread(target=_browser_probe_async, daemon=True).start()
    threading.Thread(target=_backup_loop,daemon=True).start()
    return srv, PORT


def serve(port=None, open_browser=True, auto=False, lan=False):
    srv, actual = start_server(port=port, auto=auto, lan=lan)
    url = "http://127.0.0.1:%d/" % PORT
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    serve()
