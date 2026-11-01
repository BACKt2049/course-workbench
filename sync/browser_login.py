# -*- coding: utf-8 -*-
"""两个站点的扫码登录：统一由受控 Chromium 承载页面自己的扫码逻辑。

为什么不再自己造 uuid ——
天大的二维码是 CAS 页面的 login-qrcode.js 在初始化时向服务端注册的，
uuid 是标准 UUID（带横线）且与本次 JSESSIONID 绑定。外部自造一串 hex 拼出的
appScanLogin 地址虽然能生成图、也能被扫，但服务端没有这条 uuid 的授权上下文，
轮询永远拿不到结果 —— 实测扫了没反应。所以直接复用页面：
点开 Scan Code 页签、取页面自己生成的二维码、等页面自己跳转。

智慧树使用知到 App 的扫码页签，不使用微信登录二维码。
"""

import base64
import os
import threading
import time
import uuid
import urllib.parse

import store

SERVICE = "https://classes.tju.edu.cn/eams/homeExt.action"
CAS_LOGIN = "https://sso.tju.edu.cn/cas/login?service=" + urllib.parse.quote(SERVICE, safe="")
ZHS_LOGIN = "https://passport.zhihuishu.com/login"
QR_TTL = 180
START_TTL = 90
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HINTS = {
    "tju": ("用融合门户 App 扫码", "请用手机上的「融合门户」App 扫描。这是天大统一身份认证的扫码页，"
                                  "企业微信 / 微信扫都不认。"),
    "zhs": ("用知到 App 扫码", "请打开「知到」App 的“扫一扫”扫描，不要使用微信。扫完在手机上确认；若要求滑块，请在弹出的窗口里完成。"),
}


class Session(object):
    """线程安全状态壳；浏览器活在 _run 所在的线程里。"""

    def __init__(self, site):
        self.site = site
        self.sid = uuid.uuid4().hex[:16]
        self.state = "starting"
        self.error = ""
        self.note = ""
        self.png = None
        self.created_at = time.time()
        self.ready_at = None
        self.timings = {}
        self.cancelled = threading.Event()
        self._lock = threading.Lock()

    def set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snap(self):
        with self._lock:
            return {
                "sid": self.sid,
                "site": self.site,
                "state": self.state,
                "error": self.error,
                "note": self.note,
                "hasImage": bool(self.png),
                "remain": max(0, int(QR_TTL - (time.time() - (self.ready_at or time.time())))),
                "timings": dict(self.timings),
                "hint": HINTS[self.site][1],
                "title": HINTS[self.site][0],
            }

    def expired(self):
        return self.cancelled.is_set() or time.time() - (self.ready_at or self.created_at) > (QR_TTL if self.ready_at else START_TTL)

    def public(self):
        s = self.snap()
        if s["state"] not in ("done", "expired", "error") and self.expired():
            self.set(state="expired", error="二维码已过期，请重新生成")
            s = self.snap()
        return s


def start(site):
    with SESSIONS_LOCK:
        previous = SESSIONS.get(site)
        if previous and previous.public()['state'] not in ('done', 'expired', 'error'):
            return previous
        s = Session(site)
        SESSIONS[site] = s
        t = threading.Thread(target=_run, args=(s,), daemon=True)
        t.start()
        return s


def cancel(site):
    with SESSIONS_LOCK:
        session = SESSIONS.get(site)
        if session:
            session.cancelled.set()
            session.set(state='expired', png=None, error='已取消扫码')


def _wait_qr(page, selectors):
    """Wait for visible rendered QR media, rather than sleeping a fixed interval."""
    page.wait_for_function('''selectors => [...document.querySelectorAll(selectors)].some(e => {
      const r=e.getBoundingClientRect();
      if(r.width<80 || r.height<80 || !e.getClientRects().length || getComputedStyle(e).visibility==='hidden') return false;
      if(e.tagName==='IMG') return e.complete && e.naturalWidth>=80;
      if(e.tagName==='CANVAS') return e.width>=80 && e.height>=80;
      return e.tagName==='TABLE' && e.rows.length>12;
    })''', arg=selectors, timeout=20000)


