"""Command line entry points."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import PROJECT_ROOT, AppConfig, load_config
from .errors import AppError
from .runtime import Runtime
from .timeutil import iso_utc, local_now_text, local_zone, utc_now

CONFIG_TEMPLATE = {
    "profile": {
        "identity": "",
        "interests": "",
        "ignore": "",
        "todo_rule": "",
        "notes": "",
    },
    "system": {
        "timezone": "Asia/Shanghai",
        "data_dir": "./data",
        "digest_dir": "./digests",
        "bind_host": "127.0.0.1",
        "port": 8080,
        "log_level": "INFO",
        "require_auth": True,
    },
    "scheduling": {
        "default_interval_minutes": 10,
        "max_parallel_jobs": 3,
        "digest_time": "22:00",
        "quiet_hours": {"start": "23:00", "end": "07:00", "allow_urgent": True},
    },
    "model": {
        "provider": "rules",
        "base_url": "",
        "model": "",
        "api_key_env": "MSGDESK_MODEL_API_KEY",
        "timeout_seconds": 60,
        "max_input_chars": 6000,
        "daily_call_limit": 500,
    },
    "notifications": {
        "min_priority": "high",
        "hourly_limit": 6,
        "channels": [
            {
                "id": "local-file",
                "type": "file",
                "enabled": True,
                "config": {"notify_digest": True},
            }
        ],
    },
}


def setup_logging(config: AppConfig) -> None:
    level = getattr(logging, str(config.system.log_level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        log_path = config.data_dir / "logs" / "app.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass


def _configure_stdio() -> None:
    """Use UTF-8 for Chinese output on Windows consoles and redirected logs."""
    if sys.stdout is None or sys.stderr is None:
        # Windowed PyInstaller build: keep prints harmless.
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _fatal_window(message: str) -> None:
    """Show a native message box when the app runs without a console."""
    try:
        from .paths import is_frozen

        if not is_frozen():
            return
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "MsgDesk", 0x10)
    except Exception:  # noqa: BLE001 - best effort only
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msgdesk", description="个人消息收集与处理工作台")
    parser.add_argument("--config", help="配置文件路径，默认 ./config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化配置、数据库和默认规则")
    sub.add_parser("config-check", help="检查配置")

    serve = sub.add_parser("serve", help="启动 API、调度器和任务 worker")
    serve.add_argument("--no-scheduler", action="store_true", help="只启动 API 和 worker")
    serve.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    serve.add_argument("--desktop", action="store_true", help="用桌面窗口打开（WebView2）")

    sync = sub.add_parser("sync", help="立即同步来源")
    sync.add_argument("--source", help="来源 ID；省略时同步全部启用来源")
    sync.add_argument("--max-items", type=int, default=50)

    digest = sub.add_parser("digest", help="生成日报")
    digest.add_argument("--date", help="YYYY-MM-DD，默认今天")

    reprocess = sub.add_parser("reprocess", help="重新处理消息")
    reprocess.add_argument("--source", help="只处理指定来源")
    reprocess.add_argument("--limit", type=int, default=200)

    sub.add_parser("status", help="查看运行状态")
    sub.add_parser("doctor", help="检查数据库、目录和适配器")
    sub.add_parser("demo", help="创建模拟来源并跑通完整链路")
    sub.add_parser("backup", help="备份数据库、配置和日报")
    sub.add_parser("start", help="后台启动服务")
    sub.add_parser("stop", help="停止后台服务")

    napcat = sub.add_parser("napcat", help="管理 QQ 桥（NapCat）")
    napcat.add_argument(
        "action",
        choices=["install", "configure", "start", "stop", "status", "relogin"],
    )
    napcat.add_argument("-q", "--uin", help="QQ 号，用于免扫码快速登录")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    from .paths import app_root, configure_runtime_env, is_frozen

    configure_runtime_env()
    if argv is None:
        argv = sys.argv[1:]
    if not argv and is_frozen():
        # Double-click: first run prepares the workspace, then opens the UI.
        if not (app_root() / "config.json").exists():
            cmd_init(None)
        argv = ["serve", "--desktop"]
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            return cmd_init(args.config)
        if args.command == "napcat":
            return cmd_napcat(args.action, args.uin)
        config = load_config(args.config)
        setup_logging(config)
        if args.command == "config-check":
            return cmd_config_check(config)
        runtime = Runtime(config)
        if args.command == "serve":
            return cmd_serve(
                runtime,
                no_scheduler=args.no_scheduler,
                open_browser=args.open,
                desktop=args.desktop,
            )
        if args.command == "sync":
            return cmd_sync(runtime, args.source, args.max_items)
        if args.command == "digest":
            return cmd_digest(runtime, args.date)
        if args.command == "reprocess":
            return cmd_reprocess(runtime, args.source, args.limit)
        if args.command == "status":
            return cmd_status(runtime)
        if args.command == "doctor":
            return cmd_doctor(runtime)
        if args.command == "demo":
            return cmd_demo(runtime)
        if args.command == "backup":
            return cmd_backup(runtime)
        if args.command == "start":
            return cmd_start(config)
        if args.command == "stop":
            return cmd_stop(config)
        return 2
    except AppError as exc:
        print(f"[错误] {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0


def cmd_init(config_path: str | None) -> int:
    target = Path(config_path) if config_path else PROJECT_ROOT / "config.json"
    target = target.resolve()
    if target.exists():
        print(f"配置已存在，保留原文件: {target}")
    else:
        target.write_text(
            json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已写入配置模板: {target}")
    config = load_config(target)
    setup_logging(config)
    runtime = Runtime(config)
    print(f"数据库已初始化，schema 版本: {runtime.db.schema_version()}")
    repo = runtime.repo
    if not repo.list_rules():
        repo.create_rule(
            {
                "name": "学校通知提升为高优先级",
                "priority": 20,
                "condition": {"keywords": ["学校", "老师", "教务", "报名", "考试", "作业"]},
                "action": {"priority": "high", "category": "学校通知"},
            }
        )
        repo.create_rule(
            {
                "name": "紧急关键词",
                "priority": 10,
                "condition": {"keywords": ["紧急", "立即", "截止", "务必", "最后一天"]},
                "action": {"priority": "urgent", "urgency": "urgent"},
            }
        )
        repo.create_rule(
            {
                "name": "低价值寒暄不提醒",
                "priority": 200,
                "condition": {"keywords": ["收到", "好的", "谢谢"]},
                "action": {"priority": "low", "notify": False},
            }
        )
        print("已写入 3 条默认规则。")
    print("下一步: python -m msgdesk serve")
    return 0


def cmd_config_check(config: AppConfig) -> int:
    config.ensure_dirs()
    print(f"配置: {config.config_path or '默认配置'}")
    print(f"数据目录: {config.data_dir}")
    print(f"日报目录: {config.digest_dir}")
    print(f"监听: {config.system.bind_host}:{config.system.port}")
    print("配置检查通过。")
    return 0


def cmd_serve(
    runtime: Runtime,
    *,
    no_scheduler: bool = False,
    open_browser: bool = False,
    desktop: bool = False,
) -> int:
    from .api.server import create_server

    config = runtime.config
    token = runtime.ensure_api_token()
    try:
        server = create_server(runtime)
    except OSError:
        message = (
            f"端口 {config.system.port} 已被占用，可能已经有一个 MsgDesk 在运行。\n"
            "关掉原来的窗口再试，或修改 config.json 里的 system.port。"
        )
        print(message, flush=True)
        _fatal_window(message)
        return 1
    if not no_scheduler:
        runtime.start_background()
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    # Background ``start`` redirects this process' stdout to service.log. Keep
    # the usable token in the parent command output, but never persist it in
    # the long-lived service log unless explicitly requested for local debug.
    show_token = bool(token) and (
        sys.stdout.isatty() or os.environ.get("MSGDESK_SHOW_TOKEN") == "1"
    )
    if show_token:
        url += f"?token={token}"
    print(f"工作台地址: {url}", flush=True)
    if token and not show_token:
        print(f"访问令牌见: {config.data_dir / 'api_token.txt'}", flush=True)
    print(f"数据库: {config.db_path}", flush=True)
    print("按 Ctrl+C 停止。", flush=True)
    if desktop:
        try:
            import webview
        except Exception as exc:  # noqa: BLE001 - fall back to the browser
            print(f"桌面窗口不可用（{exc}），改用浏览器打开。", flush=True)
            desktop = False
            open_browser = True
    if desktop:
        import threading

        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True
        )
        thread.start()
        if token:
            target = f"http://{host}:{port}/?token={token}&desktop=1"
        else:
            target = f"http://{host}:{port}/?desktop=1"
        webview.create_window(
            "MsgDesk 个人消息工作台",
            target,
            width=1440,
            height=920,
            min_size=(1100, 700),
        )
        try:
            webview.start()
        finally:
            runtime.stop()
            server.shutdown()
            server.server_close()
        return 0
    if open_browser:
        import threading
        import webbrowser

        target = f"http://{host}:{port}/?token={token}" if token else f"http://{host}:{port}/"
        threading.Timer(1.2, lambda: webbrowser.open(target)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        server.server_close()
    return 0


def cmd_napcat(action: str, uin: str | None) -> int:
    from .services import qq_bridge

    try:
        if action == "install":
            path = qq_bridge.install()
            print(f"QQ 桥已安装: {path}")
        elif action == "configure":
            changed = qq_bridge.configure_onebot()
            print("已开启 127.0.0.1:3000 接口" if changed else "接口已配置")
        elif action == "start":
            qq_bridge.start(uin)
            print("QQ 桥已启动")
        elif action == "stop":
            qq_bridge.stop()
            print("QQ 桥已停止")
        elif action == "relogin":
            qq_bridge.relogin()
            print("已重启，等待扫码")
        else:
            print(json.dumps(qq_bridge.state(), ensure_ascii=False, indent=2))
    except (RuntimeError, OSError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_sync(runtime: Runtime, source_id: str | None, max_items: int) -> int:
    sources = (
        [runtime.repo.get_source(source_id)] if source_id else runtime.repo.enabled_sources()
    )
    if not sources or sources[0] is None:
        print("没有可同步的来源。")
        return 1
    failures = 0
    for source in sources:
        assert source is not None
        try:
            result = runtime.sync_service.sync_source(source["id"], max_items=max_items)
            print(
                f"[{source['name']}] 获取 {result['fetched']}，新增 {result['inserted']}，"
                f"重复 {result['duplicated']}，合并 {result['merged']}，失败 {result['failed']}"
            )
            for warning in result["warnings"]:
                print(f"  警告: {warning}")
        except AppError as exc:
            failures += 1
            print(f"[{source['name']}] 失败: {exc.code} {exc.message}")
    print(f"处理队列: 处理 {runtime.drain()} 个任务")
    return 1 if failures else 0


def cmd_digest(runtime: Runtime, date_text: str | None) -> int:
    summary = runtime.digest_service.generate(date_text)
    print(
        f"日报 {summary['date']} 已生成：{summary['message_count']} 条消息，"
        f"紧急 {summary['urgent_count']} 条"
    )
    print(f"文件: {summary['file_path']}")
    return 0


def cmd_reprocess(runtime: Runtime, source_id: str | None, limit: int) -> int:
    messages = runtime.repo.list_messages(limit=limit, source_id=source_id)
    if not messages:
        print("没有可处理的消息。")
        return 0
    failures = 0
    for message in messages:
        try:
            runtime.processing_service.process(message["id"], force=True)
        except AppError as exc:
            failures += 1
            print(f"[{message['id'][:8]}] 失败: {exc.code} {exc.message}")
    print(f"已处理 {len(messages) - failures} 条，失败 {failures} 条")
    print(f"任务队列: 处理 {runtime.drain()} 个任务")
    return 1 if failures else 0


def cmd_status(runtime: Runtime) -> int:
    stats = runtime.repo.stats()
    print("运行状态")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    digests = runtime.repo.list_digests(3)
    if digests:
        print("最近日报:")
        for digest in digests:
            print(f"  {digest['date']}  {digest['message_count']} 条  {digest['file_path']}")
    jobs = runtime.repo.list_jobs(5)
    if jobs:
        print("最近任务:")
        for job in jobs:
            print(f"  {job['type']:<18} {job['status']:<14} {job.get('error_code') or ''}")
    return 0


def cmd_doctor(runtime: Runtime) -> int:
    checks: list[tuple[str, bool, str]] = []
    config = runtime.config
    for name, path in (
        ("data_dir", config.data_dir),
        ("digest_dir", config.digest_dir),
        ("raw_dir", config.data_dir / "raw"),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            checks.append((name, True, str(path)))
        except OSError as exc:
            checks.append((name, False, str(exc)))
    checks.append(("schema", runtime.db.schema_version() >= 1,
                   f"version={runtime.db.schema_version()}"))
    checks.append(("model", True, f"{runtime.model.name}/{runtime.model.model_name}"))
    for channel in config.notifications.channels:
        checks.append((
            f"channel:{channel.id}",
            channel.type in (
                "mock", "file", "webhook", "pushplus", "serverchan",
                "wecom_bot", "bark",
            ),
            channel.type,
        ))
    from .adapters.registry import ADAPTERS

    checks.append(("adapters", bool(ADAPTERS), f"{len(ADAPTERS)} 个适配器"))
    failures = 0
    print("自检结果")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        failures += 0 if ok else 1
    print("全部通过。" if failures == 0 else f"{failures} 项失败。")
    return 1 if failures else 0


def cmd_demo(runtime: Runtime) -> int:
    config = runtime.config
    demo_dir = config.data_dir / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    feed_path = demo_dir / "feed.xml"
    zone = local_zone(config.system.timezone)
    now = utc_now().astimezone(zone)

    def stamp(hours_ago: int) -> str:
        return (now - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S %z")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>MsgDesk 演示资讯</title>
    <link>https://example.com/demo</link>
    <description>用于验证采集、去重、处理和日报的固定夹具</description>
    <item>
      <title>教务处关于期中考试报名的通知</title>
      <link>https://example.com/demo/exam</link>
      <guid>demo-exam-2026</guid>
      <pubDate>{stamp(2)}</pubDate>
      <description>请各位同学于 {now.year}-{now.month:02d}-{min(now.day + 2, 28):02d} 18:00 前完成期中考试报名，逾期不再受理。报名入口见教务系统。</description>
    </item>
    <item>
      <title>开源项目发布新的本地检索方案</title>
      <link>https://example.com/demo/tech</link>
      <guid>demo-tech-2026</guid>
      <pubDate>{stamp(5)}</pubDate>
      <description>该项目使用 SQLite 和轻量索引实现本地全文检索，支持中文分词和增量更新，适合个人知识库场景。</description>
    </item>
    <item>
      <title>周末社区活动通知</title>
      <link>https://example.com/demo/life</link>
      <guid>demo-life-2026</guid>
      <pubDate>{stamp(8)}</pubDate>
      <description>本周六上午十点在社区中心举办读书交流活动，感兴趣的同学可以参加，无需报名。</description>
    </item>
  </channel>
</rss>
"""
    feed_path.write_text(feed, encoding="utf-8")
    existing = [s for s in runtime.repo.list_sources() if s["name"] == "演示 RSS 资讯"]
    if existing:
        source = existing[0]
        print(f"复用演示来源: {source['id']}")
    else:
        source = runtime.repo.create_source({
            "type": "rss",
            "name": "演示 RSS 资讯",
            "adapter_type": "rss",
            "config": {"url": feed_path.as_uri(), "max_items": 20},
            "default_category": "其他",
            "default_priority": "normal",
            "interval_seconds": 600,
        })
        print(f"已创建演示来源: {source['id']}")
    result = runtime.sync_service.sync_source(source["id"])
    print(f"同步结果: 新增 {result['inserted']}，重复 {result['duplicated']}")
    processed = runtime.drain()
    print(f"已处理任务: {processed}")
    summary = runtime.digest_service.generate()
    print(f"日报: {summary['file_path']}（{summary['message_count']} 条）")
    print(f"工作台: 运行 python -m msgdesk serve 后打开 http://127.0.0.1:"
          f"{config.system.port}/")
    return 0


