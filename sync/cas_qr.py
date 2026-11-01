# -*- coding: utf-8 -*-
"""天津大学统一身份认证 —— 企业微信扫码登录。

不需要浏览器：uuid 由客户端自造，二维码内容就是 CAS 的 appScanLogin 地址，
PC 端每 3 秒轮询 checkQRCodeScan，拿到 redirect 即代表手机端已确认。
整个会话落在一个 http.cookiejar 里，最后序列化成 state/tju.json。
"""

import io
import json
import re
import ssl
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import Cookie, CookieJar

import store

SERVICE = "https://classes.tju.edu.cn/eams/homeExt.action"
CAS = "https://sso.tju.edu.cn/cas"
QR_TTL = 180          # 天大二维码约 3 分钟失效
POLL_INTERVAL = 3

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 学校站点常见证书链不全，验证失败时才降级，并在事件流里留痕
_LAX = ssl.create_default_context()
_LAX.check_hostname = False
_LAX.verify_mode = ssl.CERT_NONE
_lax_noted = False


def _opener(jar):
    ctx = _LAX
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if hasattr(urllib.request, "HTTPSHandler"):
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _get(opener, url, timeout=15, referer=None):
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.getcode(), resp.geturl(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, url, body
    except Exception as e:
        store.append_event("error", "请求失败 %s: %s" % (url, e), site="tju")
        return 0, url, ""


def cookie_jar_from_state(obj):
    jar = CookieJar()
    for c in (obj or {}).get("cookies", []):
        try:
            jar.set_cookie(Cookie(
                version=0, name=c["name"], value=c.get("value", ""),
                port=None, port_specified=False,
                domain=c.get("domain", ""), domain_specified=True,
                domain_initial_dot=str(c.get("domain", "")).startswith("."),
                path=c.get("path", "/"), path_specified=True,
                secure=bool(c.get("secure", False)),
                expires=c.get("expires") or None,
                discard=False,
                comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False,
            ))
        except Exception:
            continue
    return jar


def cookie_jar_to_state(jar):
    out = []
    for c in jar:
        out.append({
            "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
            "expires": c.expires, "secure": bool(c.secure), "httpOnly": bool(c._rest and "HttpOnly" in c._rest),
        })
    return {"cookies": out, "savedAt": store.now_iso()}


def qr_png(text, box=8):
    """二维码 PNG bytes；qrcode 缺失时返回 None，由上层降级为「只显示链接」。"""
    try:
        import qrcode
    except ImportError:
        return None
    img = qrcode.make(text, box_size=box, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class Session(object):
    """一次扫码会话。所有状态都在内存里，超时即作废。"""

    def __init__(self, site="tju"):
        self.site = site
        self.sid = uuid.uuid4().hex[:16]
        self.uuid = uuid.uuid4().hex
        self.state = "qr_ready"
        self.error = ""
        self.created_at = time.time()
        self.scanned_at = None
        self.done_at = None
        self.jar = cookie_jar_from_state(store.load_state("tju"))
        self.opener = _opener(self.jar)
        self.qr_text = "%s/appScanLogin?uuid=%s&service=%s" % (
            CAS, self.uuid, urllib.parse.quote(SERVICE, safe=""))
        self.png = None

    def expired(self):
        return time.time() - self.created_at > QR_TTL

    def remain(self):
        return max(0, int(QR_TTL - (time.time() - self.created_at)))

    def public(self):
        return {
            "sid": self.sid,
            "state": self.state,
            "error": self.error,
            "remain": self.remain(),
            "hasImage": bool(self.png),
        }


def start():
    """注册 uuid（服务端要认识它才会有二维码状态）并生成 PNG。"""
    s = Session("tju")
    code, url, body = _get(s.opener, s.qr_text, referer=CAS + "/login")
    store.append_event("qr_start", "appScanLogin %s -> HTTP %s" % (s.uuid[:8], code), site="tju")
    s.png = qr_png(s.qr_text)
    s.state = "qr_ready"
    return s


def _extract_redirect(body):
    """从轮询响应里挖出可跳转的地址。响应格式各版本不一，宁可多试几种。"""
    body = (body or "").strip()
    if not body:
        return None
    try:
        obj = json.loads(body)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        for k in ("redirect_url", "redirectUrl", "url", "data", "location", "result"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
            if isinstance(v, dict):
                for kk in ("redirect_url", "redirectUrl", "url"):
                    vv = v.get(kk)
                    if isinstance(vv, str) and vv.startswith("http"):
                        return vv
    m = re.search(r'https?://[^\s"\'<>\\]+', body)
    if m:
        return m.group(0).replace("\\/", "/")
    return None


def _logged_in(opener):
    """去教务首页看一眼：没被弹回 CAS 就算登录成功。"""
    code, final_url, body = _get(opener, SERVICE, timeout=20, referer=CAS + "/login")
    if code == 0:
        return False, "教务站不可达"
    if "cas/login" in final_url or "cas/login" in body[:2000]:
        return False, "仍停在统一身份认证"
    if "统一身份认证" in body[:3000] and "password" in body[:3000]:
        return False, "登录页未通过"
    has_name = bool(re.search(r"(你好|欢迎|退出|注销)", body))
    return True, "已进入教务首页" + ("" if has_name else "（未识别到姓名，仍按成功处理）")


def poll(sess):
    """推进一次状态机。返回 public()，调用方每 3 秒来一次。"""
    if sess.state in ("authorized", "done", "expired", "error"):
        return sess.public()
    if sess.expired():
        sess.state = "expired"
        sess.error = "二维码已过期，请重新生成"
        return sess.public()

    code, url, body = _get(
        sess.opener,
        "%s/checkQRCodeScan?uuid=%s" % (CAS, sess.uuid),
        timeout=10, referer=CAS + "/login")

    redirect = _extract_redirect(body)
    if redirect and "appScanLogin" not in redirect:
        sess.state = "scanned"
        sess.scanned_at = time.time()
        store.append_event("qr_scanned", "拿到回跳地址", site="tju")
        # 带着同一个 cookie jar 跟进，TGC 会落进来
        _get(sess.opener, redirect, timeout=20)
        ok, note = _logged_in(sess.opener)
        if ok:
            sess.state = "done"
            sess.done_at = time.time()
            store.save_state("tju", cookie_jar_to_state(sess.jar))
            store.append_event("login_ok", note, site="tju")
        else:
            sess.state = "authorized"
            sess.error = note
            store.append_event("login_pending", note, site="tju")
        return sess.public()

    # 有些版本 result:true 就代表手机端已确认，直接试一次首页
    if body.strip() in ("true", "1") or '"result":true' in body.replace(" ", ""):
        if sess.scanned_at:
            ok, note = _logged_in(sess.opener)
            if ok:
                sess.state = "done"
                store.save_state("tju", cookie_jar_to_state(sess.jar))
                store.append_event("login_ok", note, site="tju")
                return sess.public()
        sess.state = "scanned"
        sess.scanned_at = sess.scanned_at or time.time()
    return sess.public()


def wait(sess, on_tick=None, timeout=QR_TTL):
    """阻塞轮询，供 CLI（对话里发二维码）用。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = poll(sess)
        if on_tick:
            on_tick(st)
        if st["state"] in ("done", "expired", "error"):
            return st
        time.sleep(POLL_INTERVAL)
    sess.state = "expired"
    sess.error = "等待超时"
    return sess.public()


def ensure_login():
    """有登录态就验证一下，失效则返回 False 让上层重新扫码。"""
    obj = store.load_state("tju")
    if not obj:
        return False, "未登录"
    jar = cookie_jar_from_state(obj)
    ok, note = _logged_in(_opener(jar))
    return ok, note


def keepalive():
    """访问一次教务首页，让 CAS 的 TGC 走一遍校验。

    多数学校的 CAS 是滑动过期（每次校验续期），所以「定期访问」比
    「把 cookie 存起来不动」能显著延长寿命。响应里若下发了新 cookie，
    这里会连同新的 expires 一起回写，这才是续期真正生效的地方。
    """
    obj = store.load_state("tju")
    if not obj:
        return False, "未登录"
    jar = cookie_jar_from_state(obj)
    ok, note = _logged_in(_opener(jar))
    if not ok:
        return False, note
    payload = cookie_jar_to_state(jar)
    payload["_savedAt"] = obj.get("_savedAt")     # 不伪造成「刚登录」
    payload["_keepaliveAt"] = store.now_iso()
    store.write_json(store.state_path("tju"), payload)
    meta = store.read_json(store.STATE / "meta.json", {}) or {}
    meta.setdefault("tju", {})["keepalive"] = {"at": store.now_iso(), "ok": True}
    store.write_json(store.STATE / "meta.json", meta)
    return True, note


# ---------------- 抓页面 ----------------

PAGES = {
    # 天大的教务是 beangle/struts2 框架（不是正方）。
    # 这两个地址是从登录后的首页菜单里读出来的：
    #   我的课表 -> /eams/courseTableForStd.action
    #   我的考试 -> /eams/stdExamTable.action
    # 之前写成 !courseTable/!examTable 多带了方法后缀，抓到的是空壳页。
    "courses": "https://classes.tju.edu.cn/eams/courseTableForStd.action",
    "exams": "https://classes.tju.edu.cn/eams/stdExamTable.action",
}


def _pw_cookies(obj):
    """把我们的 cookie 格式转成 Playwright 的 add_cookies 参数。"""
    out = []
    for c in (obj or {}).get("cookies", []):
        ck = {"name": c.get("name", ""), "value": c.get("value", ""),
              "domain": c.get("domain", ""), "path": c.get("path") or "/",
              "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly")),
              "sameSite": "Lax"}
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            ck["expires"] = exp
        if ck["name"] and ck["domain"]:
            out.append(ck)
    return out


# 每页「真的渲染好了」的判据。
# 课表：课程格子是 td.infoTitle；考试：表格行数（考试日期可能全是「时间未安排」，
# 但只要行出来了就算抓到了 —— 那是学校还没排考，不是抓取失败）。
READY_CHECK = {
    "courses": "() => document.querySelectorAll('td.infoTitle').length",
    "exams": "() => document.querySelectorAll('table tr').length",
}
READY_MIN = {"courses": 1, "exams": 10}


def _wait_ready(page, name, rounds=5, wait_ms=5000):
    """等到页面内容真的渲染出来，没出来就重载。

    踩过的坑：CAS 首次用 ticket 换完登录态后，第一个业务页的 AJAX 常常
    不触发，页面停在一个空壳上（课表 12KB、0 个 td），等 24 秒也不会变。
    reload 一次就好 —— 实测第 3 次能稳定拿到 82KB / 19 个格子。
    所以这里不能只等，要判 + 重载。
    """
    js = READY_CHECK.get(name)
    if not js:
        page.wait_for_timeout(7000)
        return page.content(), True
    need = READY_MIN.get(name, 1)
    for n in range(1, rounds + 1):
        if n > 1:
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
        for _ in range(max(1, wait_ms // 1000)):
            page.wait_for_timeout(1000)
            try:
                if page.evaluate(js) >= need:
                    if n > 1:
                        store.append_event("fetch", "%s 第 %d 次加载才渲染出来" % (name, n),
                                           site="tju")
                    return page.content(), True
            except Exception:
                pass
        store.append_event("fetch", "%s 第 %d 次加载仍空白，重载" % (name, n), site="tju")
    return page.content(), False


def fetch_pages(which=None):
    """渲染两个业务页并落 raw/，返回 {name: html}。

    必须用浏览器：课表内容在 #contentDiv 里由 AJAX 填进来，
    直接 GET 只会拿到一个空壳（实测 5KB，tr 只有 1 行）。
    登录态从 state/tju.json 注入，不重新走登录。
    """
    obj = store.load_state("tju")
    if not obj:
        raise RuntimeError("need_login")
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        browser = store.launch_chromium(p, headless=True,
                                        args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900},
                                  locale="zh-CN")
        try:
            ctx.add_cookies(_pw_cookies(obj))
        except Exception as e:
            store.append_event("error", "cookie 注入失败 %s" % e, site="tju")
        page = ctx.new_page()
        for name, url in PAGES.items():
            if which and name not in which:
                continue
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if "cas/login" in page.url:
                    raise RuntimeError("need_login")
                html, ok = _wait_ready(page, name)
                if "cas/login" in page.url:
                    raise RuntimeError("need_login")
                store.save_raw("tju", name, html)
                out[name] = html
                store.append_event("fetch", "%s %d 字节（%s）"
                                   % (name, len(html), "已渲染" if ok else "仍空白"), site="tju")
            except RuntimeError:
                raise
            except Exception as e:
                store.append_event("error", "抓取 %s 失败 %s" % (name, e), site="tju")
        browser.close()
    return out
