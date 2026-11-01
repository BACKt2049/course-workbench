# -*- coding: utf-8 -*-
"""智慧树作业详情抓取。

接口（逆向所得，2026-10 验证）：
    列表    GET  https://hike-examstu.zhihuishu.com/zhsathome/homework/findImportantReminderList
    作业头  GET  https://hiexam-server.zhihuishu.com/zhsathome/homeworkUserDir/Info
                 ?homeworkId={id}&courseId={courseId}&uuid={CASLOGC.uuid}
    试卷题  GET  .../homeworkUserPaper/getDetailList?homeworkId={id}&uuid={uuid}
    试卷头  GET  .../homeworkUserPaper/getDoWorkTitle?homeworkId={id}&uuid={uuid}

两类作业：
    buildMode / paperId == 0  → 目录型：content 就是全部要求，学生交文件或文字
    paperId > 0               → 试卷型：content 往往只有一句话，真正的题目在
                                getDetailList 的 groupDtoList[].questionDtoList[]

返回字段：
    title / content(HTML) / endTime(ms) / homeworkScore / status
    homeworkFileList  老师给的附件（多数为 null —— 平台上的作业附件很少，资料多放课程文件夹）
    userAnswerFileList 自己交的文件

注意：content 是富文本 HTML，带内联样式，入库前要洗成纯文本并保留链接。
附件是带鉴权的私链，下载必须带 cookie。
"""

import html as _html
import json
import re
import urllib.parse

import store

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE = "https://hiexam-server.zhihuishu.com/zhsathome"
LIST_URL = "https://hike-examstu.zhihuishu.com/zhsathome/homework/findImportantReminderList"


def _uuid():
    """CASLOGC cookie 里带 uuid，所有 zhsathome 接口都要它。"""
    obj = store.load_state("zhs") or {}
    for c in obj.get("cookies", []):
        if c.get("name") == "CASLOGC":
            try:
                v = urllib.parse.unquote(c.get("value") or "")
                return json.loads(v).get("uuid", "")
            except Exception:
                continue
    return ""


_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"</(p|div|li|tr|h[1-6])>|<br\s*/?>", re.I)
_LINK = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)


def html_to_text(content):
    """富文本 → 可读纯文本，链接保留成「文字（URL）」。"""
    if not content:
        return ""
    s = content
    # 链接先抽出来，免得被标签清洗掉
    links = []

    def keep_link(m):
        href, text = m.group(1), _TAG.sub("", m.group(2)).strip()
        if not href.startswith("http"):
            return text
        if not text:
            text = href
        links.append((text, href))
        return "\n@@LINK%d@@\n" % (len(links) - 1)

    s = _LINK.sub(keep_link, s)
    s = _BR.sub("\n", s)
    s = _TAG.sub("", s)
    s = _html.unescape(s)
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    lines = []
    for line in s.split("\n"):
        line = line.strip()
        m = re.fullmatch(r"@@LINK(\d+)@@", line)
        if m:
            text, href = links[int(m.group(1))]
            lines.append("%s（%s）" % (text, href))
        elif line:
            lines.append(line)
    return "\n".join(lines).strip()


def _ms_to_iso(ms):
    if not ms:
        return ""
    import time
    try:
        n = int(ms) // 1000
        return time.strftime("%Y-%m-%dT%H:%M", time.localtime(n))
    except Exception:
        return ""


def fetch_all(client=None):
    """补充当前待办详情；允许同步任务注入共享请求客户端。"""
    from zhs_client import ZhsClient
    if client is None:
        with ZhsClient() as active:
            return _fetch_using(active)
    return _fetch_using(client)


