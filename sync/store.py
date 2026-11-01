# -*- coding: utf-8 -*-
"""状态、快照与数据读写。

三个目录职责分离，避免凭证混进业务数据：
  state/  登录态（等同凭证，绝不外发）
  raw/    每次拉取的原始 HTML，供解析失败时人工校准
  data/   结构化结果（courses/exams/ddl/progress）
"""

import json
import os
import sys
import time
import durable_json
import task_sync
import threading
from functools import wraps
from datetime import datetime
from pathlib import Path

DATA_LOCK = threading.RLock()


def locked(function):
    @wraps(function)
    def call(*args, **kwargs):
        with DATA_LOCK:
            return function(*args, **kwargs)
    return call

_here = Path(__file__).resolve().parent


def _pick_root():
    """决定 sync-data 放哪儿。

    打包后 exe 可能被放到桌面，若仍按「exe 旁边」的规则，
    桌面会凭空多出一个 sync-data 文件夹，用户第二天就懵了。
    所以三级解析：

      1. 环境变量 WB_DATA            显式指定（调试、多份数据切换用）
      2. exe 旁边已有 sync-data/    绿色便携：U 盘拷走连数据一起走
      3. 都没有                      %LOCALAPPDATA%\\课程工作台\\
                                     Windows 应用数据的标准位置
    """
    import runtime_config
    return runtime_config.configured_root(Path(sys.executable).parent)


if getattr(sys, "frozen", False):
    # PyInstaller 单文件：代码跑在临时解压目录，数据必须落到可写且固定的地方
    ROOT = _pick_root()
    EXEDIR = Path(sys.executable).parent   # exe 所在目录，自带内核放这儿
    WEB = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    # 源码模式：数据统一放工作区的 sync-data/，和打包后目录名保持一致，
    # 免得在两处各留一份、搞不清哪份是活的。
    EXEDIR = _here
    WEB = _here.parent
    ROOT = Path(os.environ['WB_DATA']).expanduser().resolve() if os.environ.get('WB_DATA') else _here.parent / "sync-data"
    if not os.environ.get('WB_DATA') and not ROOT.is_dir() and (_here / "state").is_dir():
        ROOT = _here          # 老布局兜底

STATE = ROOT / "state"
RAW = ROOT / "raw"
DATA = ROOT / "data"

for _d in (STATE, RAW, DATA):
    _d.mkdir(parents=True, exist_ok=True)

SITE_NAME = {"tju": "天津大学教务", "zhs": "智慧树"}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def read_json(path, default=None):
    return durable_json.read(path, default)


def write_json(path, obj):
    """原子写：先写 .tmp 再替换，避免进程中断留下半截 JSON。"""
    durable_json.write(path, obj)


