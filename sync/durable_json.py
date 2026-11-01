"""带上一份有效副本、独立临时文件和损坏恢复的 JSON 读写。"""
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path

LOCK = threading.RLock()


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value, preserve=True):
    path = Path(path)
    with LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.with_name(path.name + '.bak')
        if preserve and path.is_file():
            try:
                _load(path)
                shutil.copy2(path, previous)
            except (ValueError, OSError):
                pass
        fd, name = tempfile.mkstemp(prefix=path.name + '-', suffix='.tmp', dir=str(path.parent))
        temp = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            if not previous.exists():
                shutil.copy2(path, previous)
        finally:
            temp.unlink(missing_ok=True)


def read(path, default=None):
    path = Path(path)
    with LOCK:
        try:
            return _load(path)
        except (ValueError, OSError):
            pass
        try:
            value = _load(path.with_name(path.name + '.bak'))
        except (ValueError, OSError):
            return default
        try:
            if path.exists():
                damaged = path.with_name(path.name + '.damaged-' + datetime.now().strftime('%Y%m%d%H%M%S%f'))
                shutil.copy2(path, damaged)
            write(path, value, preserve=False)
            with (path.parent / 'recovery.log').open('a', encoding='utf-8') as log:
                log.write(datetime.now().isoformat(timespec='seconds') + ' 已从副本恢复 ' + path.name + '\n')
        except OSError:
            # 目录暂时只读时仍能读取有效副本，不把用户数据当成空数据。
            pass
        return value