def cmd_backup(runtime: Runtime) -> int:
    config = runtime.config
    stamp = utc_now().astimezone(local_zone(config.system.timezone)).strftime("%Y%m%d-%H%M%S")
    target = config.data_dir.parent / "backups" / stamp
    target.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(config.db_path) as source, sqlite3.connect(target / "messages.db") as dest:
        source.backup(dest)
    if config.config_path and config.config_path.exists():
        shutil.copy2(config.config_path, target / "config.json")
    if config.digest_dir.exists():
        shutil.copytree(config.digest_dir, target / "digests")
    print(f"备份完成: {target}")
    return 0


def cmd_start(config: AppConfig) -> int:
    pid_file = config.data_dir / "msgdesk.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            print(f"服务已在运行，PID {pid}")
            return 1
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)
    log_path = config.data_dir / "logs" / "service.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    command = [sys.executable, "-u", "-m", "msgdesk"]
    if config.config_path:
        command += ["--config", str(config.config_path)]
    command.append("serve")
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(config.base_dir),
            stdout=log_file,
            stderr=log_file,
            creationflags=flags,
            close_fds=True,
        )
    pid_file.write_text(str(process.pid), encoding="utf-8")
    print(f"已后台启动，PID {process.pid}")
    print(f"日志: {log_path}")
    token_file = config.data_dir / "api_token.txt"
    for _ in range(20):
        if token_file.exists():
            break
        time.sleep(0.2)
    token = config.system.api_token
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
    print(f"地址: http://{config.system.bind_host}:{config.system.port}/"
          + (f"?token={token}" if token else ""))
    if config.system.require_auth and not token:
        print(f"访问令牌见: {token_file}")
    return 0


def cmd_stop(config: AppConfig) -> int:
    pid_file = config.data_dir / "msgdesk.pid"
    if not pid_file.exists():
        print("没有找到运行中的服务。")
        return 1
    try:
        pid = int(pid_file.read_text().strip())
        token_file = config.data_dir / "api_token.txt"
        token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
        stopped = False
        try:
            request = urllib.request.Request(
                f"http://{config.system.bind_host}:{config.system.port}/api/admin/shutdown",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    **({"X-API-Token": token} if token else {}),
                },
                method="POST",
            )
            urllib.request.urlopen(request, timeout=3).read()
            for _ in range(25):
                try:
                    os.kill(pid, 0)
                except OSError:
                    stopped = True
                    break
                time.sleep(0.2)
        except (OSError, urllib.error.URLError):
            stopped = False
        if not stopped:
            os.kill(pid, signal.SIGTERM)
        print(f"已停止 PID {pid}")
    except (OSError, ValueError) as exc:
        print(f"停止失败: {exc}")
        return 1
    finally:
        pid_file.unlink(missing_ok=True)
    return 0