# ---------------- 天大 ----------------

def _tju_grab(page):
    """点开 Scan Code 页签，取页面生成的二维码（base64 或 canvas）。"""
    clicked = False
    for sel in ["#qrcode_login", "text=Scan Code", ".block-tab-item >> text=Scan Code"]:
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=4000)
                clicked = True
                break
        except Exception:
            continue
    store.append_event("qr_start", "Scan Code 页签: %s" % ("已点开" if clicked else "未找到"), site="tju")
    _wait_qr(page, "#qrcode img, #qrcode canvas, canvas, img[src^='data:image']")

    for sel in ["img[src^='data:image/png;base64']", "img[src^='data:image']", "canvas", "#qrcode img"]:
        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            if sel.startswith("img[src^='data"):
                src = loc.get_attribute("src") or ""
                if "," in src:
                    return base64.b64decode(src.split(",", 1)[1]), "页面二维码 " + sel
            return loc.screenshot(), "页面二维码(截图) " + sel
        except Exception:
            continue
    return None, "none"


def _tju_done(ctx, page):
    if "/cas/login" not in page.url:
        return True
    return any(c.get("name") == "CASTGC" for c in ctx.cookies())


# ---------------- 智慧树 ----------------

def _zhs_grab(page):
    """智慧树登录页是 Vue 应用，加载慢且不稳定：
    先等它渲染出 tabs，再点「扫码」页签，然后用「可见近正方形容器」定位二维码，
    最后兜底截整个登录卡片 —— 二维码是 jquery.qrcode 画的，img/canvas 选择器都靠不住。
    """
    page.wait_for_function("""() => {
                if (document.readyState === 'loading') return false;
                if (document.querySelectorAll('.el-tabs__item').length > 0) return true;
                return /扫码|知到|机构登录/.test(document.body.innerText || '');
            }""", timeout=30000)
    store.append_event("qr_start", "智慧树页面就绪", site="zhs")

    clicked = page.evaluate("""() => {
      const tab = [...document.querySelectorAll('.el-tabs__item')]
        .find(e => (e.innerText || '').trim() === '扫码');
      if (tab) { tab.click(); return 'el-tab'; }
      const c = [...document.querySelectorAll('a,div,span,li,button')]
        .filter(e => (e.innerText || '').trim() === '扫码');
      if (c.length) { c[c.length - 1].click(); return 'text'; }
      return 'none';
    }""")
    store.append_event("qr_start", "扫码页签: %s" % clicked, site="zhs")
    _wait_qr(page, '.qrcode-box img, .qrcode-box canvas, .qrcode-box table, .qrcode img, img.qrcode, .qr-code img, .el-tabs__content canvas, .el-tabs__content table')

    # 1) 实测的容器：.qrcode-box / .qrcode-box img（191×191）
    for sel in (".qrcode-box img", ".qrcode-box", ".qrcode img", "img.qrcode", ".qr-code img"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                if sel.endswith("img"):
                    src = loc.get_attribute("src") or ""
                    if src.startswith("data:image"):
                        return base64.b64decode(src.split(",", 1)[1]), "二维码 " + sel
                return loc.screenshot(timeout=8000), "二维码 " + sel
        except Exception:
            continue

    try:
        handle = page.evaluate_handle("""() => {
          const scope = document.querySelector('.el-tabs__content') || document.body;
          const big = e => e.offsetWidth > 80 && e.offsetHeight > 80;
          // 1) jquery.qrcode 画的 table（格子多、行数在 12–80 之间）
          const t = [...scope.querySelectorAll('table')].filter(e => {
            const rows = e.rows ? e.rows.length : 0;
            return big(e) && rows > 12 && rows < 80;
          }).sort((a, b) => b.offsetWidth - a.offsetWidth)[0];
          if (t) return t;
          // 2) canvas
          const cv = [...scope.querySelectorAll('canvas')].filter(big)
            .sort((a, b) => b.offsetWidth - a.offsetWidth)[0];
          if (cv) return cv;
          // 3) 顺着「知到 / 扫一扫」这句提示往上找它所在的容器
          const hint = [...scope.querySelectorAll('div,span,p')]
            .find(e => /知到|扫一扫|APP/.test(e.innerText || '') && (e.innerText || '').length < 40);
          if (hint) {
            let box = hint.parentElement;
            for (let i = 0; i < 4 && box; i++) {
              const w = box.offsetWidth;
              if (w >= 100 && w <= 420) return box;
              box = box.parentElement;
            }
          }
          // 4) 近正方形兜底
          return [...scope.querySelectorAll('div')].filter(e => {
            const w = e.offsetWidth, h = e.offsetHeight;
            return w > 80 && h > 80 && Math.abs(w - h) < 40
              && e.querySelectorAll('*').length < 3000;
          }).sort((a, b) => a.offsetWidth - b.offsetWidth)[0] || null;
        }""")
        el = handle.as_element() if handle else None
        if el:
            tag = el.evaluate("e => e.tagName.toLowerCase() + ':' + e.offsetWidth + 'x' + e.offsetHeight")
            try:
                return el.screenshot(timeout=8000), "二维码 %s" % tag
            except Exception as e:
                store.append_event("qr_start", "二维码元素截图失败 %s" % str(e)[:80], site="zhs")
    except Exception:
        pass

    for sel in ("#login_center_app", ".login_center_app_overflow", ".site-root-wrap"):
        try:
            loc = page.locator(sel).first
            if loc.count():
                return loc.screenshot(timeout=8000), "登录卡片 " + sel
        except Exception:
            continue
    return None, "none"


def _zhs_done(page):
    u = page.url or ""
    if "login.zhihuishu.com" in u or "passport.zhihuishu.com" in u:
        return False
    return "zhihuishu.com" in u


def _verify(page, site):
    """最终判据：业务页能不能打开。被弹回登录页就是没登进去。

    光看 URL 变化不够 —— SPA 扫码成功后可能原地不跳转，
    反过来也可能只是页面刷新。所以拿「目标业务页是否可达」来一锤定音。
    """
    target = ("https://classes.tju.edu.cn/eams/homeExt.action" if site == "tju"
              else "https://onlineweb.zhihuishu.com/onlinewebForH5/studying")
    try:
        page.goto(target, wait_until="commit" if site == "zhs" else "domcontentloaded",
                  timeout=30000)
        page.wait_for_timeout(2000)
        u = page.url or ""
        if site == "tju":
            return "cas/login" not in u
        return "login.zhihuishu.com" not in u and "passport" not in u
    except Exception:
        return False


# ---------------- 主流程 ----------------

def _run(sess):
    started = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        sess.set(state="error", error="playwright 未安装：%s" % e)
        return

    site = sess.site
    url = CAS_LOGIN if site == "tju" else ZHS_LOGIN
    try:
        with sync_playwright() as p:
            try:
                # 天大静默跑；智慧树 headless 下渲染很不稳定（还容易撞限流），
                # 直接开有头窗口，用户看得见也扫得踏实。设 WB_ZHS_HEADLESS=1 可切回静默。
                headless = True if site == "tju" else (os.environ.get("WB_ZHS_HEADLESS", "0") == "1")
                browser = store.launch_chromium(
                    p, headless=headless,
                    args=["--disable-blink-features=AutomationControlled"])
                sess.timings['browserMs'] = round((time.monotonic() - started) * 1000)
            except Exception as e:
                sess.set(state="error", error="内置浏览器暂时无法启动，请重新打开程序以自动修复：%s" % str(e)[:140])
                store.append_event("error", "chromium 启动失败 %s" % e, site=site)
                return
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1180, "height": 860},
                                      locale="zh-CN", timezone_id="Asia/Shanghai")
            if site == "zhs":
                # 智慧树的字体加载会一直挂住 document.fonts.ready，
                # 导致 screenshot() 永远等下去（实测 headful 直接超时）。
                # 二维码是 table 画的，跟字体无关，直接掐掉字体请求。
                for pat in ("**/*.woff", "**/*.woff2", "**/*.ttf", "**/*.otf", "**/*.eot"):
                    try:
                        ctx.route(pat, lambda route: route.abort())
                    except Exception:
                        pass
            page = ctx.new_page()
            sess.set(state="opening")
            try:
                # 智慧树首屏资源很重，等 domcontentloaded 容易被拖死，只等导航提交
                page.goto(url, wait_until="commit" if site == "zhs" else "domcontentloaded",
                          timeout=45000)
            except Exception as e:
                sess.set(state="error", error="打不开登录页：%s" % str(e)[:140])
                store.append_event("error", "打开登录页失败 %s" % str(e)[:100], site=site)
                browser.close()
                return
            sess.timings['pageMs'] = round((time.monotonic() - started) * 1000)

            png, how = (_tju_grab(page) if site == "tju" else _zhs_grab(page))
            sess.set(png=png, state="qr_ready" if png else "waiting_manual",
                     ready_at=time.time(), note=how if png else "没截到二维码，请在浏览器窗口里扫")
            sess.timings['qrReadyMs'] = round((time.monotonic() - started) * 1000)
            store.append_event('qr_timing', dict(sess.timings), site=site)
            store.append_event("qr_start", "二维码来源 %s" % how, site=site)

            deadline = time.time() + QR_TTL
            verified = False
            seen_move = False
            while time.time() < deadline:
                if sess.cancelled.is_set():
                    browser.close()
                    return
                moved = _tju_done(ctx, page) if site == "tju" else _zhs_done(page)
                if moved:
                    seen_move = True
                    store.append_event("qr_scanned", "页面已离开登录页，验证业务页", site=site)
                    sess.set(state="scanned")
                    if _verify(page, site):
                        verified = True
                        break
                    store.append_event("login_pending", "离开登录页但业务页仍不可达", site=site)
                if sess.expired():
                    break
                page.wait_for_timeout(1500)

            if not verified and seen_move:
                # 见到过跳转却验证失败，超时前再试一次，别让用户白扫
                verified = _verify(page, site)

            if not verified:
                sess.set(state="expired", error="没检测到登录成功，请重新扫码")
                store.append_event("login_fail", "超时未通过业务页验证", site=site)
                browser.close()
                return

            if site == "tju":
                cookies = []
                for c in ctx.cookies():
                    exp = c.get("expires")
                    cookies.append({
                        "name": c.get("name"), "value": c.get("value"),
                        "domain": c.get("domain"), "path": c.get("path"),
                        "expires": exp if isinstance(exp, (int, float)) and exp > 0 else None,
                        "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly")),
                    })
                store.save_state("tju", {"cookies": cookies})
                sess.set(note="登录态已保存（%d 个 cookie）" % len(cookies))
            else:
                state = ctx.storage_state()
                state["_savedAt"] = store.now_iso()
                store.write_json(store.state_path("zhs"), state)
                meta = store.read_json(store.STATE / "meta.json", {}) or {}
                meta.setdefault("zhs", {})["lastLogin"] = state["_savedAt"]
                store.write_json(store.STATE / "meta.json", meta)
                sess.set(note="登录态已保存")

            sess.set(state="done")
            store.append_event("login_ok", sess.note, site=site)
            browser.close()
    except Exception as e:
        sess.set(state="error", error="扫码流程异常：%s" % str(e)[:160])
        store.append_event("error", "扫码异常 %s" % e, site=site)
