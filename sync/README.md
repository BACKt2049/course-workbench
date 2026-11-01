# 本地同步服务

本文说明现有程序的运行与接口。项目目标、当前缺陷、文件夹与 Agent 协作、增量同步以及版本更新路线见 [开发文档与演进方案](../docs/development-plan.md)。其中 SQLite、任务 API/CLI/MCP 和程序内更新属于待开发能力。

## 答案中心（2026-10-31）

发行入口为 `课程工作台-答案中心.exe`。1.3.0 首次桌面启动会选择数据目录，之后读取已保存的目录配置；
也可用环境变量 `WB_DATA` 指定目录。旧数据迁移保留源文件，不要求用户把数据搬到桌面。
桌面启动器会检查已有实例的版本与资源状态，并在需要接管时先备份。
源码运行仍使用根目录 `sync-data`，设置 `WB_DATA` 时以该变量为准。

登录智慧树后点“一键拉取”，先保存课表、待办、要求与附件，再更新已发现课程的全部作业答案，
包括已经完成的作业。答案阶段失败会报告部分完成，基础同步成果和成功缓存保留。
“答案中心”支持课程、关键词、状态筛选，刷新全部或单份，复制答案以及 TXT、Markdown、JSON 导出。
待办详情中的“查看答案”按课程ID、作业ID定位。历史作业仅进入答案中心，不加入待办清单。

七种状态是未检查、答案完整、答案部分可得、未公布、无试卷、需要登录、获取失败。
本期展示平台提供正确标记的选择题、判断题；未知答案会明确标注，成绩卡对错不生成选项答案。
老师未开放的答案不会被推断。课程来源合并本轮课程卡片、提醒和本账号历史索引，页面显示实际发现范围。
平台没有返回的课程不计入覆盖。无网络时本地服务可读取当前账号缓存；单独HTML和普通快照不含答案缓存。

答案存储于 `store.ROOT/answers/<accountKey>/<courseId>/<homeworkId>.json`，独立于 `latest.json`、普通快照和附件目录。
账号键由平台 UUID 派生；账号之间分别读取。仓库使用账号级写锁与原子替换，失败记录保留上次成功答案和获取时间。
切换账号后只读取对应账号的答案。清除登录态后需重新连接同一账号才能在页面读取该账号缓存。

```bat
python sync\cli.py answers --course-id 123 --export answers.json
python sync\zhs_answer_probe.py --course-id 123 --export answers.json
python -m unittest discover -s sync\tests -p "test_a*.py" -v
```

旧探测脚本的命令和参数保留，报告中的 `state=ok` 仍可用，新增 `availability` 表达完整状态。
附件启动菜单引用的 `zhs-hw.mjs` 未提供，本次集成基于工作台现有 Python 能力和本地平台响应样本。

模块关系：`answers/service.py` 是不依赖终端、HTTP、页面的应用服务；
`answers/jobs.py` 负责任务协调，供流水线、CLI和路由共用；`answers/api.py` 只适配HTTP；
`answers/zhs_provider.py` 适配平台；`zhs_client.py` 复用会话和本轮响应；
`answers/normalize.py` 是纯解析；`answers/repository.py` 负责持久化；`answers/exporters.py` 负责导出。
一个任务中的 Playwright 请求上下文始终在线程内部创建、使用、释放。

所有答案接口沿用工作台的 `X-WB-Token` 鉴权和同源检查：

| 接口 | 参数与结果 |
| --- | --- |
| `GET /api/answers` | `courseId`、`q`、`state`筛选当前账号缓存，返回课程和状态统计 |
| `GET /api/answers/{courseId}/{homeworkId}` | 单份结构化答案、时间及历史缓存标识 |
| `POST /api/answers/refresh` | JSON中的课程、作业ID均可选；返回后台任务ID |
| `GET /api/answers/jobs/{jobId}` | 阶段、进度、状态统计和失败明细 |
| `POST /api/answers/export` | `format=txt/md/json`，可用`keys`选择记录；直接返回下载内容 |

页面以 `sync/app.html` 为模板，由 `ui_assets.load_html()` 装配 `sync/web/answers.js` 与CSS。
源码服务、快照和EXE都使用该入口。根目录旧HTML保留；独立文件新版可由发布脚本生成。

构建：先安装 PyInstaller，并设置 `WB_BROWSER_SOURCE` 指向含匹配 `chromium-<revision>` 的目录，
再运行 `python -m PyInstaller --noconfirm --distpath . --workpath output/build-answers sync/课程工作台.spec`。
EXE包含Python、Playwright驱动与Chromium，使用者无需安装Node。

