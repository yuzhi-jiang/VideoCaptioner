"""watch command — monitor a directory and automatically process new videos.

Sub-commands
------------
  watch <dir> [options]       Start watching (foreground or daemon)
  watch stop                  Stop a running daemon
  watch status                Show queue + daemon status
  watch logs [--last N]       Tail the daemon log file
  watch bump <file> [--priority N]  Raise task priority
  watch reset <file>          Reset a task to pending

Interactive wizard
------------------
  watch --interactive         Step-by-step configuration prompt
  watch                       (no directory) — triggers wizard automatically
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from argparse import Namespace
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Wizard helpers ─────────────────────────────────────────────────────────────


def _wizard() -> dict:
    """Interactive configuration wizard.  Returns a config dict."""
    sep = "=" * 60
    print(sep)
    print("  VideoCaptioner — Watch 自动化配置向导")
    print(sep)
    print()

    # [1/5] Directory
    while True:
        raw = input("[1/5] 视频目录路径: ").strip()
        if raw:
            p = Path(raw).expanduser()
            if p.is_dir():
                directory = str(p.resolve())
                break
            print(f"      目录不存在: {p}")
        else:
            print("      请输入一个有效的目录路径")

    # [2/5] Steps
    step_menu = [
        ("transcribe", "转录 (transcribe)"),
        ("optimize",   "优化 (optimize)"),
        ("translate",  "翻译 (translate)"),
        ("synthesize", "合成 (synthesize)"),
    ]
    print()
    print("[2/5] 选择处理步骤（空格分隔序号，回车全选）:")
    for i, (_, label) in enumerate(step_menu, 1):
        print(f"  {i}) {label}")
    raw = input("> ").strip()
    if not raw:
        steps_keys = [k for k, _ in step_menu]
    else:
        idxs = raw.split()
        steps_keys = []
        for idx in idxs:
            try:
                n = int(idx) - 1
                if 0 <= n < len(step_menu):
                    steps_keys.append(step_menu[n][0])
            except ValueError:
                pass
        if not steps_keys:
            steps_keys = [k for k, _ in step_menu]

    # [3/5] Condition
    cond_menu = [
        ("always",  "always  — 总是翻译"),
        ("non-cjk", "non-cjk — 非中日韩"),
        ("non-zh",  "non-zh  — 非中文"),
        ("is-en",   "is-en   — 仅英文"),
        ("custom",  "自定义  — 输入表达式"),
    ]
    print()
    print("[3/5] 翻译触发条件:")
    for i, (_, label) in enumerate(cond_menu, 1):
        print(f"  {i}) {label}")
    raw = input("> ").strip()
    try:
        n = int(raw) - 1
        if n == len(cond_menu) - 1:  # custom
            condition = input("      输入条件表达式: ").strip() or "always"
        elif 0 <= n < len(cond_menu) - 1:
            condition = cond_menu[n][0]
        else:
            condition = "always"
    except ValueError:
        condition = "always"

    # [4/5] Target language
    print()
    raw = input("[4/5] 翻译目标语言 [zh-Hans]: ").strip()
    target_lang = raw if raw else "zh-Hans"

    # [5/5] Run mode
    print()
    print("[5/5] 运行模式:")
    print("  1) 前台运行（实时输出日志）")
    print("  2) 后台运行（daemon，日志写入文件）")
    raw = input("> ").strip()
    daemon = raw == "2"

    # Summary
    from videocaptioner.automation.policy import steps_summary
    print()
    print("-" * 60)
    print("将以以下配置启动:")
    print(f"  目录:   {directory}")
    print(f"  步骤:   {steps_summary(steps_keys)}")
    print(f"  条件:   {condition}")
    print(f"  目标:   {target_lang}")
    print(f"  模式:   {'daemon' if daemon else '前台'}")

    # Optionally save to config
    raw = input("保存为配置文件? [y/N]: ").strip().lower()
    if raw == "y":
        _save_watch_config(
            directory=directory,
            steps=steps_keys,
            condition=condition,
            target_lang=target_lang,
            daemon=daemon,
        )

    print("-" * 60)
    print("启动中...")
    print()

    return {
        "directory": directory,
        "steps": steps_keys,
        "condition": condition,
        "target_lang": target_lang,
        "daemon": daemon,
    }


def _save_watch_config(
    directory: str,
    steps: List[str],
    condition: str,
    target_lang: str,
    daemon: bool,
) -> None:
    from videocaptioner.cli.config import CONFIG_FILE, ensure_config_dir, load_config_file

    ensure_config_dir()
    existing = load_config_file()
    existing.setdefault("watch", {})
    existing["watch"].update({
        "directory": directory,
        "steps": steps,
        "condition": condition,
        "target_lang": target_lang,
        "daemon": daemon,
    })

    from videocaptioner.cli.config import _write_toml
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        _write_toml(f, existing)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    print(f"  配置已保存: {CONFIG_FILE}", file=sys.stderr)


# ── Main run() ─────────────────────────────────────────────────────────────────

_WATCH_ACTIONS = {"stop", "status", "logs", "bump", "reset"}


def run(args: Namespace, config: dict) -> int:
    from videocaptioner.cli import exit_codes as EXIT

    target = getattr(args, "target", None)
    target_file = getattr(args, "target_file", None)

    # Detect sub-command vs directory from the 'target' positional
    if target in _WATCH_ACTIONS:
        watch_action = target
        # For bump/reset, the file is in target_file
        if watch_action in ("bump", "reset"):
            args.file = target_file
    else:
        watch_action = None
        # target is either a directory path or None (wizard)
        args.directory = target

    if watch_action == "stop":
        return _cmd_stop(args)
    if watch_action == "status":
        return _cmd_status(args, config)
    if watch_action == "logs":
        return _cmd_logs(args)
    if watch_action == "bump":
        if not target_file:
            from videocaptioner.cli import output
            output.error("Usage: videocaptioner watch bump <file> [--priority N]")
            return EXIT.USAGE_ERROR
        return _cmd_bump(args, config)
    if watch_action == "reset":
        if not target_file:
            from videocaptioner.cli import output
            output.error("Usage: videocaptioner watch reset <file>")
            return EXIT.USAGE_ERROR
        return _cmd_reset(args, config)

    # Start watching
    interactive = getattr(args, "interactive", False)
    directory_arg = getattr(args, "directory", None)

    # Trigger wizard if no directory given or --interactive
    if interactive or not directory_arg:
        # Check if we can load from saved config instead
        saved = _load_saved_watch_config(config)
        if saved and not interactive:
            wizard_cfg = saved
            print("  使用已保存的配置。(使用 --interactive 重新配置)", file=sys.stderr)
        else:
            try:
                wizard_cfg = _wizard()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消。", file=sys.stderr)
                return EXIT.SUCCESS
    else:
        # Build config from CLI args + loaded config
        from videocaptioner.automation.policy import parse_steps, validate_condition
        raw_steps = getattr(args, "steps", None) or []
        if not raw_steps:
            raw_steps = config.get("watch", {}).get("steps", ["transcribe", "optimize", "translate"])
        try:
            steps = parse_steps(raw_steps)
        except ValueError as exc:
            from videocaptioner.cli import output
            output.error(str(exc))
            return EXIT.USAGE_ERROR

        condition = getattr(args, "condition", None) or config.get("watch", {}).get("condition", "always")
        try:
            validate_condition(condition)
        except ValueError as exc:
            from videocaptioner.cli import output
            output.error(str(exc))
            return EXIT.USAGE_ERROR

        target_lang = (
            getattr(args, "target_language", None)
            or config.get("watch", {}).get("target_lang")
            or config.get("translate", {}).get("target_language", "zh-Hans")
        )

        wizard_cfg = {
            "directory": directory_arg,
            "steps": steps,
            "condition": condition,
            "target_lang": target_lang,
            "daemon": getattr(args, "daemon", False),
        }

    return _start_watch(args, config, wizard_cfg)


def _load_saved_watch_config(config: dict) -> Optional[dict]:
    """Return the [watch] section from config if it has a directory set."""
    w = config.get("watch", {})
    if w.get("directory"):
        return {
            "directory": w["directory"],
            "steps": w.get("steps", ["transcribe", "optimize", "translate"]),
            "condition": w.get("condition", "always"),
            "target_lang": w.get("target_lang", "zh-Hans"),
            "daemon": w.get("daemon", False),
        }
    return None


def _start_watch(args: Namespace, config: dict, wizard_cfg: dict) -> int:
    from videocaptioner.automation import daemon as dmn
    from videocaptioner.automation import db as taskdb
    from videocaptioner.automation.scanner import watch_loop
    from videocaptioner.automation.worker import run_worker
    from videocaptioner.cli import exit_codes as EXIT

    directory = Path(wizard_cfg["directory"])
    if not directory.is_dir():
        from videocaptioner.cli import output
        output.error(f"Directory not found: {directory}")
        return EXIT.FILE_NOT_FOUND

    steps = wizard_cfg["steps"]
    condition = wizard_cfg["condition"]
    target_lang = wizard_cfg["target_lang"]
    use_daemon = wizard_cfg.get("daemon", False)

    # Allow explicit --foreground to override daemon mode
    if getattr(args, "foreground", False):
        use_daemon = False

    # Merge target_lang into config so the worker uses it
    config.setdefault("translate", {})["target_language"] = target_lang

    # DB path
    db_path_arg = getattr(args, "db", None) or config.get("watch", {}).get("db_path", "")
    if db_path_arg:
        db_path = Path(db_path_arg)
    else:
        from platformdirs import user_data_dir
        db_path = Path(user_data_dir("videocaptioner")) / "watch.db"

    log_file_arg = getattr(args, "log_file", None) or config.get("watch", {}).get("log_file", "")
    log_file = Path(log_file_arg) if log_file_arg else dmn.default_log_file()

    pid_file_arg = getattr(args, "pid_file", None)
    pid_file = Path(pid_file_arg) if pid_file_arg else dmn.default_pid_file()

    interval = int(
        getattr(args, "interval", None)
        or config.get("watch", {}).get("interval", 60)
    )
    output_dir = getattr(args, "output", None) or config.get("watch", {}).get("output_dir", "")

    # Daemonize if requested
    if use_daemon:
        if dmn.is_running(pid_file):
            from videocaptioner.cli import output
            output.warn(f"Daemon already running (PID {dmn.read_pid(pid_file)})")
            return EXIT.GENERAL_ERROR
        dmn.daemonize(log_file, pid_file)
        # After daemonize on Unix, we continue here as the daemon child.
        # On Windows, daemonize() calls sys.exit() in the parent, so the child
        # is a new process started with --foreground and we never reach this line
        # in the parent.

    # Setup logging for daemon or foreground
    _configure_logging(use_daemon, log_file)

    logger.info(
        "Watch started: dir=%s steps=%s condition=%r interval=%d daemon=%s",
        directory, steps, condition, interval, use_daemon,
    )

    conn = taskdb.open_db(db_path)
    stop_event = threading.Event()

    # Scanner thread
    scanner_thread = threading.Thread(
        target=watch_loop,
        args=(directory, conn, steps, interval, stop_event),
        daemon=True,
        name="scanner",
    )
    scanner_thread.start()

    # Worker runs in the main (daemon) thread
    try:
        run_worker(conn, config, condition=condition, output_dir=output_dir or None)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        scanner_thread.join(timeout=5)
        if use_daemon:
            dmn.remove_pid(pid_file)
        logger.info("Watch stopped.")

    return EXIT.SUCCESS


def _configure_logging(daemon_mode: bool, log_file: Path) -> None:
    """Set up root logger: file handler for daemon, stream handler for foreground."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if daemon_mode:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(log_file), encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root.addHandler(handler)