def _fetch_using(client):
    from zhs_client import PlatformError
    out, errors = {}, []
    reminders = client.get(LIST_URL).get("rt")
    if not isinstance(reminders, list):
        raise PlatformError("parse", "作业提醒列表结构异常")
    reminders = list(reminders)
    known = {(str(x.get('courseId')), str(x.get('homeworkId') or x.get('id'))) for x in reminders}
    for task in store.read_latest().get('ddl', []):
        ref = task.get('sourceRef') or {}
        if ref.get('provider') != 'zhs' or ref.get('kind') == 'exam':
            continue
        if ref.get('accountKey') and ref['accountKey'] != client.account_key:
            continue
        pair = (str(ref.get('courseId') or ''), str(ref.get('homeworkId') or ''))
        if all(pair) and pair not in known:
            reminders.append(dict(task, courseId=pair[0], homeworkId=pair[1], type=1))
            known.add(pair)
    for item in reminders:
        if str(item.get("type", "1")) != "1":
            continue
        hw = item.get("homeworkId") or item.get("id")
        cid = item.get("courseId")
        if not hw or not cid:
            continue
        params = {"homeworkId": hw, "courseId": cid, "uuid": client.uuid}
        try:
            rt = client.get("/homeworkUserDir/Info", params).get("rt")
            if not isinstance(rt, dict):
                raise PlatformError("parse", "作业详情结构异常")
        except PlatformError as error:
            if error.kind == "need_login":
                raise
            errors.append({"homeworkId": hw, "error": str(error)})
            continue
        rec = {
            "homeworkId": str(hw), "courseId": str(cid),
            "title": (rt.get("title") or item.get("title") or "").strip(),
            "courseName": item.get("courseName") or "",
            "contentHtml": rt.get("content") or "",
            "requirement": html_to_text(rt.get("content") or ""),
            "due": _ms_to_iso(rt.get("endTime") or item.get("endTime")),
            "paperId": rt.get("paperId"), "files": [],
            "questions": [], "status": rt.get("status"),
        }
        import task_sync
        rec.update(task_sync.submission(rt))
        rec['accountKey'] = client.account_key
        short = {"homeworkId": hw, "uuid": client.uuid}
        try:
            full = client.get("/homeworkDir/homeworkDirGet2", short).get("rt") or {}
            rec["files"] = _norm_files(full.get("fileList"))
            if len(full.get("content") or "") > len(rec["contentHtml"]):
                rec["contentHtml"] = full["content"]
                rec["requirement"] = html_to_text(full["content"])
        except PlatformError as error:
            errors.append({"homeworkId": hw, "error": str(error)})
        if rec["paperId"] is not None and str(rec["paperId"]) != "0":
            try:
                rec["questions"] = _norm_questions(client.paper(hw).get("rt"))
            except (PlatformError, ValueError) as error:
                if not isinstance(error, PlatformError) or error.kind != "closed":
                    errors.append({"homeworkId": hw, "error": str(error)})
        out[str(hw)] = rec
    return {"items": out, "errors": errors}


def _norm_questions(rt):
    """兼容原详情调用方；正确标记统一由纯解析模块处理。"""
    from answers.normalize import normalize_questions
    return normalize_questions(rt)


def _norm_files(fl):
    """附件列表做成统一结构。

    homeworkDirGet2 的 fileList 实测字段：
      {id, homeworkId, fileId, fileType:"pdf", uploadFrom, fileTitle,
       fileUrl, fileSize, suffixType, viewUrl, suffixTypeName:"pdf"}
    fileUrl 指向 file.zhihuishu.com —— 公开 CDN，带不带 cookie 都能下。
    """
    if not fl:
        return []
    out = []
    for f in fl:
        if not isinstance(f, dict):
            continue
        url = (f.get("fileUrl") or f.get("url") or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        ext = (f.get("suffixTypeName") or f.get("fileType")
               or f.get("fileSuffix") or "").lstrip(".").lower()
        out.append({
            "name": f.get("fileTitle") or f.get("fileName") or f.get("name") or "附件",
            "url": url,
            "viewUrl": f.get("viewUrl") or "",
            "size": f.get("fileSize") or 0,
            "ext": ext,
            "id": f.get("fileId") or f.get("id"),
        })
    return out


def _safe_name(s):
    """去掉 Windows 文件名非法字符，保留中文。"""
    bad = '\\/:*?"<>|\r\n\t'
    s = "".join(("_" if c in bad else c) for c in (s or "")).strip(" .")
    return s[:120] or "file"


def download_files(files, subdir="att"):
    """把附件下载到 sync-data/data/<subdir>/，按 URL 去重。

    返回新列表（每项多两个字段：local=相对 data 的路径，downloaded=bool）。
    下载失败不改原 url，前端仍可点开原站。
    """
    import hashlib
    if not files:
        return []
    root = store.DATA / subdir
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        item = dict(f)
        url = item.get("url") or ""
        if not url.startswith("http"):
            item["downloaded"] = False
            out.append(item)
            continue
        h = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        name = "%s_%s" % (h, _safe_name(item.get("name")))
        p = root / name
        item["local"] = "%s/%s" % (subdir, name)
        if subdir == "att":
            import attachments
            local = attachments.resolve_local(item["local"])
            if local is None:
                # 用户已经删除，后续同步不再重新下载。
                continue
            if local != item["local"]:
                p = attachments.path_for(local[4:])
                item.update(local=local, name=p.name, ext=p.suffix.lstrip("."))
        if p.exists() and p.stat().st_size > 0:
            item["downloaded"] = True
            item["size"] = p.stat().st_size
            out.append(item)
            continue
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://hiexam.zhihuishu.com/"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) < 100:
                raise RuntimeError("响应过短 %d 字节" % len(data))
            p.write_bytes(data)
            item["downloaded"] = True
            item["size"] = len(data)
            store.append_event("file", "下载 %s（%d KB）" % (name, len(data) // 1024), site="zhs")
        except Exception as e:
            item["downloaded"] = False
            item["error"] = str(e)[:100]
            store.append_event("error", "附件下载失败 %s：%s" % (name, str(e)[:60]), site="zhs")
        out.append(item)
    return out