def append_event(kind, detail, site=None):
    """事件流水，用于排查「为什么没拉到」。"""
    rec = {"at": now_iso(), "kind": kind, "site": site, "detail": detail}
    with (STATE / "events.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def recent_events(limit=20):
    p = STATE / "events.log"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


# ---------------- 登录态 ----------------

def state_path(site):
    return STATE / ("%s.json" % site)


def has_state(site):
    p = state_path(site)
    if not p.exists():
        return False
    obj = read_json(p, {})
    return bool(obj)


def save_state(site, payload):
    payload = dict(payload)
    payload["_savedAt"] = now_iso()
    write_json(state_path(site), payload)
    meta = read_json(STATE / "meta.json", {}) or {}
    meta.setdefault(site, {})["lastLogin"] = payload["_savedAt"]
    write_json(STATE / "meta.json", meta)


def load_state(site):
    return read_json(state_path(site), None)


def clear_state(site):
    p = state_path(site)
    if p.exists():
        p.unlink()
    meta = read_json(STATE / "meta.json", {}) or {}
    meta.setdefault(site, {})["lastLogin"] = None
    write_json(STATE / "meta.json", meta)


def mark_pull(site, ok, count=0, note=""):
    meta = read_json(STATE / "meta.json", {}) or {}
    slot = meta.setdefault(site, {})
    slot["lastPull"] = now_iso()
    slot["lastPullOk"] = bool(ok)
    slot["lastCount"] = count
    slot["lastNote"] = note
    write_json(STATE / "meta.json", meta)


def meta():
    return read_json(STATE / "meta.json", {}) or {}


# ---------------- 登录态还能撑多久 ----------------

def _latest_expiry(obj):
    """登录态里最晚的 cookie 过期时间戳（秒）。

    -1 / 缺失表示会话 cookie —— 只要进程活着、jar 不丢就一直有效
    （这一点比浏览器强：关浏览器不会丢）。
    """
    best = None
    for c in (obj or {}).get("cookies") or []:
        e = c.get("expires")
        if isinstance(e, (int, float)) and e > 0:
            best = e if best is None else max(best, e)
    return best


def session_info(site):
    """给前端与 status 用：这枚登录态还剩多少天。"""
    obj = load_state(site)
    if not obj or not obj.get("cookies"):
        return {"loggedIn": False, "expiresAt": None, "daysLeft": None,
                "expired": False, "note": "未登录"}
    exp = _latest_expiry(obj)
    if exp is None:
        return {"loggedIn": True, "expiresAt": None, "daysLeft": None,
                "expired": False, "note": "无固定过期，服务端不踢就一直有效"}
    left = exp - time.time()
    return {
        "loggedIn": True,
        "expiresAt": datetime.fromtimestamp(exp).isoformat(timespec="seconds"),
        "daysLeft": round(left / 86400.0, 1),
        "expired": left <= 0,
        "note": "",
    }


# ---------------- 原始页面 ----------------

def save_raw(site, page, html):
    p = RAW / ("%s-%s-%s.html" % (site, page, stamp()))
    p.write_text(html, encoding="utf-8")
    # 只留最近 20 份，避免无限增长
    olds = sorted(RAW.glob("%s-%s-*.html" % (site, page)))
    for f in olds[:-20]:
        try:
            f.unlink()
        except OSError:
            pass
    return p


def load_latest_raw(site, page):
    files = sorted(RAW.glob("%s-%s-*.html" % (site, page)))
    if not files:
        return None
    return files[-1].read_text(encoding="utf-8", errors="replace")


# ---------------- 汇总数据 ----------------

LATEST = DATA / "latest.json"
EMPTY = {"courses": [], "exams": [], "ddl": [], "progress": [], "unparsed": []}


def read_latest():
    obj = read_json(LATEST, None) or {}
    out = {key: [] for key in EMPTY}
    out.update({k: v for k, v in obj.items() if k in EMPTY or k in ("generatedAt", "unparsed", "syncStats")})
    for k in ("courses", "exams", "ddl", "progress", "unparsed"):
        out.setdefault(k, [])
    import attachments
    for item in out["ddl"]:
        item["attachments"] = attachments.normalize(item.get("attachments") or [])
    return out


@locked
def merge_latest(site, parsed, replace=False):
    """Incremental upsert; a reminder/list omission never deletes saved work."""
    cur = read_latest()
    workspace = read_json(DATA / 'workspace.json', {}) or {}
    deleted = workspace.get('deletedTasks') or []
    # Older versions saved the checkbox only in workspace.json, not latest.json.
    cur['ddl'], _ = task_sync.reconcile(cur['ddl'], workspace.get('ddl') or [], local=True, deleted=deleted)
    added = {k: 0 for k in ("courses", "exams", "ddl", "progress")}
    for key in ("courses", "exams", "ddl", "progress"):
        incoming = [dict(item, src=site) for item in parsed.get(key, [])]
        added[key] = len(incoming)
        if key == 'ddl':
            cur[key], stats = task_sync.reconcile(cur[key], incoming, deleted=deleted)
            cur.setdefault('syncStats', {})[site] = stats
            continue
        positions = {(x.get('src'), x.get('id')): i for i, x in enumerate(cur[key])}
        for item in parsed.get(key, []):
            item = dict(item)
            item["src"] = site
            pair = (site, item.get('id'))
            if pair in positions:
                cur[key][positions[pair]].update(item)
            else:
                positions[pair] = len(cur[key])
                cur[key].append(item)
    cur['unparsed'] = [x for x in cur['unparsed'] if x.get('src') != site]
    for item in parsed.get("unparsed", []):
        item = dict(item)
        item["src"] = site
        cur["unparsed"].append(item)
    cur["generatedAt"] = now_iso()
    write_json(LATEST, cur)
    return added


@locked
def save_workspace(value):
    """Keep local decisions in both durable snapshots; preserve newer source facts."""
    previous = read_json(DATA / 'workspace.json', {}) or {}
    deleted = {x['id']: x for x in previous.get('deletedTasks', []) if x.get('id')}
    deleted.update({x['id']: x for x in value.get('deletedTasks', []) if isinstance(x, dict) and x.get('id')})
    value = dict(value)
    if deleted or 'deletedTasks' in value:
        value['deletedTasks'] = list(deleted.values())
    latest = read_latest()
    if int(value.get('resetAt') or 0) > int(previous.get('resetAt') or 0):
        import data_safety
        data_safety.backup(ROOT, reason='清空工作台前', force=True)
        previous = {}
        latest = {key: [] for key in EMPTY}
    existing, _ = task_sync.reconcile(latest['ddl'], previous.get('ddl') or [], local=True)
    tasks, _ = task_sync.reconcile(existing, value['ddl'], local=True, deleted=list(deleted.values()))
    value['ddl'] = tasks
    write_json(DATA / 'workspace.json', value)
    latest['ddl'] = [x for x in tasks if not str(x.get('id', '')).startswith('demo_')]
    write_json(LATEST, latest)
    return value


def counts():
    cur = read_latest()
    return {k: len(cur.get(k, [])) for k in ("courses", "exams", "ddl", "progress", "unparsed")}


@locked
def enrich_ddl(details):
    """按平台ID关联详情；旧任务只在课程、标题、截止时间唯一匹配时补关联。

    更新要求正文、附件和来源标识；答案单独入库，任务ID和本地状态保持不变。
    """
    cur = read_latest()
    by_key = {}
    by_ref = {}
    for hw, d in (details or {}).items():
        key = ((d.get('courseName') or '').strip(), (d.get('title') or '').strip(), d.get('due') or '')
        by_key.setdefault(key, []).append(d)
        if d.get('homeworkId') and d.get('courseId'):
            by_ref[(str(d['courseId']), str(d['homeworkId']))] = d
    hit, miss = 0, []
    for item in cur.get("ddl", []):
        if item.get("src") != "zhs":
            continue
        ref = item.get('sourceRef') or {}
        d = by_ref.get((str(ref.get('courseId')), str(ref.get('homeworkId')))) if ref.get('provider') == 'zhs' else None
        key = ((item.get('courseName') or '').strip(), (item.get('title') or '').strip(), item.get('due') or '')
        candidates = by_key.get(key, [])
        if d is None and len(candidates) == 1:
            d = candidates[0]
        if not d:
            continue
        if ref.get('accountKey') and d.get('accountKey') and ref['accountKey'] != d['accountKey']:
            continue
        req = d.get("requirement") or ""
        item['content'] = req[:20000]
        if d.get('homeworkId') and d.get('courseId'):
            item['sourceRef'] = dict(ref, provider='zhs', kind='homework',
                                     courseId=str(d['courseId']), homeworkId=str(d['homeworkId']))
            if d.get('accountKey'):
                item['sourceRef']['accountKey'] = d['accountKey']
        if 'submissionStatus' in d:
            item.update(submissionStatus=d['submissionStatus'], submissionObservedAt=now_iso(),
                        submissionEvidence=d.get('submissionEvidence', ''))
            item.update(task_sync.normalize(item))

        # 附件：homeworkDirGet2 给的 fileList，下载到 data/att/
        # 前端 attachments 只要 {name, url}；多给的 local/size/ext 用于显示「已存本地」
        files = d.get("files") or []
        if files:
            try:
                import zhs_detail
                files = zhs_detail.download_files(files)
            except Exception as e:
                append_event("error", "附件下载失败 %s" % str(e)[:60], site="zhs")
        atts = []
        for f in files:
            a = {"name": f.get("name") or "附件", "url": f.get("url") or ""}
            if f.get("local"):
                a["local"] = f["local"]
            if f.get("size"):
                a["size"] = f["size"]
            if f.get("ext"):
                a["ext"] = f["ext"]
            if f.get("downloaded"):
                a["downloaded"] = True
            atts.append(a)
        item["attachments"] = atts + [a for a in item.get("attachments", []) if a.get("manual")]
        item["detailAt"] = now_iso()
        item["paperId"] = d.get("paperId") or 0
        item["enrich"] = "zhs_detail"
        hit += 1
    for key, candidates in by_key.items():
        for d in candidates:
            if not any(((x.get('courseName') or '').strip(), (x.get('title') or '').strip(), x.get('due') or '') == key
                       and x.get('src') == 'zhs' for x in cur.get('ddl', [])):
                miss.append(d.get('title'))
    write_json(LATEST, cur)
    return {"updated": hit, "unmatched": miss}


def html_path():
    """工作台页面文件。打包时统一叫 app.html（ASCII 名更稳，中文名在
    别人的系统上解压偶发乱码），源码运行时仍是中文名。"""
    if not getattr(sys, 'frozen', False) and (_here / 'app.html').is_file():
        return _here / 'app.html'
    roots = (WEB,) if getattr(sys,'frozen',False) else (_here,WEB)
    for root in roots:
        for name in ("app.html", "校园课程工作台.html"):
            p = root / name
            if p.exists():
                return p
    return None


_PREPARED_BROWSER = None
BROWSER_BUNDLED = False


def prepare_browser():
    """先用单文件 EXE 解压出的内核，再兼容旧版旁置内核。"""
    global _PREPARED_BROWSER, BROWSER_BUNDLED
    if _PREPARED_BROWSER and any(Path(_PREPARED_BROWSER).glob('chromium-*/chrome-win64/chrome.exe')):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _PREPARED_BROWSER
        return _PREPARED_BROWSER
    _PREPARED_BROWSER = None
    for cand in (WEB / "browser", EXEDIR / "browser"):
        if any(cand.glob("chromium-*/chrome-win64/chrome.exe")):
            BROWSER_BUNDLED = cand == WEB / 'browser'
            if getattr(sys,'frozen',False) and BROWSER_BUNDLED:
                import runtime_assets
                manifest = read_json(WEB / 'browser-manifest.json', {})
                cand = runtime_assets.browser(cand, ROOT / 'runtime/browser',manifest)
                runtime_assets.allow_browser_sandbox(cand)
                driver_manifest = read_json(WEB / 'driver-manifest.json',{})
                runtime_assets.playwright_driver(WEB,ROOT/'runtime/playwright',driver_manifest)
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cand)
            _PREPARED_BROWSER = str(cand)
            return _PREPARED_BROWSER
    return None


