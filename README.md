# MsgDesk

MsgDesk 是一个本地运行的个人消息聚合与日报工具。它以只读方式采集指定网站和 QQ 群的通知，借助大模型完成分类、摘要与待办提取，并按日生成 Markdown 日报。

系统不提供发送、回复、点赞或转发能力，所有数据保存在本机。

## 功能概览

- **多来源采集**：支持网站（静态列表页、模型智能体两种模式）与 QQ 群（NapCat / OneBot 11）。
- **消息管理**：按日期分组展示，支持来源筛选、关键词搜索、已读、忽略、分类调整与批量操作。
- **每日日报**：按可配置时间自动生成，包含概览、今日待办、群聊摘要、主题汇总与来源索引。
- **全局时间范围**：统一控制采集与日报的起止日期，结束日期默认跟随当天。
- **模型配置**：支持配置多个 OpenAI 兼容供应商，用于消息处理与群聊摘要。
- **个人画像**：将身份、关注点、忽略项与待办规则注入模型提示词，提升摘要相关性。
- **开机自启**：Windows 登录后自动运行并继续采集，可在设置中随时关闭。

## 工作原理

```text
来源（网站 / QQ）
    → 采集适配器
    → 规范化与去重
    → 模型处理 / 群聊摘要
    → SQLite 消息库
    → Markdown 日报
```

## 系统要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 及以上（x64） |
| Python | 源码运行需要 3.13；安装包已内置运行时 |
| 桌面窗口 | Microsoft Edge WebView2 Runtime（Windows 10/11 通常已预装） |
| 网络 | 可访问所配置的来源站点与模型服务 |
| QQ 来源 | 本机 NapCat（安装包已内置），首次使用需扫码登录 |

## 安装

正式版本发布于 GitHub Releases 页面。

- **安装版**：下载 `MsgDesk-Setup-<版本>.exe` 运行安装程序。安装无需管理员权限，默认安装到当前用户目录。
- **绿色版**：下载 `MsgDesk-win64.zip`，解压后运行 `MsgDesk.exe`。

首次启动会在程序目录生成 `config.json` 与 `data/`，程序目录可整体迁移。

## 使用

1. 在「设置 → 模型配置」中添加 OpenAI 兼容供应商，填写 Base URL、API Key 与模型名称。
2. 在「设置 → 基础配置」中设置全局起止日期、日报生成时间与开机自启。
3. 在「设置 → 来源」中添加网站或 QQ 来源，并按需填写采集提示词、Cookie 或选择群聊。
4. 在「消息」页面查看、筛选和处理采集结果；在「日报」中查看按日汇总。

系统按来源配置的周期自动同步，并在设定的日报时间生成当日日报；应用未运行期间不会执行采集与调度。

## 目录结构

```text
msgdesk/
  adapters/       来源适配器（网站、QQ / OneBot）
  api/            HTTP 服务、JSON 路由与前端静态资源
  services/       同步、模型处理、群聊摘要、日报、调度与任务队列
  migrations/     SQLite 数据库迁移
tests/            单元测试
packaging/        PyInstaller 与 Inno Setup 配置
scripts/          构建、打包与诊断脚本
data/             数据库、原始快照、图片、附件与日志
digests/          按日期生成的 Markdown 日报
```

主要数据文件：

| 路径 | 说明 |
| --- | --- |
| `config.json` | 本机配置，包含模型 API Key 与 Cookie，属于敏感文件 |
| `data/messages.db` | SQLite 消息数据库 |
| `data/raw/` | 采集原始快照 |
| `data/media/` | 网站正文图片 |
| `data/attachments/` | QQ 与网站附件 |
| `digests/YYYY/YYYY-MM/YYYY-MM-DD.md` | 按日期生成的日报 |

`config.json`、`data/`、`digests/` 均不纳入版本控制，也不会打包进安装包。

## 从源码运行

```powershell
python -m pip install pywebview==6.2.1 pythonnet==3.2.1
python -m msgdesk --config .\config.json init
python -m msgdesk --config .\config.json serve --desktop
```

常用命令：

| 命令 | 说明 |
| --- | --- |
| `serve` | 启动 API、调度器与后台任务 |
| `serve --desktop` | 以桌面窗口方式启动 |
| `sync` | 立即同步全部来源 |
| `digest --date YYYY-MM-DD` | 生成指定日期的日报 |
| `status` | 查看运行状态 |
| `napcat` | 管理内置 QQ 桥接（安装、配置、启动、停止） |

## 测试

```powershell
python -m unittest discover -s tests -t .
python -m compileall -q msgdesk
```

当前基线为 45 项单元测试。

## 构建与发布

Windows 安装包通过 GitHub Actions 构建，不在本机构建。

在仓库 Actions 页面选择 **Build Windows Package** → **Run workflow**，按需填写以下参数：

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `version` | 安装包版本号，格式为 `x.y.z` | `0.2.0` |
| `bundle_napcat` | 是否内置 QQ 桥接 NapCat（约 345MB） | `true` |
| `with_browser` | 是否内置网站登录用 Chromium（约 150MB） | `false` |
| `publish_release` | 构建成功后发布 GitHub Release | `false` |

工作流依次执行单元测试、打包、打包后烟雾测试、产物安全检查与安装包生成，并上传 `MsgDesk-Setup-<版本>.exe` 与 `MsgDesk-win64.zip` 两个附件。启用 `publish_release` 时，将自动创建 `v<版本>` 标签并发布 Release。

## 隐私与安全

- 系统只读采集，不包含任何发送、回复或社交操作接口。
- 本地服务默认仅监听 `127.0.0.1`，不对局域网开放。
- 构建工作流会校验产物中不包含 `config.json`、`data/`、`digests/`。
- 模型 API Key 与登录 Cookie 以明文保存在本机 `config.json`，请勿将该文件或程序数据目录外传。

## 已知限制

- QQ 历史图片的原始链接会过期；本机缓存缺失时无法恢复，消息中会明确标注。
- 部分校内网附件通过带图形验证码的下载页提供，需在浏览器中手动完成。
- 定时采集与日报生成依赖应用保持运行，关闭应用后不会执行。
- 当前版本仅支持网站与 QQ 来源，微信、企业微信与 RSS 尚未纳入范围。

## 版本

当前版本：`0.2.0`（标签 `v0.2.0`）。
