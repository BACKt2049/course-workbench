# -*- coding: utf-8 -*-
"""拉取编排：抓页面 → 解析 → 合并入库。

一个原则贯穿到底：任何一步失败都不清空已有数据。
登录失效只回 need_login，让前端弹二维码；解析失败只加 unparsed，
让人看到「哪里没对上」，而不是看到一张空课表。
"""

import store
from parsers import tju_courses, tju_exams, zhs_progress, zhs_work


class NeedLogin(RuntimeError):
    pass


def pull_tju(which=None):
    import cas_qr
    ok, note = cas_qr.ensure_login()
    if not ok:
        raise NeedLogin(note or "登录态失效")
    pages = cas_qr.fetch_pages(which=which)
    parsed = {"courses": [], "exams": [], "unparsed": []}

    # 「我的考试」页顺带给出课程类别，拿来给课表补 category
    cats = {}
    if "exams" in pages:
        cats = tju_exams.course_list(pages["exams"])
        r = tju_exams.parse(pages["exams"])
        parsed["exams"] += r["items"]
        parsed["unparsed"] += r["unparsed"]
    if "courses" in pages:
        r = tju_courses.parse(pages["courses"], categories=cats)
        parsed["courses"] += r["items"]
        parsed["unparsed"] += r["unparsed"]

    if not pages:
        raise RuntimeError("没有抓到任何页面")
    added = store.merge_latest("tju", parsed, replace=True)
    store.mark_pull("tju", True, sum(added.values()),
                    "%d 课 / %d 考试" % (added["courses"], added["exams"]))
    return {"counts": added, "unparsed": parsed["unparsed"]}


def pull_zhs(which=None, progress=None):
    import zhs_qr
    ok, note = zhs_qr.ensure_login()
    if not ok:
        raise NeedLogin(note or "未登录")
    pages = zhs_qr.fetch_pages(which=which)
    parsed = {"ddl": [], "progress": [], "unparsed": []}
    for name, html in pages.items():
        if name == "api":
            # 作业/考试的真身在这些 JSON 里，比解析渲染后的 DOM 可靠
            r = zhs_work.parse_api(html)
            parsed["ddl"] += r["items"]
            continue
        r = zhs_work.parse(html)
        parsed["ddl"] += r["items"]
        parsed["unparsed"] += r["unparsed"]
        r = zhs_progress.parse(html)
        parsed["progress"] += r["items"]
        parsed["unparsed"] += r["unparsed"]
    # 同一条作业会在多个页面重复出现，按 id 去重
    seen, uniq = set(), []
    for d in parsed["ddl"]:
        if d["id"] in seen:
            continue
        seen.add(d["id"])
        uniq.append(d)
    parsed["ddl"] = uniq
    seen, uniq = set(), []
    for p in parsed["progress"]:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        uniq.append(p)
    parsed["progress"] = uniq

    added = store.merge_latest("zhs", parsed, replace=True)

    # 基础数据已经保存；详情与答案属于可独立失败的后续阶段。
    detail_note = ""
    answer_result = None
    try:
        import zhs_detail
        from zhs_client import ZhsClient
        from answers.jobs import JOBS as answer_jobs
        with ZhsClient() as client:
            details = {}
            try:
                if progress:
                    progress({"stage": "details", "step": "正在补充作业要求和附件"})
                dr = zhs_detail.fetch_all(client=client)
                details = dr["items"]
                en = store.enrich_ddl(details)
                detail_note = "详情 %d 条，失败 %d" % (en["updated"], len(dr["errors"]))
            except Exception:
                detail_note = "详情暂未更新，原数据保留"
            answer_result = answer_jobs.inline(
                client, captured=pages.get("api"), details=details, progress=progress)
    except Exception as error:
        from zhs_client import PlatformError
        need_login = isinstance(error, PlatformError) and error.kind == "need_login"
        answer_result = {"ok": False, "partial": True, "needLogin": need_login,
                         "processed": 0, "counts": {},
                         "errors": [{"message": str(error) if isinstance(error, PlatformError)
                                     else "答案阶段未完成，已保留基础同步结果"}]}
    counts = (answer_result or {}).get("counts") or {}
    note = "%d 作业 / %d 进度 / %s / 答案 %d 完整" % (
        added["ddl"], added["progress"], detail_note, counts.get("available", 0))
    store.mark_pull("zhs", True, sum(added.values()), note)
    store.append_event("answers", "答案阶段：%s" % (
        "部分完成" if answer_result.get("partial") else "完成"), site="zhs")
    return {"counts": added, "unparsed": parsed["unparsed"],
            "detail": detail_note, "answers": answer_result}


def pull(sites=("tju", "zhs"), which=None, progress=None):
    """一次拉多地。单站失败不影响另一站，结果按站汇总。"""
    result = {"ok": True, "sites": {}, "needLogin": [], "errors": []}
    for site in sites:
        try:
            if site == "tju":
                r = pull_tju(which)
            elif site == "zhs":
                r = pull_zhs(which, progress=progress)
            else:
                continue
            result["sites"][site] = {"ok": True, "counts": r["counts"],
                                     "unparsed": len(r["unparsed"])}
            if r.get('answers') is not None:
                result['sites'][site]['answers'] = r['answers']
                if r['answers'].get('partial'):
                    result['partial'] = True
                if r['answers'].get('needLogin') or r['answers'].get('counts', {}).get('need_login'):
                    result['needLogin'].append(site)
        except NeedLogin as e:
            store.mark_pull(site, False, 0, "需要重新登录")
            store.append_event("need_login", str(e), site=site)
            result["sites"][site] = {"ok": False, "needLogin": True, "note": str(e)}
            result["needLogin"].append(site)
            result["ok"] = False
        except Exception as e:
            store.mark_pull(site, False, 0, str(e)[:120])
            store.append_event("error", "拉取失败 %s" % e, site=site)
            result["sites"][site] = {"ok": False, "error": str(e)[:200]}
            result["errors"].append("%s: %s" % (site, e))
            result["ok"] = False
    return result
