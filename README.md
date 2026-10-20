# MsgDesk 个人消息工作台

用自己的账号只读采集网站和 QQ 群，交给大模型整理成消息与 Markdown 日报。
系统不发送、不回复、不代替你做任何社交操作，也不包含手机推送。

当前 MVP 的主链路是：

```text
网站 / QQ 来源 → 采集 → 模型处理 / 群聊摘要 → 消息与日报
```

## 运行

安装版：

```text
双击 MsgDesk-Setup-0.1.0.exe
选择安装位置（默认 %LOCALAPPDATA%\Programs\MsgDesk）
安装完成后自动打开 MsgDesk 窗口
```

绿色版：

```text
解压 MsgDesk-win64.zip，双击 MsgDesk.exe
```

第一次启动会自动生成 `config.json` 和数据库。窗口使用系统 WebView2，
有独立标题栏和任务栏图标。`config.json`、`data/`、`digests/` 都在程序旁边，
整个目录拷走就能迁移。

从源码运行：

```powershell
python -m msgdesk --config .\config.json init
python -m msgdesk --config .\config.json serve
python -m msgdesk --config .\config.json serve --desktop
```

命令行诊断：

```powershell
python -m msgdesk --config .\config.json status
python -m msgdesk --config .\config.json sync
python -m msgdesk --config .\config.json digest --date 2026-10-20
python -m msgdesk --config .\config.json napcat status
```

打绿色包和安装包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_package.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
```

产物：

- `dist\MsgDesk\`：免安装目录
- `dist\MsgDesk-win64.zip`：绿色包
- `dist\MsgDesk-Setup-0.1.0.exe`：安装包

## 目录

```text
msgdesk/
  api/
    server.py          HTTP、鉴权、静态文件
    routes.py          MVP JSON 路由
    static/
      index.html       页面结构
      app.js           页面逻辑
      app.css          页面样式
  adapters/
    web.py            网站采集
    qq_onebot.py      QQ / OneBot 只读适配器
  services/
    sync.py           来源同步与入库
    agent.py          网站智能体采集
    processing.py     单条消息模型处理
    conversation_digest.py  群聊按天摘要
    digest.py         Markdown 日报
    model.py          OpenAI 兼容模型客户端
    browser_login.py  网站浏览器登录与 Cookie
    session_keeper.py Cookie 续期
    scheduler.py      到期来源与日报调度
    worker.py         持久任务队列
  migrations/         SQLite 迁移
tests/                单元测试
packaging/            PyInstaller / Inno Setup
scripts/              打包与诊断脚本
config.json           本机配置（含密钥，不要外传）
data/                 数据库、原始快照、图片、登录态
digests/              按日期生成的日报
```

`config.json`、`data/`、`digests/` 已加入 `.gitignore`，不要提交。

## 来源

MVP 只支持两种来源：

- **网站**：填写地址、采集提示词、提示词附件和可选 Cookie。同步时模型拿到
  `fetch_page` 工具，只保留当天发布的内容，正文转 Markdown，图片落到
  `data/media/日期/`。
- **QQ**：通过本机 NapCat + OneBot 只读接入。来源页扫码登录后勾选群，
  每个群每天汇总一次。消息里的图片和文件会下载到 `data/attachments/日期/`，
  不限制文件类型。

微信、企业微信、RSS 和本地聊天导入不在 MVP 范围内。

## 模型

在「设置 → 模型配置」中添加 OpenAI 兼容供应商，填写 Base URL、API Key 和模型名。
未配置模型时，网站采集和消息处理会明确失败，界面不会把规则降级伪装成成功。
全局起止日期在「设置 → 基础配置」中统一配置；结束日期留空时跟随今天。

## 输出

- 消息页：按日期分组，支持全部 / 未读 / 待办 / 已忽略筛选，可搜索、改分类、
  已读、忽略、重新处理和删除。
- 日报：`digests/YYYY/YYYY-MM/YYYY-MM-DD.md`，包含概览、今日待办、
  群聊摘要、按主题汇总、来源索引和待处理与失败。
- 图片与附件：`data/media/`、`data/attachments/`。

## 测试

```powershell
F:\env\python\python.exe -m unittest discover -s tests -t .
F:\env\python\python.exe -m compileall -q msgdesk
```

当前基线为 44 项单元测试。

## 边界

- 只读采集，不提供发送、回复、点赞、转发接口。
- 需要登录的页面必须提供 Cookie；Cookie 失效时显示“登录失效”。
- 模型 API Key 与 Cookie 明文保存在本机 `config.json`，目录不要外传。
- 现有的 SQLite 迁移历史保留，旧数据库可以直接继续使用；停用功能的旧表不会删除。
