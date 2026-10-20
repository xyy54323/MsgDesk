"""Command line entry points."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import PROJECT_ROOT, AppConfig, load_config
from .errors import AppError
from .runtime import Runtime

CONFIG_TEMPLATE = {
    "profile": {
        "identity": "",
        "interests": "",
        "ignore": "",
        "todo_rule": "",
        "notes": "",
    },
    "system": {
        "data_dir": "./data",
        "digest_dir": "./digests",
        "bind_host": "127.0.0.1",
        "port": 8080,
        "log_level": "INFO",
    },
    "scheduling": {
        "default_interval_minutes": 10,
        "max_parallel_jobs": 3,
        "digest_time": "22:00",
    },
    "range": {
        "start": "",
        "end": "",
    },
    "model": {
        "provider": "",
        "base_url": "",
        "model": "",
        "api_key_env": "MSGDESK_MODEL_API_KEY",
        "timeout_seconds": 60,
        "max_input_chars": 6000,
        "daily_call_limit": 500,
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

    sub.add_parser("init", help="初始化配置和数据库")

    serve = sub.add_parser("serve", help="启动 API、调度器和任务 worker")
    serve.add_argument("--no-scheduler", action="store_true", help="只启动 API 和 worker")
    serve.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    serve.add_argument("--desktop", action="store_true", help="用桌面窗口打开（WebView2）")

    sync = sub.add_parser("sync", help="立即同步来源")
    sync.add_argument("--source", help="来源 ID；省略时同步全部启用来源")
    sync.add_argument("--max-items", type=int, default=50)

    digest = sub.add_parser("digest", help="生成日报")
    digest.add_argument("--date", help="YYYY-MM-DD，默认今天")

    sub.add_parser("status", help="查看运行状态")

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
        if args.command == "status":
            return cmd_status(runtime)
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
    print("下一步: python -m msgdesk serve")
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
    print(f"工作台地址: {url}", flush=True)
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
        target = f"http://{host}:{port}/"
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

        target = f"http://{host}:{port}/"
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
