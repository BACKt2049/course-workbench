"""源码、EXE与快照共用的单文件页面装配。"""
from pathlib import Path
import sys
from app_info import APP_VERSION

_CACHED = None


def load_html():
    global _CACHED
    if _CACHED is not None and getattr(sys,'frozen',False):
        return _CACHED
    import store
    cached = store.ROOT / 'runtime/ui' / APP_VERSION / 'page.html'
    template = store.html_path()
    if template is None:
        if cached.is_file():
            _CACHED = cached.read_text(encoding='utf-8')
            return _CACHED
        raise FileNotFoundError('页面资源暂不可用，请关闭并重新打开程序以自动恢复')
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    html = template.read_text(encoding='utf-8')
    css = (root / 'web/answers.css').read_text(encoding='utf-8')
    js = (root / 'web/answers.js').read_text(encoding='utf-8')
    html = html.replace('</head>', '<style id="answer-center-styles">' + css + '</style></head>', 1)
    before, closing, after = html.rpartition('</body>')
    if not closing:
        raise ValueError('页面缺少body结束标签')
    result = before + '<script id="answer-center-script">' + js + '</script>' + closing + after
    if getattr(sys,'frozen',False):
        from runtime_assets import atomic_bytes
        atomic_bytes(cached,result.encode('utf-8'))
        _CACHED = result
    return result
