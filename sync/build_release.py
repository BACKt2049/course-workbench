"""生成独立页面和不含个人数据的源码包；EXE由PyInstaller构建。"""
from pathlib import Path
import sys
import zipfile
import ui_assets


def main():
    source = Path(__file__).resolve().parent
    import ast
    for python in source.rglob('*.py'):
        if '__pycache__' not in python.parts:
            ast.parse(python.read_text(encoding='utf-8-sig'),filename=str(python))
    root = source.parent
    output = root/'output/release'
    output.mkdir(parents=True, exist_ok=True)
    # 和本地服务、快照、EXE使用同一个页面装配入口。
    (output/'校园课程工作台-答案中心.html').write_text(ui_assets.load_html(), encoding='utf-8')
    files = [p for p in source.iterdir() if p.is_file() and not p.name.startswith('.') and
             p.suffix in ('.py', '.html', '.spec', '.md', '.txt')]
    files.append(source/'.gitignore')
    for folder in ('answers', 'parsers', 'tests', 'web'):
        allowed = ('.js', '.css') if folder == 'web' else ('.py',)
        files += [p for p in (source/folder).rglob('*') if p.is_file() and
                  '__pycache__' not in p.parts and p.suffix in allowed]
    name = output/'课程工作台-答案中心-源码.zip'
    with zipfile.ZipFile(name, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            archive.write(path, path.relative_to(root).as_posix())
        archive.write(root/'答案中心-使用说明.txt','答案中心-使用说明.txt')
        if (root/'课程工作台.ico').is_file():
            archive.write(root/'课程工作台.ico','课程工作台.ico')
    with zipfile.ZipFile(name) as archive:
        assert not archive.testzip()
        assert not any('/state/' in n or '/raw/' in n or '/data/' in n or '__pycache__' in n for n in archive.namelist())
    print('已生成：'+str(name))
    print('已生成：'+str(output/'校园课程工作台-答案中心.html'))


if __name__ == '__main__':
    main()
