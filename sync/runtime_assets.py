"""将随包运行资源恢复到稳定目录，避免长期运行依赖临时解压文件。"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

LOCK = threading.RLock()


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name+'-',suffix='.tmp',dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def browser(source, destination, manifest=None):
    source, destination = Path(source), Path(destination)
    manifest = manifest or {}
    with LOCK:
        files = [p for p in source.rglob('*') if p.is_file()]
        if not files:
            raise OSError('程序内置浏览器资源不可用，请重新打开程序以恢复')
        for file in files:
            relative = file.relative_to(source)
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()):
                raise OSError('运行资源路径异常')
            expected = manifest.get(relative.as_posix())
            valid = target.is_file() and target.stat().st_size == file.stat().st_size
            if valid and expected:
                with target.open('rb') as stream:
                    valid = hashlib.file_digest(stream,'sha256').hexdigest() == expected
            if not valid:
                target.parent.mkdir(parents=True,exist_ok=True)
                fd, name = tempfile.mkstemp(prefix='repair-',suffix='.tmp',dir=str(target.parent))
                os.close(fd)
                temp = Path(name)
                try:
                    shutil.copy2(file,temp)
                    if expected:
                        with temp.open('rb') as stream:
                            if hashlib.file_digest(stream,'sha256').hexdigest() != expected:
                                raise OSError('内置资源完整性检查失败：'+file.name)
                    os.replace(temp,target)
                finally:
                    temp.unlink(missing_ok=True)
        return destination


def playwright_driver(bundle, destination, manifest=None):
    """Playwright 的 Node 与 JS 驱动也不能长期依赖临时目录。"""
    import playwright._impl._driver as driver
    destination = browser(Path(bundle)/'playwright/driver',destination,manifest)
    node = destination / ('node.exe' if os.name=='nt' else 'node')
    cli = destination / 'package/cli.js'
    if not node.is_file() or not cli.is_file():
        raise OSError('内置运行驱动不完整，请重新打开程序以恢复')
    os.environ['PLAYWRIGHT_NODEJS_PATH'] = str(node)
    driver.compute_driver_executable = lambda:(str(node),str(cli))
    import playwright._impl._transport as transport
    transport.compute_driver_executable = driver.compute_driver_executable
    return destination


def allow_browser_sandbox(directory):
    """只为公开的浏览器二进制设置 Windows 沙箱读/执行权限，保留浏览器沙箱。"""
    if os.name != 'nt':
        return
    directory = Path(directory).resolve()
    if not any(directory.glob('chromium-*/chrome-win64/chrome.exe')):
        raise OSError('浏览器组件路径不正确')
    result = subprocess.run(['icacls.exe',str(directory),'/grant','*S-1-15-2-1:(OI)(CI)(RX)',
                             '/T','/C','/Q'],capture_output=True,timeout=25,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise OSError('系统未能设置浏览器组件的运行权限，请选择自己拥有写入权限的数据文件夹')
