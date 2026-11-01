# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

单文件 EXE 内置完整 Chromium；构建时检查 Playwright 对应版本。
console=False 双击无黑窗，launcher 提供独立桌面窗口。
"""
import json
import hashlib
import os
from pathlib import Path

import playwright

from PyInstaller.utils.hooks import collect_all

_here = Path(SPECPATH)          # noqa: F821  PyInstaller 注入
_root = _here.parent
_ico = _root / "课程工作台.ico"

datas = [(str(_here / 'app.html'), '.'), (str(_here / 'web'), 'web')]
binaries = []
hiddenimports = [
    'server', 'store', 'cas_qr', 'zhs_qr', 'browser_login',
    'pipeline', 'zhs_detail', 'launcher', 'attachments',
    'runtime_config', 'runtime_assets', 'durable_json', 'data_safety', 'app_info',
    'zhs_client', 'zhs_answer_probe', 'ui_assets', 'answers',
    'answers.models', 'answers.normalize', 'answers.repository',
    'answers.service', 'answers.exporters', 'answers.api', 'answers.jobs', 'answers.zhs_provider',
]
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# 内核必须进入单文件 EXE；缺少匹配版本时拒绝构建。
_pw = Path(playwright.__file__).parent
_browsers = json.loads((_pw / 'driver/package/browsers.json').read_text(encoding='utf-8'))
_revision = next(b['revision'] for b in _browsers['browsers'] if b['name'] == 'chromium')
_dirname = 'chromium-' + _revision
_sources = [
    Path(os.environ.get('WB_BROWSER_SOURCE') or _here / 'browser'),
    Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or Path.home() / 'AppData/Local/ms-playwright'),
]
_browser = next((p / _dirname for p in _sources
                 if (p / _dirname / 'chrome-win64/chrome.exe').is_file()), None)
if _browser is None:
    raise RuntimeError('缺少 Chromium ' + _revision + '；设置 WB_BROWSER_SOURCE 后重新打包。')
datas.append((str(_browser), 'browser/' + _dirname))
_manifest = {}
for _file in _browser.rglob('*'):
    if _file.is_file():
        with _file.open('rb') as _stream:
            _manifest[_dirname + '/' + _file.relative_to(_browser).as_posix()] = hashlib.file_digest(_stream,'sha256').hexdigest()
_manifest_path = _here / 'browser-manifest.json'
_manifest_path.write_text(json.dumps(_manifest),encoding='utf-8')
datas.append((str(_manifest_path),'.'))
_driver_manifest = {}
for _driver_file in (_pw/'driver').rglob('*'):
    if not _driver_file.is_file():
        continue
    _relative = _driver_file.relative_to(_pw/'driver').as_posix()
    with _driver_file.open('rb') as _stream:
        _driver_manifest[_relative] = hashlib.file_digest(_stream,'sha256').hexdigest()
_driver_manifest_path = _here / 'driver-manifest.json'
_driver_manifest_path.write_text(json.dumps(_driver_manifest),encoding='utf-8')
datas.append((str(_driver_manifest_path),'.'))


a = Analysis(
    [str(_here / 'cli.py')],
    pathex=[str(_here)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=os.environ.get('WB_EXE_NAME', '课程工作台-答案中心'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_ico) if _ico.exists() else None,
)
