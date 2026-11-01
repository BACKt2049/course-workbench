# -*- coding: utf-8 -*-
"""智慧树 —— 微信扫码登录（Playwright 承载）。

为什么必须用浏览器：微信开放平台的 qrconnect 授权结果只认「加载了那个 iframe
的浏览器上下文」，扫码确认后 cookie 落在那个上下文里，纯 HTTP 客户端拿不到。

所以这里起一个受控 Chromium，把二维码截出来交给前端显示，用户扫完我们
轮询地址跳转，最后 storage_state 落盘。整个过程在一个后台线程里跑，
主线程只读状态，避免 sync API 跨线程崩溃。
"""

import os
import threading
import time

import store

LOGIN_URL = "https://passport.zhihuishu.com/login"
HOME = "https://onlineweb.zhihuishu.com/onlinewebForH5/studying"
QR_TTL = 180

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

QR_SELECTORS = [
    'iframe[src*="qrconnect"]',
    'iframe[src*="open.weixin.qq.com"]',
    'iframe[src*="wxLogin"]',
]

INNER_QR = [
    '.qrcode img',
    '#qrcode img',
    'img.qrcode',
    'img[src*="qrcode"]',
    '.login_qrcode img',
    '.wrp_code img',
]


class ZhsSession(object):
    """线程安全的状态壳。真正的浏览器活在 _run() 所在的线程里。"""

    def __init__(self):
        import uuid
        self.sid = uuid.uuid4().hex[:16]
        self.state = "starting"
        self.error = ""
        self.png = None
        self.created_at = time.time()
        self.note = ""
        self._lock = threading.Lock()
        self._thread = None

    def set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snap(self):
        with self._lock:
            return {
                "sid": self.sid,
                "state": self.state,
                "error": self.error,
                "note": self.note,
                "hasImage": bool(self.png),
                "remain": max(0, int(QR_TTL - (time.time() - self.created_at))),
            }

    def expired(self):
        return time.time() - self.created_at > QR_TTL

    def public(self):
        s = self.snap()
        if s["state"] not in ("done", "expired", "error") and self.expired():
            self.set(state="expired", error="二维码已过期，请重新生成")
            s = self.snap()
        return s


def start():
    s = ZhsSession()
    t = threading.Thread(target=_run, args=(s,), daemon=True)
    s._thread = t
    t.start()
    return s


def _grab_qr(page):
    """三级兜底：iframe 内二维码 → iframe 整块 → 登录区截图。"""
    for sel in QR_SELECTORS:
        try:
            el = page.wait_for_selector(sel, timeout=12000)
        except Exception:
            continue
        try:
            frame = el.content_frame()
        except Exception:
            frame = None
        if frame is None:
            continue
        for inner in INNER_QR:
            try:
                loc = frame.locator(inner).first
                if loc.count() and loc.is_visible():
                    return loc.screenshot(), "iframe:" + inner
            except Exception:
                continue
        try:
            return el.screenshot(), "iframe-bounds"
        except Exception:
            pass
    # 智慧树自己渲染的二维码
    for inner in INNER_QR:
        try:
            loc = page.locator(inner).first
            if loc.count() and loc.is_visible():
                return loc.screenshot(), "page:" + inner
        except Exception:
            continue
    # 最后：截登录面板，用户至少能看到可扫的图
    for sel in (".login-box", ".loginWrap", ".content", "body"):
        try:
            loc = page.locator(sel).first
            if loc.count():
                return loc.screenshot(), "fallback:" + sel
        except Exception:
            continue
    return None, "none"


