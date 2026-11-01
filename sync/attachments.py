# -*- coding: utf-8 -*-
"""统一附件目录、文件操作，以及同步附件的重命名/删除记录。"""
import os
import re
import threading
from pathlib import Path

import store

LOCK = threading.RLock()
MAX_UPLOAD = 100 * 1024 * 1024


def root():
    path = store.DATA / "att"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def valid_name(name):
    if not isinstance(name, str) or not name or len(name) > 180:
        raise ValueError("文件名不能为空，且不能超过 180 个字符")
    if name != name.strip(" .") or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        raise ValueError("文件名含有非法字符，或以空格、句点结尾")
    if name in (".", "..") or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        raise ValueError("这个文件名不能在 Windows 中使用")
    return name


def path_for(name):
    path = root() / valid_name(name)
    # 不允许通过链接或目录操作附件目录以外的文件。
    if path.is_symlink() or path.resolve().parent != root():
        raise ValueError("路径超出附件目录")
    if path.exists() and not path.is_file():
        raise ValueError("请选择文件")
    return path


def changes():
    return store.read_json(store.STATE / "attachment-changes.json", {}) or {}


def resolve_local(local, mapping=None):
    mapping = changes() if mapping is None else mapping
    seen = set()
    while local in mapping and local not in seen:
        seen.add(local)
        local = mapping[local]
        if local is None:
            return None
    return local


def describe(path):
    stat = path.stat()
    return {"name": path.name, "local": "att/" + path.name,
            "size": stat.st_size, "ext": path.suffix.lstrip("."),
            "modified": stat.st_mtime, "downloaded": True}


def listing():
    with LOCK:
        files = [describe(p) for p in root().iterdir()
                 if p.is_file() and not p.is_symlink() and p.resolve().parent == root()]
        files.sort(key=lambda x: x["name"].casefold())
        return {"ok": True, "path": str(root()), "files": files,
                "count": len(files), "size": sum(f["size"] for f in files),
                "changes": changes()}


def normalize(files):
    mapping = changes()
    out = []
    for item in files:
        item = dict(item)
        old = item.get("local")
        if old:
            local = resolve_local(old, mapping)
            if local is None:
                continue
            if local.startswith("att/"):
                try:
                    path = path_for(local[4:])
                    item["local"] = local
                    item["downloaded"] = path.is_file()
                    if path.is_file():
                        item["size"] = path.stat().st_size
                    if local != old:
                        item["name"] = path.name
                        item["ext"] = path.suffix.lstrip(".")
                except (ValueError, OSError):
                    item["downloaded"] = False
        out.append(item)
    return out


def rename(name, new_name):
    with LOCK:
        old, new = path_for(name), path_for(new_name)
        if not old.is_file():
            raise FileNotFoundError("文件不存在，请刷新")
        if old.name == new.name:
            return describe(old)
        if new.exists():
            raise FileExistsError("同名文件已存在")
        mapping = changes()
        old_local, new_local = "att/" + old.name, "att/" + new.name
        if new_local in mapping and resolve_local(new_local, mapping) != old_local:
            raise FileExistsError("这个名称曾用于其他附件，请换一个文件名")
        for key in list(mapping):
            if resolve_local(key, mapping) == old_local:
                mapping[key] = new_local
        mapping.pop(new_local, None)
        mapping[old_local] = new_local
        old.rename(new)
        store.write_json(store.STATE / "attachment-changes.json", mapping)
        return describe(new)


def delete(name):
    with LOCK:
        path = path_for(name)
        if not path.is_file():
            raise FileNotFoundError("文件不存在，请刷新")
        mapping = changes()
        mapping["att/" + path.name] = None
        # 留一份本地回收副本，附件目录和任务卡中立即移除。
        recycle = store.DATA / "att-trash"
        recycle.mkdir(parents=True, exist_ok=True)
        import uuid
        path.rename(recycle / (uuid.uuid4().hex + "_" + path.name))
        store.write_json(store.STATE / "attachment-changes.json", mapping)


def upload(name, stream, size, replace=False, task_id=None):
    if size < 0 or size > MAX_UPLOAD:
        raise ValueError("单个文件不能超过 100 MB")
    with LOCK:
        path = path_for(name)
        if not replace:
            stem, suffix, number = path.stem, path.suffix, 2
            reserved = changes()
            while path.exists() or "att/" + path.name in reserved:
                path = path_for("%s (%d)%s" % (stem, number, suffix))
                number += 1
        elif not path.is_file():
            raise FileNotFoundError("待替换的文件不存在，请刷新")
        import tempfile
        # 临时文件位于附件目录外，管理列表不会列出上传中的文件。
        fd, temp_name = tempfile.mkstemp(prefix="attachment-", dir=str(store.DATA))
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as dest:
                remaining = size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("文件上传不完整")
                    dest.write(chunk)
                    remaining -= len(chunk)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        mapping = changes()
        if mapping.get("att/" + path.name, "absent") is None:
            mapping.pop("att/" + path.name)
            store.write_json(store.STATE / "attachment-changes.json", mapping)
        item = describe(path)
        item["manual"] = True
        if task_id:
            latest = store.read_latest()
            for task in latest.get("ddl", []):
                if task.get("id") == task_id:
                    task.setdefault("attachments", []).append(item)
                    store.write_json(store.LATEST, latest)
                    break
        return item
