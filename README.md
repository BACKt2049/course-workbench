# 课程工作台

面向 Windows 的本地课程工作台，连接天津大学教务和智慧树，集中查看课表、待办、考试、作业附件与平台返回的作业答案。当前版本为 **1.3.0**。

## 功能

- 天津大学使用“融合门户”App 扫码，智慧树使用“知到”App 扫码。
- 同步课表、考试、作业要求、课程进度和附件；失败时保留已有数据。
- DDL 页提供统一附件目录、绝对路径复制、上传、搜索、重命名、替换和删除。
- 答案中心支持筛选、刷新、复制与 TXT、Markdown、JSON 导出；缺失答案会标明状态。
- 首次桌面启动选择数据文件夹，已有数据可保留或复制迁移。
- 配置与业务 JSON 使用原子写入及有效副本恢复；自动备份保留最近 10 份。
- Windows EXE 内置 Python、Playwright 驱动和 Chromium。运行资源保存到稳定目录，缺失或损坏时从 EXE 恢复。

## 从源码运行

需要 Python 3.11 或更新版本。Windows 桌面模式使用系统文件夹选择器；命令行服务使用标准库 HTTP 服务。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r sync/requirements.txt
python -m playwright install chromium

# 桌面模式：首次选择数据目录
python sync/cli.py desktop

# 或仅启动本机服务
python sync/cli.py serve --no-lan
```

源码安装步骤用于开发环境；已经构建的 Windows EXE 不需要用户另装 Python、Node 或浏览器内核。

默认本机地址为 `http://127.0.0.1:8787`。桌面程序会处理已运行实例；端口占用时服务可选择可用端口。开发时也可以显式指定目录：

```powershell
$env:WB_DATA = 'D:\CourseWorkbenchData'
python sync/cli.py serve --no-lan
```

## 数据与备份

首次桌面启动选择数据目录。目录配置保存在 `%LOCALAPPDATA%\课程工作台\app-settings.json`，并保留有效副本。源码服务未显式指定目录时使用工作区的 `sync-data`。

| 子目录 | 内容 |
| --- | --- |
| `state` | 本地登录会话与服务状态 |
| `data` | 课表、待办、笔记与 `att` 附件目录 |
| `answers` | 按账号分开的答案缓存 |
| `backups` | 已校验的数据备份 |
| `runtime` | 从 EXE 恢复的页面、浏览器和驱动 |

“设置与数据”中可以查看目录并立即备份。仓库仅包含程序代码和合成测试样例，不包含个人会话、课程数据、附件、答案缓存、浏览器资料或备份。

## 测试

以下 60 项测试不需要真实账号：

```powershell
python -m unittest discover -s sync/tests -p test_answers.py -v
python -m unittest discover -s sync/tests -p test_attachments.py -v
python -m unittest discover -s sync/tests -p test_reliability.py -v
```

`test_parsers.py` 是历史假页面测试，目前仍有 4 项课表/考试样例失败，未计入上述通过的测试。`e2e_local.py` 是旧的手工联调脚本，会写测试数据，应使用独立的 `WB_DATA` 目录和对应测试服务。

## 构建 Windows 单文件 EXE

在 Windows 开发环境安装构建工具，并指定 Playwright 对应的 Chromium 缓存目录：

```powershell
python -m pip install pyinstaller
python -m playwright install chromium
$env:WB_BROWSER_SOURCE = "$env:LOCALAPPDATA\ms-playwright"
python -m PyInstaller --noconfirm --workpath output/build --distpath output/release sync/课程工作台.spec
```

成品为 `output/release/课程工作台-答案中心.exe`。构建配置会检查内核版本并嵌入完整内核；缺少匹配版本时拒绝构建。EXE、内核缓存和构建产物不会提交到源码仓库。

生成独立页面与源码压缩包：

```powershell
python sync/build_release.py
```

## 代码结构

- `sync/server.py`：本地接口、连接恢复、健康检查与备份入口。
- `sync/runtime_config.py`、`durable_json.py`、`data_safety.py`、`runtime_assets.py`：目录选择、数据恢复、备份和运行组件恢复。
- `sync/browser_login.py`、`cas_qr.py`、`zhs_qr.py`、`zhs_client.py`：登录与平台会话。
- `sync/answers`：答案获取、解析、缓存和导出。
- `sync/parsers`：课程、考试、作业及进度解析。
- `sync/app.html` 与 `sync/web`：工作台界面。

更多接口与实现说明见 [开发说明](sync/README.md)，日常使用见 [使用说明](答案中心-使用说明.txt)。
