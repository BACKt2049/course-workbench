"""数据快照备份：排除运行内核和缓存，包含登录态、附件、答案与浏览器笔记。"""
import json
import os
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

LOCK = threading.Lock()
DATA_FOLDERS = ('state', 'data', 'raw', 'answers')
PROFILE_FOLDERS = ('Default/Local Storage', 'Default/IndexedDB', 'Default/Session Storage', 'Default/WebStorage')


def seed_recovery(root):
    import durable_json
    for name in ('state','data','answers'):
        folder = Path(root) / name
        if folder.is_dir():
            for file in folder.rglob('*.json'):
                if file.name == 'server.json' or not file.resolve().is_relative_to(Path(root).resolve()):
                    continue
                value = durable_json.read(file,None)
                if value is not None and not file.with_name(file.name+'.bak').exists():
                    import shutil
                    shutil.copy2(file,file.with_name(file.name+'.bak'))


def user_files(root):
    root = Path(root)
    seen = set()
    for name in DATA_FOLDERS:
        folder = root / name
        if folder.is_dir():
            for file in folder.rglob('*'):
                if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(root.resolve()):
                    relative = file.relative_to(root).as_posix()
                    if relative not in ('state/server.json', 'state/server.json.bak') and not file.name.endswith('.tmp'):
                        seen.add(relative)
                        yield file
    for profile in ('edge-profile', 'chromium-profile'):
        for sub in PROFILE_FOLDERS:
            folder = root / profile / sub
            if folder.exists():
                for file in folder.rglob('*'):
                    if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(root.resolve()):
                        relative = file.relative_to(root).as_posix()
                        if relative not in seen and file.name != 'LOCK':
                            seen.add(relative)
                            yield file
    for name in ('.workbench-settings.json', '.workbench-settings.json.bak', 'app-settings.json', 'app-settings.json.bak'):
        file = root / name
        if file.is_file():
            yield file


def backup(root, reason='自动备份', force=False):
    root = Path(root).resolve()
    folder = root / 'backups'
    with LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        previous = sorted(folder.glob('workbench-*.zip'), key=lambda p:p.stat().st_mtime, reverse=True)
        if not force and previous and time.time() - previous[0].stat().st_mtime < 12 * 3600:
            return {'ok':True, 'file':str(previous[0]), 'created':False}
        files = list(user_files(root))
        name = 'workbench-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.zip'
        target = folder / name
        temp = folder / (name + '.tmp')
        skipped = []
        try:
            with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as archive:
                for file in files:
                    try:
                        archive.write(file, file.relative_to(root).as_posix())
                    except (PermissionError, FileNotFoundError):
                        skipped.append(file.relative_to(root).as_posix())
                archive.writestr('backup-manifest.json', json.dumps({
                    'format':1, 'source':str(root), 'reason':reason,
                    'createdAt':datetime.now().isoformat(timespec='seconds'),
                    'fileCount':len(files)-len(skipped), 'skipped':skipped},ensure_ascii=False,indent=2))
            with zipfile.ZipFile(temp) as archive:
                if archive.testzip():
                    raise OSError('备份校验失败')
            os.replace(temp, target)
            # 只轮换本程序创建的备份压缩包，保留最近 10 份。
            for old in previous[9:]:
                if old.resolve().parent == folder.resolve():
                    old.unlink()
        finally:
            temp.unlink(missing_ok=True)
        return {'ok':True, 'file':str(target), 'created':True, 'skipped':skipped}


def information(root):
    folder = Path(root) / 'backups'
    files = sorted(folder.glob('workbench-*.zip'), key=lambda p:p.stat().st_mtime, reverse=True) if folder.exists() else []
    return {'directory':str(folder.resolve()), 'count':len(files),
            'latest':str(files[0]) if files else '', 'lastAt':files[0].stat().st_mtime if files else None}
