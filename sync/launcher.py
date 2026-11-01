# -*- coding: utf-8 -*-
"""桌面模式：起本地服务，再用 Edge 的 --app 打开一个「没有浏览器界面」的窗口。

为什么用 Edge --app 而不是嵌入式控件：
    pywebview / WebView2 与 PyQt5 / QtWebEngine 在这台机器上都能开窗口，
    但内部渲染层始终不工作（一个黑屏一个白屏，evaluate_js 永不返回），
    而服务端日志证明页面本身是正常的 200。--app 用的是完整的 Edge
    进程，不走嵌入，因此绕开了这个问题。

窗口长什么样：
    只有标题栏和网页内容，没有地址栏、标签页、书签栏。
    任务栏是一个独立条目，图标取页面 favicon。
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from app_info import APP_VERSION
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import store  # noqa: E402

WINDOW_W, WINDOW_H = 1320, 880
MIN_ALIVE_SECONDS = 4.0     # 主进程活不过这么久 = 启动失败或进程交接


def _log(msg):
    """窗口版没有控制台，一律落盘到 desktop.log。"""
    try:
        with open(str(store.ROOT / "desktop.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), str(msg)[:900]))
    except Exception:
        pass


# ---------------------------------------------------------------- 控制台

def ensure_console():
    """console=False 打包后没有控制台，但命令行用法需要能输出。

    优先附到调用方的 cmd 窗口，附不上才新建一个。

    注意一个 Windows 的固有限制：GUI 子系统的程序，输出写不进
    调用方重定向出来的管道（`exe status > out.txt` 是抓不到的）。
    所以同时把输出 tee 一份到 cli.log，管道场景下看那个文件。
    """
    if os.environ.get('WB_NO_CONSOLE') == '1':
        protect_std_streams()
        return
    if os.name != "nt":
        return
    try:
        k32 = ctypes.windll.kernel32
        if not k32.GetConsoleWindow():
            if not k32.AttachConsole(ctypes.c_uint(-1).value):
                k32.AllocConsole()
    except Exception:
        pass

    # 无论附没附上控制台，都 tee 一份到文件，保证输出不会彻底丢掉
    try:
        tee = _Tee(sys.stdout, str(store.ROOT / "cli.log"))
        sys.stdout = tee
        sys.stderr = _Tee(sys.stderr, str(store.ROOT / "cli.log"))
    except Exception:
        pass


class _Tee(object):
    """同时往控制台和文件写。控制台句柄可能无效，所以每步都容错。"""

    def __init__(self, stream, path):
        self.stream = stream
        self.path = path
        self.console = None
        try:
            self.console = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        except Exception:
            self.console = None
        if self.stream is None or not hasattr(self.stream, "write"):
            self.stream = self.console

    def write(self, s):
        for t in (self.stream, self.console):
            try:
                if t:
                    t.write(s)
                    t.flush()
            except Exception:
                pass
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        for t in (self.stream, self.console):
            try:
                if t:
                    t.flush()
            except Exception:
                pass


def protect_std_streams():
    """console=False 时 sys.stdout 是 None，任何 print 都会抛异常。

    桌面模式不打印，但依赖库可能打印，所以先接到日志文件兜住。
    """
    if sys.stdout is not None:
        try:
            sys.stdout.write("")
            return
        except Exception:
            pass
    try:
        f = open(str(store.ROOT / "desktop.log"), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f
    except Exception:
        pass


# ---------------------------------------------------------------- 找浏览器

def _from_registry():
    try:
        import winreg
    except Exception:
        return None
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
    ]
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as k:
                p = winreg.QueryValue(k, None)
                if p and Path(p).exists():
                    return Path(p)
        except Exception:
            continue
    return None


def find_browser():
    """按可靠性排序找 Edge，找不到退 Chrome。返回 (路径, 名称) 或 (None, None)。"""
    if os.environ.get('WB_BROWSER') == 'embedded':
        bundled = store.prepare_browser()
        if bundled:
            for exe in Path(bundled).glob('chromium-*/chrome-win64/chrome.exe'):
                return exe,'Chromium'
    hit = _from_registry()
    if hit:
        return hit, "Edge"

    env = os.environ
    candidates = [
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Edge"),
        (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "Edge"),
        (str(Path(env.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe"), "Edge"),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Chrome"),
        (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "Chrome"),
        (str(Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"), "Chrome"),
    ]
    for p, name in candidates:
        if p and Path(p).exists():
            return Path(p), name

    for exe, name in (("msedge", "Edge"), ("chrome", "Chrome")):
        w = shutil.which(exe)
        if w:
            return Path(w), name
    bundled = store.prepare_browser()
    if bundled:
        for exe in Path(bundled).glob("chromium-*/chrome-win64/chrome.exe"):
            return exe, "Chromium"
    return None, None


def window_geometry():
    """算一个合适的窗口尺寸和居中位置，返回 (w, h, x, y)。

    注意 DPI：不声明 DPI 感知的话，在 150%/200% 缩放的屏幕上
    GetSystemMetrics 返回的是缩放后的逻辑值，算出来的位置会把窗口
    挤到角落或推出屏幕外。所以先 SetProcessDPIAware 再取值。
    """
    lx, ly, aw, ah = 0, 0, WINDOW_W, WINDOW_H
    try:
        import ctypes.wintypes
        u = ctypes.windll.user32
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass
        rect = ctypes.wintypes.RECT()
        if u.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):   # SPI_GETWORKAREA
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 200 and h > 200:
                lx, ly, aw, ah = rect.left, rect.top, w, h
    except Exception:
        pass

    # 按可用区域的比例取，再夹到合理区间：
    # 太死板地写 1320×880，在 2560×1600 这种大屏上会显得空荡荡像个小弹窗。
    w = min(max(int(aw * 0.62), 980), 1560)
    h = min(max(int(ah * 0.72), 640), 1040)
    w = min(w, aw - 60)
    h = min(h, ah - 60)
    x = lx + max(0, (aw - w) // 2)
    y = ly + max(0, (ah - h) // 2 - 24)
    return w, h, x, y


# ---------------------------------------------------------------- 起窗口

def _seed_profile(profile_dir):
    """往独立 profile 里预写偏好，压掉首启动的登录/同步弹窗。

    光靠命令行开关压不干净：Edge 会在 profile 里记「用户还没决定要不要登录」，
    下次启动继续弹。这里直接把那些状态标成「已拒绝」，弹窗就不再出现。
    只在文件不存在时写，不覆盖用户之后的选择。
    """
    prefs = Path(profile_dir) / "Default" / "Preferences"
    try:
        prefs.parent.mkdir(parents=True, exist_ok=True)
        if prefs.exists():
            return
        payload = {
            "profile": {"exit_type": "Normal", "exited_cleanly": True},
            "signin": {
                "allowed": False,
                "allowed_on_next_startup": False,
            },
            "sync": {"requested": False},
            "edge_sync": {"sync_disabled": True},
            "browser": {"has_seen_welcome_page": True},
            "credentials_enable_service": False,
            "autofill": {"profile_enabled": False},
        }
        prefs.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        _log("预写 profile 偏好失败：%s" % e)


def open_window(port, browser=None, name=None):
    """用 --app 打开独立窗口。返回 Popen（主进程），失败返回 None。"""
    url = "http://127.0.0.1:%d/" % port
    exe, label = (browser, name) if browser else find_browser()
    if not exe:
        _log("没找到 Edge 或 Chrome")
        return None

    profile = store.ROOT / ('chromium-profile' if label == 'Chromium' else 'edge-profile')
    try:
        profile.mkdir(parents=True, exist_ok=True)
        if label == "Edge":
            _seed_profile(profile)
    except Exception:
        profile = None

    w, h, x, y = window_geometry()
    args = [
        str(exe),
        "--app=" + url,
        # 只让本机直连，其余流量照走系统代理 —— 页面本身完全自包含，
        # 但如果以后点开外链，代理还得在，所以不能一刀切 --no-proxy-server
        "--proxy-bypass-list=127.0.0.1;localhost",
        "--window-size=%d,%d" % (w, h),
        "--window-position=%d,%d" % (x, y),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",      # 关窗就退出，别留后台常驻
        "--disk-cache-size=104857600",
        # 独立 profile 会触发「在所有设备上同步浏览数据」的登录弹窗，
        # 那个弹窗会盖住页面，必须关掉。下面几项一起把这类提示压住。
        "--disable-sync",
        "--disable-features=msEdgeIdentityFeature,msImplicitSignin,"
        "msEdgeSyncPromo,SigninInterception,EdgeCollections,msEdgeSplitScreen",
        "--no-service-autorun",
    ]
    if profile:
        args.append("--user-data-dir=" + str(profile))

    try:
        _log("启动 %s：%s" % (label, " ".join(args[1:4])))
        return subprocess.Popen(args, close_fds=True)
    except Exception as e:
        _log("启动 %s 失败：%s" % (label, e))
        return None


def wait_ready(port, timeout=20.0):
    """等本地服务真的能出页面，再去开窗口，否则窗口会显示连接被拒。"""
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def existing_instance():
    """已有服务在跑吗？活着就复用它，避免两个实例抢同一份数据。"""
    import urllib.request
    obj = store.read_json(store.STATE / "server.json", None) or store.read_json(store.STATE/'previous-server.json',None) or {}
    port, tok = obj.get("port"), obj.get("token")
    if not port:
        return None
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/status" % port,
                                     headers={"X-WB-Token": tok or ""})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                status = json.load(r)
                if not status.get('ok') or 'sites' not in status:
                    return None
        try:
            req = urllib.request.Request('http://127.0.0.1:%d/api/health' % port,
                                         headers={'X-WB-Token':tok or ''})
            with urllib.request.urlopen(req,timeout=2) as r:
                health = json.load(r)
            if health.get('version') == APP_VERSION and health.get('pageReady') and Path(health.get('dataDir','')).resolve() == store.ROOT.resolve():
                return port
        except Exception:
            pass
        _log('检测到旧版或资源失效的后台，备份后切换当前版本')
        import data_safety
        try:
            data_safety.backup(store.ROOT,reason='替换旧后台前',force=True)
        except OSError as error:
            raise RuntimeError('暂时无法创建数据备份，未停止旧程序：'+str(error)) from error
        if not retire_instance(port,tok):
            raise RuntimeError('旧后台暂时无法停止，请退出旧版后重新打开。数据已备份。')
    except Exception:
        if isinstance(sys.exc_info()[1], RuntimeError):
            raise
    return None


def retire_instance(port, token):
    """仅接管已用本目录 token 验证过的工作台后台；不结束其他程序。"""
    import urllib.request
    try:
        req = urllib.request.Request('http://127.0.0.1:%d/api/shutdown' % port,
                                     headers={'X-WB-Token':token or ''},data=b'{}')
        urllib.request.urlopen(req,timeout=3).close()
    except Exception:
        if os.name != 'nt':
            return False
        # 旧版没有退出接口。端口来自本目录 server.json，并已成功验证其 token。
        command = 'Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ConvertTo-Json' % int(port)
        result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                                capture_output=True,text=True,timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            pids = json.loads(result.stdout)
        except (ValueError,TypeError):
            return False
        for pid in pids if isinstance(pids,list) else [pids]:
            if not isinstance(pid,int) or pid == os.getpid():
                continue
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.argtypes = [ctypes.c_uint,ctypes.c_int,ctypes.c_uint]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p,ctypes.c_uint,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint)]
            kernel.TerminateProcess.argtypes = [ctypes.c_void_p,ctypes.c_uint]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1001,False,pid)
            if not handle:
                continue
            try:
                name, length = ctypes.create_unicode_buffer(32768), ctypes.c_uint(32768)
                if kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(length)):
                    image = Path(name.value).name
                    if image.startswith('课程工作台') and image.lower().endswith('.exe'):
                        kernel.TerminateProcess(handle,0)
            finally:
                kernel.CloseHandle(handle)
    import socket
    end = time.time()+8
    while time.time()<end:
        with socket.socket() as sock:
            sock.settimeout(.3)
            if sock.connect_ex(('127.0.0.1',int(port))) != 0:
                return True
        time.sleep(.2)
    return False


# ---------------------------------------------------------------- 主流程

def run(port=None):
    """桌面模式入口。返回退出码。"""
    protect_std_streams()
    _log("=== 桌面模式启动 ===")

    import server

    port_now = existing_instance()
    if port_now:
        _log("复用已在运行的服务 %s，只补开一个窗口" % port_now)
        if open_window(port_now) is None:
            webbrowser.open("http://127.0.0.1:%d/" % port_now)
        return 0

    srv, actual = server.start_server(port=port, auto=False, lan=False, quiet=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _log("服务已启动，端口 %s" % actual)

    if not wait_ready(actual):
        _log("服务 20 秒内没就绪，仍然尝试开窗口")

    url = "http://127.0.0.1:%d/" % actual
    proc = open_window(actual)

    if proc is None:
        _log("没有可用的独立窗口，退回系统默认浏览器")
        try:
            webbrowser.open(url)
            print("已用默认浏览器打开：%s" % url)
            print("关闭这个终端窗口即可停止服务。")
        except Exception:
            pass
        try:
            server.STOP_REQUESTED.wait()
        except KeyboardInterrupt:
            pass
        _stop(srv)
        return 0

    started = time.time()
    try:
        while proc.poll() is None and not server.STOP_REQUESTED.wait(.3):
            pass
        rc = proc.poll() or 0
    except KeyboardInterrupt:
        rc = 0
    alive = time.time() - started
    # Edge 关窗后返回码常是 1（不是 0），这是正常的，不代表出错。
    _log("浏览器主进程退出，rc=%s，存活 %.1f 秒" % (rc, alive))

    if alive < MIN_ALIVE_SECONDS:
        # 主进程秒退：要么启动失败，要么把请求交接给了已存在的同 profile 实例。
        # 这时如果顺手关服务，正在显示的窗口会立刻变成"连接已断开"，
        # 所以宁可让服务继续挂着（下次双击会复用），也不误关。
        _log("主进程过早退出，判定为异常/交接，保留服务不关闭")
        try:
            server.STOP_REQUESTED.wait()
        except KeyboardInterrupt:
            pass
        _stop(srv)
        return 0

    _stop(srv)
    return 0


def _stop(srv):
    try:
        srv.shutdown()
        srv.server_close()
        _log("服务已停止")
        import data_safety
        data_safety.backup(store.ROOT,reason='退出前备份',force=True)
    except Exception as e:
        _log("关服务异常：%s" % e)