def launch_chromium(p, **kw):
    """启动 chromium，统一走完整版内核。

    分发包只内置了一份完整 chromium（智慧树要有头模式，不能只带 headless_shell），
    而新版 Playwright 在 headless=True 时会默认去找 headless_shell，
    只带完整版就会报 "Executable doesn't exist"。
    所以统一指定 channel="chromium" 用完整版；万一没有再退回默认。
    """
    try:
        return p.chromium.launch(channel="chromium", **kw)
    except Exception:
        return p.chromium.launch(**kw)


def status():
    m = meta()
    out = {"generatedAt": read_latest().get("generatedAt"), "sites": {}, "counts": counts()}
    for site, label in SITE_NAME.items():
        st = m.get(site, {})
        sess = session_info(site)
        out["sites"][site] = {
            "label": label,
            "loggedIn": sess["loggedIn"],
            "lastLogin": st.get("lastLogin"),
            "lastPull": st.get("lastPull"),
            "lastPullOk": st.get("lastPullOk"),
            "lastCount": st.get("lastCount", 0),
            "lastNote": st.get("lastNote", ""),
            "session": sess,
            "keepalive": (st.get("keepalive") or {}).get("at"),
        }
    return out


def age_seconds(site):
    m = meta().get(site, {})
    if not m.get("lastPull"):
        return None
    try:
        return int(time.time() - datetime.fromisoformat(m["lastPull"]).timestamp())
    except Exception:
        return None