把「校园课程工作台」从纯离线单文件，升级成能从教务与智慧树拉数据的本地应用。
使用平台的登录与课程服务，不存密码；指定`--no-lan`时只监听127.0.0.1。

## 源码开发环境安装

以下步骤只用于源码运行。已构建的完整 Windows EXE 内置 Python、Playwright 驱动及 Chromium，用户无需另装内核。

```bat
python -m pip install -r sync\requirements.txt
python -m playwright install chromium
```

浏览器用于登录和动态页面访问，平台数据获取结合 HTTP 请求与页面解析。
天津大学使用“融合门户”App 扫码，智慧树使用“知到”App 扫码。

## 日常使用

双击根目录的 `启动工作台.bat`，或：

```powershell
python sync\cli.py serve --no-lan          # 起本机服务并打开浏览器
python sync\cli.py serve --no-lan --auto   # 顺带开启每天 08:00 / 20:00 自动刷新
```

页面里点「连接」出二维码 → 手机扫码 → 自动拉取。

## 命令行（不开页面也能用）

```powershell
python sync\cli.py up tju      # 连接天津大学并拉取，使用融合门户 App 扫码
python sync\cli.py up zhs      # 连接智慧树并拉取，使用知到 App 扫码
python sync\cli.py pull all    # 已有登录态时直接拉
python sync\cli.py status      # 登录态、上次拉取、各表条数、最近事件
python sync\cli.py snapshot    # 生成只读快照，分享前检查课程内容与个人信息
python sync\cli.py logout tju  # 清除登录态
```

## 数据在哪

| 目录 | 内容 | 能否外发 |
| --- | --- | --- |
| `sync-data/state/` | 登录态（cookie / storage_state）、服务 token | **绝对不行**，等同凭证 |
| `sync-data/raw/` | 每次拉取的原始 HTML，只留最近 20 份 | 含个人信息，别外发 |
| `sync-data/data/` | 结构化结果 `latest.json`、快照 HTML | 可能包含个人课程与笔记，分享前检查并脱敏 |
| `sync-data/answers/` | 各账号的答案缓存 | 含课程内容，按需要选择导出 |

`.gitignore` 已经把前两个目录排除了。

## 三种运行形态

页面自己探测，不需要配置：

- `file://` 打开 —— 离线，无同步卡，功能与以前一致
- `127.0.0.1:8787` —— 本地，有同步卡，可扫码可拉取
- 线上域名 / 快照 —— 只读，同步卡变成一句「这个页面不含登录态」

## 拉不到东西怎么查

1. `python sync\cli.py status` 看最近事件（失败原因都记在 `state/events.log`）
2. 解析结果是空但有 `unparsed` —— 可能是页面结构变化或抓取不完整；在当前数据目录的 `raw/` 中本地检查，需分享时先移除凭证与个人信息
3. 提示 `need_login` —— 登录态过期（通常 7–30 天），重新扫一次即可，已有数据不受影响

## 登录态能保持多久

三个因素叠在一起，前两个我们控制得了：

1. **学校给的 cookie 寿命**（我们决定不了）。天大 CAS 的 `CASTGC` 常见是 7 天，智慧树约 30 天。服务会把每个 cookie 的 `expires` 原样存下来，页面上直接写「有效至 11-06，还剩 7.0 天」。
2. **定期保活**（我们做了）。多数 SSO 是**滑动过期**——每次校验就往后顺延，放着不动反而先死。所以服务每 6 小时会轻量敲一次：天大直接请求教务首页（纯 HTTP），智慧树只带 cookie 请求学习中心（不启浏览器）。响应里下发了新 cookie 就连新的 `expires` 一起回写，这才是续期真正生效的地方。
3. **服务要一直在跑**。关掉 `启动工作台.bat` 的窗口，保活就停了；登录态本身还留在磁盘上，下次启动照旧能用，只是空窗期不计入续期。

另外一点比浏览器强：浏览器一关，会话 cookie 就没了；我们是把它序列化进 `sync/state/`，进程重启也能原样恢复。

失效时会怎样：`pull` 检测到被弹回登录页，返回 `need_login`，页面自动弹二维码，**已有数据一条不动**。

## 已知限制