# ── Sub-command handlers ────────────────────────────────────────────────────────


def _get_conn(args: Namespace, config: dict):
    from platformdirs import user_data_dir

    from videocaptioner.automation import db as taskdb

    db_path_arg = getattr(args, "db", None) or config.get("watch", {}).get("db_path", "")
    if db_path_arg:
        db_path = Path(db_path_arg)
    else:
        db_path = Path(user_data_dir("videocaptioner")) / "watch.db"
    return taskdb.open_db(db_path)


def _cmd_stop(args: Namespace) -> int:
    from videocaptioner.automation import daemon as dmn
    from videocaptioner.cli import exit_codes as EXIT
    from videocaptioner.cli import output

    pid_file_arg = getattr(args, "pid_file", None)
    pid_file = Path(pid_file_arg) if pid_file_arg else dmn.default_pid_file()

    if not dmn.is_running(pid_file):
        output.warn("No daemon is running.")
        return EXIT.SUCCESS
    pid = dmn.read_pid(pid_file)
    if dmn.stop_daemon(pid_file):
        output.success(f"Daemon stopped (was PID {pid})")
    else:
        output.error("Failed to stop daemon")
        return EXIT.GENERAL_ERROR
    return EXIT.SUCCESS


def _cmd_status(args: Namespace, config: dict) -> int:
    from videocaptioner.automation import daemon as dmn
    from videocaptioner.automation import db as taskdb
    from videocaptioner.cli import exit_codes as EXIT

    pid_file = Path(getattr(args, "pid_file", None) or dmn.default_pid_file())

    print("=== Daemon ===")
    if dmn.is_running(pid_file):
        print(f"  Running  (PID {dmn.read_pid(pid_file)})")
    else:
        print("  Stopped")

    print()
    print("=== Task Queue ===")
    conn = _get_conn(args, config)
    summary = taskdb.get_status_summary(conn)
    if not summary:
        print("  (empty)")
    else:
        for status, count in sorted(summary.items()):
            print(f"  {status:<12} {count}")

    return EXIT.SUCCESS