def _run(sess):
    """后台线程：开浏览器 → 截二维码 → 等扫码 → 落 state。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        sess.set(state="error", error="playwright 未安装：%s" % e)
        store.append_event("error", "playwright 缺失", site="zhs")
        return

    headless = os.environ.get("WB_ZHS_HEADLESS", "0") == "1"
    try:
        with sync_playwright() as p:
            try:
                browser = store.launch_chromium(p, headless=headless, args=["--disable-blink-features=AutomationControlled"])
            except Exception as e:
                sess.set(state="error", error="启动浏览器失败（可能没装内核）：%s" % str(e)[:160])
                store.append_event("error", "chromium 启动失败 %s" % e, site="zhs")
                return
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1180, "height": 860},
                                      locale="zh-CN")
            page = ctx.new_page()
            sess.set(state="opening")
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                sess.set(state="error", error="打不开登录页：%s" % str(e)[:140])
                browser.close()
                return
            page.wait_for_timeout(2500)

            # 默认扫码页使用知到 App，与工作台统一登录流程一致。
            import browser_login
            png, how = browser_login._zhs_grab(page)
            sess.set(png=png, state="qr_ready" if png else "waiting_manual",
                     note=how if png else "没截到二维码，请直接在浏览器窗口里扫码")
            store.append_event("qr_start", "二维码来源 %s" % how, site="zhs")

            deadline = time.time() + QR_TTL
            last_url = ""
            while time.time() < deadline:
                try:
                    url = page.url
                except Exception:
                    url = last_url
                if url != last_url:
                    last_url = url
                    if "passport" not in url and ("onlineweb" in url or "zhihuishu.com/study" in url):
                        break
                if sess.expired():
                    sess.set(state="expired", error="二维码已过期，请重新生成")
                    browser.close()
                    return
                page.wait_for_timeout(1500)
            else:
                sess.set(state="expired", error="等待超时，请重新扫码")
                browser.close()
                return

            # 落到业务页，确保 cookie 完整
            try:
                page.goto(HOME, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
            except Exception:
                pass
            state = ctx.storage_state()
            state["_savedAt"] = store.now_iso()
            store.write_json(store.state_path("zhs"), state)
            meta = store.read_json(store.STATE / "meta.json", {}) or {}
            meta.setdefault("zhs", {})["lastLogin"] = state["_savedAt"]
            store.write_json(store.STATE / "meta.json", meta)
            sess.set(state="done", note="登录态已保存")
            store.append_event("login_ok", "智慧树登录态已保存", site="zhs")
            browser.close()
    except Exception as e:
        sess.set(state="error", error="扫码流程异常：%s" % str(e)[:160])
        store.append_event("error", "zhs 扫码异常 %s" % e, site="zhs")


def ensure_login():
    obj = store.load_state("zhs")
    if not obj or not obj.get("cookies"):
        return False, "未登录"
    return True, "有登录态（是否有效以拉取结果为准）"


def keepalive_light():
    """不启浏览器，只拿 cookie 敲一下学习中心。

    智慧树的业务接口要 JS 签名，但「会不会被踢回登录页」用普通 HTTP 就能判断，
    成本比开一个 Chromium 低两个数量级。响应里若刷新了 cookie 会一并回写。
    """
    import urllib.request
    from http.cookiejar import Cookie, CookieJar

    obj = store.load_state("zhs")
    if not obj or not obj.get("cookies"):
        return False, "未登录"
    jar = CookieJar()
    for c in obj["cookies"]:
        try:
            exp = c.get("expires")
            jar.set_cookie(Cookie(
                0, c.get("name", ""), c.get("value", ""), None, False,
                c.get("domain", ""), True, str(c.get("domain", "")).startswith("."),
                c.get("path", "/"), True, bool(c.get("secure", False)),
                exp if isinstance(exp, (int, float)) and exp > 0 else None,
                False, None, None, {}, False))
        except Exception:
            continue
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(
        "https://onlineweb.zhihuishu.com/onlinewebForH5/studying",
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        r = opener.open(req, timeout=20)
        final = r.geturl()
        r.read(2048)
    except Exception as e:
        return False, "请求失败 %s" % str(e)[:60]
    if "passport" in final or "/login" in final:
        return False, "被踢回登录页"
    state = store.load_state("zhs") or {}
    state["cookies"] = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
         "expires": c.expires if isinstance(c.expires, (int, float)) else -1,
         "secure": bool(c.secure), "httpOnly": True}
        for c in jar
    ]
    state["_keepaliveAt"] = store.now_iso()
    store.write_json(store.state_path("zhs"), state)
    meta = store.read_json(store.STATE / "meta.json", {}) or {}
    meta.setdefault("zhs", {})["keepalive"] = {"at": store.now_iso(), "ok": True}
    store.write_json(store.STATE / "meta.json", meta)
    return True, "会话仍有效"


# ---------------- 抓页面 ----------------

PAGES = {
    # 学生端真实入口。注意不是 /onlinewebForH5/studying（那个会被踢回 passport），
    # 也不是 studyh5.zhihuishu.com（直接 403）。这个页面还要等它把课程渲染完，
    # 而且账号若有「开课老师/学生」双身份，默认可能停在教师视图 —— 见 fetch_pages 里的兜底。
    "study": "https://onlineweb.zhihuishu.com/onlinestuh5",
}


def _clean_state_file():
    """兼容旧版保活写入的空cookie字段，统一使用共享客户端的校正规则。"""
    p = store.state_path("zhs")
    obj = store.read_json(p, None)
    if not obj:
        return False
    from zhs_client import normalize_storage_state
    cleaned = normalize_storage_state(obj)
    if cleaned != obj:
        store.write_json(p, cleaned)
        store.append_event("state", "已兼容旧版 storageState 的空cookie字段", site="zhs")
    return True


def fetch_pages(which=None):
    """用已保存的 storage_state 拉起浏览器抓页面，落 raw/。

    同时把页面里所有 JSON 接口响应存一份 —— 智慧树的数据几乎都在 XHR 里，
    只解析 HTML 永远拿不到作业和进度。这些 JSON 不直接进库，只作校准样本。
    """
    from playwright.sync_api import sync_playwright

    sp = store.state_path("zhs")
    if not sp.exists():
        raise RuntimeError("need_login")
    _clean_state_file()
    out = {}
    apis = []
    with sync_playwright() as p:
        browser = store.launch_chromium(p, headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, storage_state=str(sp), locale="zh-CN",
                                  viewport={"width": 1440, "height": 900})
        for pat in ("**/*.woff", "**/*.woff2", "**/*.ttf", "**/*.otf"):
            try:
                ctx.route(pat, lambda route: route.abort())
            except Exception:
                pass
        page = ctx.new_page()

        def on_resp(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "json" not in ct.lower():
                    return
                body = resp.text()
                if body and 20 < len(body) < 400000:
                    apis.append({"url": resp.url, "body": body})
            except Exception:
                pass

        page.on("response", on_resp)

        for name, url in PAGES.items():
            if which and name not in which:
                continue
            try:
                page.goto(url, wait_until="commit", timeout=45000)
                # SPA 骨架先出来，再处理遮挡
                page.wait_for_timeout(6000)

                # ① 有个「绑定手机号」弹窗会挡住内容，先跳过
                for t in ("跳过，暂不绑定", "暂不绑定", "跳过"):
                    try:
                        page.get_by_text(t, exact=False).first.click(timeout=2500)
                        store.append_event("fetch", "已点「%s」关掉遮挡" % t, site="zhs")
                        page.wait_for_timeout(1500)
                        break
                    except Exception:
                        continue

                # ② 双身份账号默认停在「开课老师」视图 —— 点开身份切换再选学生
                try:
                    page.get_by_text("身份切换", exact=True).first.click(timeout=3000)
                    page.wait_for_timeout(3500)
                    store.append_event("fetch", "已点开身份切换", site="zhs")
                except Exception as e:
                    store.append_event("fetch", "身份切换没点到：%s" % str(e)[:50], site="zhs")
                try:
                    page.get_by_text("学生", exact=True).last.click(timeout=3000)
                    page.wait_for_timeout(3000)
                    store.append_event("fetch", "已切到学生视图", site="zhs")
                except Exception:
                    pass

                # ③ 等课程卡片渲染（翻转课/共享课/进度）
                ready = False
                for _ in range(20):
                    try:
                        if page.evaluate("""() => /翻转课|共享课|进度\\s*[:：]/.test(document.body.innerText || '')"""):
                            ready = True
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                if not ready:
                    try:
                        page.click("text=身份切换", timeout=4000)
                        page.wait_for_timeout(4000)
                        ready = bool(page.evaluate(
                            """() => /翻转课|共享课|进度\\s*[:：]/.test(document.body.innerText || '')"""))
                    except Exception:
                        pass
                # ④ 「即将截止」是懒加载模块，滚一下才会请求那个接口
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(4000)
                    page.evaluate("() => window.scrollTo(0, 0)")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

                if "passport" in page.url or "login.zhihuishu.com" in page.url:
                    raise RuntimeError("need_login")
                html = page.content()
                store.save_raw("zhs", name, html)
                out[name] = html
                store.append_event("fetch", "%s %d 字节（渲染%s）"
                                   % (name, len(html), "完成" if ready else "未完成"), site="zhs")
            except RuntimeError:
                raise
            except Exception as e:
                store.append_event("error", "抓取 %s 失败 %s" % (name, e), site="zhs")

        if apis:
            blob = "\n".join(
                '<script>window.__API_%d__ = %s;</script>' % (i, a["body"])
                for i, a in enumerate(apis[:60]))
            store.save_raw("zhs", "api", blob)
            # 作业/考试的真实数据就在这些 JSON 里，交给解析器单独处理
            out["api"] = blob
            store.append_event("fetch", "捕获 %d 个 JSON 接口响应" % len(apis), site="zhs")

        browser.close()
    return out
