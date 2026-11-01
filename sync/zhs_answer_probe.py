# -*- coding: utf-8 -*-
"""兼容原答案探测命令；HTTP、同步与CLI共用答案服务。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(course_ids=None, download=False, verbose=True):
    from answers.jobs import JOBS, repository
    from answers.models import LABELS, now
    from zhs_client import ZhsClient
    summaries = []
    with ZhsClient() as client:
        if course_ids:
            for course in course_ids:
                summaries.append(JOBS.inline(client, scope={"courseId": str(course)}))
        else:
            summaries.append(JOBS.inline(client))
        repo = repository(client.account_key)
        entries = repo.listing()
        if course_ids:
            allowed = {str(c) for c in course_ids}
            entries = [e for e in entries if e["courseId"] in allowed]
        report = {"schemaVersion": 2, "generatedAt": now(), "courses": {},
                  "refresh": summaries}
        for entry in entries:
            if download and entry.get("attachments"):
                from zhs_detail import download_files
                entry["attachments"] = download_files(entry["attachments"])
            course = report["courses"].setdefault(entry["courseId"],
                {"courseName": entry["courseName"], "homework": []})
            legacy = dict(entry, availability=entry["state"],
                          n_question=entry.get("questionCount", 0),
                          n_correct=sum(len(q.get("answer") or []) for q in entry.get("questions") or []))
            if legacy["state"] == "available":
                legacy["state"] = "ok"
            course["homework"].append(legacy)
            if verbose:
                print("[%s] %s / %s" % (LABELS[entry["state"]], entry["courseName"], entry["title"]))
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="智慧树作业答案中心（兼容原探测命令）")
    parser.add_argument("--course-id", type=int, action="append")
    parser.add_argument("--export")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.course_id, args.download, not args.quiet)
    except Exception as error:
        from zhs_client import PlatformError
        print(str(error) if isinstance(error, PlatformError) else "答案任务失败，请检查本地数据目录")
        return 1
    if args.export:
        Path(args.export).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结果已写到", args.export)
    return 1 if any(s.get("partial") for s in result["refresh"]) else 0


if __name__ == "__main__":
    sys.exit(main())
