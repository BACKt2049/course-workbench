"""首次选择数据目录、配置持久化与旧数据迁移；不依赖浏览器或第三方 GUI。"""
import ctypes
import os
import shutil
import sys
import tempfile
import time
import traceback
from ctypes import wintypes
from pathlib import Path

import durable_json


def report_failure(kind,error,tb):
    log = settings_path().parent/'startup.log'
    try:
        log.parent.mkdir(parents=True,exist_ok=True)
        with log.open('a',encoding='utf-8') as stream:
            stream.write(time.strftime('%Y-%m-%d %H:%M:%S')+'\n'+''.join(traceback.format_exception(kind,error,tb))+'\n')
    except OSError:
        pass
    if os.name=='nt' and (len(sys.argv)==1 or sys.argv[1]=='desktop'):
        ctypes.windll.user32.MessageBoxW(None,'启动暂时失败，请重新打开程序。现有数据不会被清空。\n'+str(error)[:240]+'\n诊断日志：'+str(log),'课程工作台启动恢复',0x10)
    else:
        sys.__excepthook__(kind,error,tb)


def settings_path():
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
    return base / '课程工作台/app-settings.json'


def settings():
    obj = durable_json.read(settings_path(), {})
    return obj if isinstance(obj, dict) else {}


def legacy_root(exedir=None):
    exedir = Path(exedir or Path(sys.executable).parent)
    beside = exedir / 'sync-data'
    if beside.is_dir():
        return beside
    return settings_path().parent


def configured_root(exedir=None):
    if os.environ.get('WB_DATA'):
        return Path(os.environ['WB_DATA']).expanduser().resolve()
    value = settings()
    if value.get('setupComplete') and value.get('dataDir'):
        selected = Path(value['dataDir']).expanduser()
        if not selected.is_dir():
            raise OSError('已选择的数据文件夹当前不可用：' + str(selected))
        return selected.resolve()
    return legacy_root(exedir).resolve()


def writable(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.workbench-write-', dir=str(path), delete=True) as file:
        file.write(b'OK')
        file.flush()


def save_directory(path):
    path = Path(path).expanduser().resolve()
    writable(path)
    value = {'schemaVersion':1, 'setupComplete':True, 'dataDir':str(path)}
    durable_json.write(path / '.workbench-settings.json', value)
    durable_json.write(settings_path(), value)
    return path


def pick_directory(default):
    """Windows 系统原生文件夹选择器，可新建文件夹；取消时不启动空数据实例。"""
    if os.name != 'nt':
        return None
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, ctypes.c_uint, wintypes.LPARAM, wintypes.LPARAM)
    default_text = ctypes.create_unicode_buffer(str(default))

    @callback_type
    def callback(hwnd, message, _param, _data):
        if message == 1:  # BFFM_INITIALIZED
            ctypes.windll.user32.SendMessageW(hwnd, 0x467, 1, ctypes.cast(default_text, ctypes.c_void_p).value)
        return 0

    class BrowseInfo(ctypes.Structure):
        _fields_ = [('hwndOwner',wintypes.HWND), ('pidlRoot',ctypes.c_void_p),
                    ('pszDisplayName',wintypes.LPWSTR), ('lpszTitle',wintypes.LPCWSTR),
                    ('ulFlags',ctypes.c_uint), ('lpfn',callback_type),
                    ('lParam',wintypes.LPARAM), ('iImage',ctypes.c_int)]
    shell = ctypes.windll.shell32
    shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    ctypes.windll.user32.SendMessageW.argtypes = [wintypes.HWND,ctypes.c_uint,wintypes.WPARAM,wintypes.LPARAM]
    ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    display, result = ctypes.create_unicode_buffer(260), ctypes.create_unicode_buffer(32768)
    info = BrowseInfo(None,None,ctypes.cast(display,wintypes.LPWSTR),'选择数据文件夹：课表、笔记、作业附件、答案及自动备份都保存在这里。',0x51,callback,0,0)
    initialized = ctypes.windll.ole32.CoInitializeEx(None,2) in (0,1)
    pidl = None
    try:
        pidl = shell.SHBrowseForFolderW(ctypes.byref(info))
        return Path(result.value) if pidl and shell.SHGetPathFromIDListW(pidl,result) else None
    finally:
        if pidl:
            ctypes.windll.ole32.CoTaskMemFree(pidl)
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


def migrate(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        return
    destination.mkdir(parents=True,exist_ok=True)
    pending = destination / '.migration-pending.json'
    durable_json.write(pending,{'source':str(source)})
    import data_safety
    files = list(data_safety.user_files(source))
    if files:
        data_safety.backup(source, reason='首次设置目录前', force=True)
    for file in files:
        target = destination / file.relative_to(source)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
    previous = durable_json.read(source/'state/server.json',None)
    if isinstance(previous,dict) and previous.get('port') and previous.get('token'):
        # 迁移后接管原端口，保留浏览器缓存所属的同一来源地址。
        durable_json.write(destination/'state/previous-server.json',previous)
    pending.unlink(missing_ok=True)
    pending.with_name(pending.name+'.bak').unlink(missing_ok=True)


def prepare_desktop():
    if os.environ.get('WB_DATA'):
        path = Path(os.environ['WB_DATA']).expanduser().resolve()
        writable(path)
        return path
    try:
        value = settings()
        if value.get('setupComplete'):
            path = configured_root()
            writable(path)
            return path
    except (OSError,TypeError,ValueError) as error:
        ctypes.windll.user32.MessageBoxW(None,str(error)+'\n请重新选择原数据文件夹，或选择可用目录恢复数据。','数据目录暂不可用',0x30)
    old = legacy_root()
    while True:
        path = pick_directory(old)
        if path is None:
            return None
        try:
            writable(path)
            # 已含有工作台数据的目录直接打开，不把另一份数据覆盖进去。
            pending = durable_json.read(path/'.migration-pending.json',None)
            if isinstance(pending,dict) and pending.get('source'):
                migrate(Path(pending['source']),path)
            elif not any((path/name).exists() for name in ('data','answers','state')):
                migrate(old,path)
            return save_directory(path)
        except OSError as error:
            ctypes.windll.user32.MessageBoxW(None,'这个目录暂时无法使用：'+str(error)+'\n请选择其他文件夹。','无法保存数据',0x30)