def _cmd_logs(args: Namespace) -> int:
    from videocaptioner.automation import daemon as dmn
    from videocaptioner.cli import exit_codes as EXIT
    from videocaptioner.cli import output

    log_file = dmn.default_log_file()
    if not log_file.exists():
        output.warn(f"Log file not found: {log_file}")
        return EXIT.FILE_NOT_FOUND

    last = getattr(args, "last", 0) or 0
    if last > 0:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-last:]))
    else:
        # tail -f equivalent
        with open(log_file, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # seek to end
            print(f"Following {log_file}  (Ctrl+C to stop)")
            try:
                while True:
                    line = f.readline()
                    if line:
                        print(line, end="")
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                pass

    return EXIT.SUCCESS


def _cmd_bump(args: Namespace, config: dict) -> int:
    from videocaptioner.automation import db as taskdb
    from videocaptioner.cli import exit_codes as EXIT
    from videocaptioner.cli import output

    file_path = str(Path(args.file).resolve())
    priority = getattr(args, "priority", 100)
    conn = _get_conn(args, config)
    if taskdb.bump_priority(conn, file_path, priority):
        output.success(f"Priority set to {priority} for: {file_path}")
    else:
        output.error(f"File not in task queue: {file_path}")
        return EXIT.GENERAL_ERROR
    return EXIT.SUCCESS


def _cmd_reset(args: Namespace, config: dict) -> int:
    from videocaptioner.automation import db as taskdb
    from videocaptioner.cli import exit_codes as EXIT
    from videocaptioner.cli import output

    file_path = str(Path(args.file).resolve())
    conn = _get_conn(args, config)
    if taskdb.reset_task(conn, file_path):
        output.success(f"Reset to pending: {file_path}")
    else:
        output.error(f"File not in task queue: {file_path}")
        return EXIT.GENERAL_ERROR
    return EXIT.SUCCESS