- **天大要用「融合门户」App 扫**，不是企业微信、也不是微信。二维码是 CAS 页面点开 `Scan Code` 页签后由页面自己的 `login-qrcode.js` 生成的，uuid 是标准 UUID 且与本次 JSESSIONID 绑定。**不要试图自造 uuid** —— 拼出来的地址虽然能生成图、也能被扫，但服务端没有这条 uuid 的授权上下文，轮询永远拿不到结果（这个坑踩过一次）。
- **智慧树要点头部的「扫码」页签**才会有二维码，二维码是 `.qrcode-box` 里的一个 img（不是 canvas，也不是微信 qrconnect iframe）。该页面还常有字体加载挂住截图的情况，所以抓取时会拦掉字体请求。
- **智慧树用有头浏览器**：headless 下它渲染很不稳定，还容易撞限流。窗口弹出来是正常的，扫完自动关。想静默跑可以设 `WB_ZHS_HEADLESS=1`，但失败率高。
- **正方课表解析**是按「8 列表格 + 单元格多行」写的，真实 DOM 若有出入，第一次拉取后需要校准一次。校准前宁可显示「N 条待确认」，也不会写一份空课表覆盖你的数据。
- **智慧树的数据几乎都在 XHR 里**，只解析 HTML 拿不到作业和进度。所以抓取时会把所有 JSON 响应原样存进 `raw/zhs-api-*.html`，用来按真实结构校准解析器；这些样本不会直接入库。
- 解析校准应使用本地响应与界面核对，公开问题只附合成或脱敏样例，不直接发送原始抓取文件。

## 单文件 EXE 与附件管理

### 1.3.0 运行保障

- 首次桌面启动由 `runtime_config.py` 使用 Windows 原生选择器确定数据目录；保存配置及有效副本，支持旧数据的无覆盖复制和中断后继续迁移。
- `durable_json.py` 为用户 JSON 提供独立临时文件、fsync、原子替换和上一份有效副本恢复；损坏原文件会另行保留。
- `data_safety.py` 将账号状态、数据、附件、答案和浏览器笔记打包校验，自动及退出前备份，保留最近 10 份；运行组件和浏览器缓存不进入备份。
- HTML 先装配进内存并保存到 `runtime/ui/<version>`；Chromium 和 Playwright 驱动校验后复制到 `runtime`，消除长期依赖临时解压目录的问题。
- 内置内核在 `/api/setup` 标记为 bundled/ready，前端不再显示安装、下载或检查提示；需要时可手动调用鉴权后的 `/api/health?deep=1` 做组件诊断。
- `/api/bootstrap` 在同源、Host/Origin 检查后恢复连接配置；普通 API 的过期 token 可恢复后重试一次。
- 笔记、手动任务等完整工作台状态保存到 `data/workspace.json`；`/api/workspace` 验证结构后写入，浏览器缓存缺失时可恢复。
- 复用后台前验证版本和资源状态；升级时先备份，再通过退出接口接管。旧版无接口时，只处理经本目录 token 验证且进程文件名匹配工作台的端口所有者。
- Windows 下仅为浏览器公开二进制目录设置沙箱所需读/执行权限，保留浏览器沙箱；依据 [Puppeteer 官方 Windows 沙箱排查说明](https://pptr.dev/troubleshooting#chrome-reports-sandbox-errors-on-windows)。

验证：`python -m unittest discover -s sync/tests -p test_reliability.py -v`。
`output/reliability/verify_ui.py` 和 `verify_exe.py` 在独立数据目录验证页面与成品，均不使用个人账号。

`课程工作台.spec` 将当前 Playwright 对应版本的完整 Chromium 嵌入 EXE。
构建时找不到匹配版本会报错，不能发布缺少内核的包。浏览器源码目录通过
`WB_BROWSER_SOURCE` 指定，目录内应有 `chromium-<revision>/chrome-win64/chrome.exe`。

在工作区根目录用 PowerShell 构建：

```powershell
$env:WB_BROWSER_SOURCE = 'D:\Caches\ms-playwright'
python -m PyInstaller --noconfirm --workpath output\build\pyinstaller --distpath output\release sync\课程工作台.spec
```

发布 `output/release/课程工作台-答案中心.exe` 即可，无需旁置 browser 文件夹。
运行时从 EXE 恢复并校验内置资源到稳定的 `runtime` 目录，后续复用，不长期依赖临时解压目录。
在无系统 Edge/Chrome 的机器上，桌面窗口也能使用内置 Chromium 打开。

DDL 页的「作业附件」提供复制绝对路径、打开资源管理器、搜索、上传、预览、
重命名、替换和删除。同步与手动上传文件均存于 `DATA/att`；重命名和删除记录
保存在 `STATE/attachment-changes.json`，确保下一次同步不恢复旧名称或已删除的文件。
删除的文件移到 `DATA/att-trash` 留作回收副本。单文件上传上限为 100 MB。

离线回归检查：`python -m unittest discover -s sync/tests -p test_attachments.py -v`。
