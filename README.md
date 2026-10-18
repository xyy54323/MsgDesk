# MsgDesk 个人消息工作台

用你自己的账号，只读采集 QQ、微信、企业微信、网站和资讯，交给大模型整理，
按日期写成 Markdown，并支持手机提醒。系统不发送、不回复、不代替你做任何社交操作。

## 运行

```powershell
F:\env\python\python.exe -m msgdesk --config .\config.json serve
```

启动后终端会打印带令牌的地址，形如 `http://127.0.0.1:8080/?token=...`。
也可以直接用 `msgdesk.cmd` / `msgdesk.ps1`。

## 目录

```
msgdesk/          后端：API、适配器、采集与处理服务
  api/static/     单页工作台（像素风前端）
tests/            单元测试
data/             SQLite 数据库、原始快照、下载的图片
digests/          按日期生成的 Markdown 日报
scripts/          辅助脚本
config.json       本机配置（含密钥，不要外传）
```

## 来源

类型共五种：网站、QQ、微信、企业微信、资讯。编辑页字段为地址、采集提示词、
登录 Cookie（可选）、启用。

- **网站 / 资讯**：由智能体采集。同步时模型拿到 `fetch_page` 工具，自己决定
  抓哪些页面，只保留发布日期是今天的条目，图片下载到 `data/media/日期/`，
  正文以 Markdown 入库。
- **QQ**：通过 OneBot 只读接入，需要本地运行 NapCat。
- **微信 / 企业微信**：暂无可用采集通道，可先建来源占位。

采集提示词写给模型看，例如：

```
只保留今天发布的校内通知；每条两三句话概括，写清截止时间和提交方式；
有附件列出文件名；正文用 Markdown。
```

## 模型

在「模型配置」里添加 OpenAI 兼容供应商（Base URL、API Key、模型列表），
用于消息分类、摘要、行动项，以及网站来源的智能体采集。未配置模型时，
网站来源无法采集。

## 输出

- 消息页：按日期分组，详情里渲染 Markdown，可删除、重处理、改分类。
- 日报：`digests/YYYY/YYYY-MM/YYYY-MM-DD.md`，含概览、待办、按主题汇总。
- 图片：`data/media/YYYY-MM-DD/`，页面通过 `/media/...` 访问。

## 测试

```powershell
F:\env\python\python.exe -m unittest discover -s tests -t .
```

## 边界

- 只读采集，不提供发送/回复接口。
- 需要登录的页面必须提供 Cookie，过期后要重新粘贴。
- 微信、企业微信尚未实现采集通道。
- 密钥与 Cookie 明文保存在本机 `config.json`，不要提交到公开仓库。
