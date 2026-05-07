"""Daemon utilities — daemonize the process (Unix) or launch a detached subprocess (Windows).

Usage
-----
daemonize(log_file, pid_file)
    Called *in the child process* after fork to fully detach from the terminal.
    On Windows, re-launches self as a detached subprocess instead.

read_pid(pid_file) → Optional[int]
stop_daemon(pid_file) → bool
is_running(pid_file) → bool
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import List, Optional


def _data_dir() -> Path:
    """Return the per-user data directory for VideoCaptioner watch state."""
    from platformdirs import user_data_dir
    p = Path(user_data_dir("videocaptioner"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_pid_file() -> Path:
    return _data_dir() / "watch.pid"


def default_log_file() -> Path:
    return _data_dir() / "watch.log"


# ── PID file helpers ──────────────────────────────────────────────────────────

def write_pid(pid_file: Path) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))


def read_pid(pid_file: Path) -> Optional[int]:
    try:
        text = pid_file.read_text().strip()
        return int(text) if text else None
    except (FileNotFoundError, ValueError):
        return None


def remove_pid(pid_file: Path) -> None:
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


def is_running(pid_file: Path) -> bool:
    """Return True if the daemon PID is present and the process is alive."""
    pid = read_pid(pid_file)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_daemon(pid_file: Path) -> bool:
    """Send SIGTERM to the daemon.  Returns True if signal was delivered."""
    pid = read_pid(pid_file)
    if pid is None:
        return False
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=False)
        else:
            os.kill(pid, signal.SIGTERM)
        remove_pid(pid_file)
        return True
    except (ProcessLookupError, PermissionError):
        remove_pid(pid_file)
        return False


# ── Daemonize / detach ────────────────────────────────────────────────────────

def daemonize(log_file: Path, pid_file: Path) -> None:
    """Detach from terminal and run in background.

    On Unix: double-fork, close stdio, redirect to log_file, write PID file.
    On Windows: re-launch self as a detached subprocess (DETACHED_PROCESS).
    """
    if sys.platform == "win32":
        _daemonize_windows(log_file, pid_file)
    else:
        _daemonize_unix(log_file, pid_file)


def _daemonize_unix(log_file: Path, pid_file: Path) -> None:
    """Classic Unix double-fork daemonize."""
    # First fork
    try:
        pid = os.fork()
        if pid > 0:
            # Parent: exit so the child can be re-parented to init
            sys.exit(0)
    except OSError as exc:
        sys.exit(f"fork #1 failed: {exc}")

    # Decouple from parent environment
    os.setsid()
    os.umask(0)

    # Second fork
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as exc:
        sys.exit(f"fork #2 failed: {exc}")

    # We are now the daemon child
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _redirect_streams(log_file)
    write_pid(pid_file)


def _redirect_streams(log_file: Path) -> None:
    """Redirect stdin/stdout/stderr to /dev/null and log_file respectively."""
    import io
    sys.stdout.flush()
    sys.stderr.flush()

    null_fd = os.open(os.devnull, os.O_RDWR)
    log_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    os.dup2(null_fd, 0)   # stdin  → /dev/null
    os.dup2(log_fd, 1)    # stdout → log_file
    os.dup2(log_fd, 2)    # stderr → log_file

    os.close(null_fd)
    os.close(log_fd)

    # Replace sys streams so Python print() also goes to the log
    sys.stdin  = open(os.devnull, "r")   # noqa: SIM115
    new_log_fd = os.open(str(log_file), os.O_WRONLY | os.O_APPEND)
    sys.stdout = io.TextIOWrapper(       # noqa: SIM115
        open(new_log_fd, "wb"),
        line_buffering=True,
    )
    sys.stderr = sys.stdout


def _daemonize_windows(log_file: Path, pid_file: Path) -> None:
    """On Windows: re-launch self as a DETACHED_PROCESS and exit parent."""
    import subprocess

    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x00000008
    flags = CREATE_NO_WINDOW | DETACHED_PROCESS

    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(str(log_file), "a", encoding="utf-8")  # noqa: SIM115

    # Re-run with --foreground so the child doesn't fork again
    argv: List[str] = sys.argv[:]
    if "--daemon" in argv:
        argv[argv.index("--daemon")] = "--foreground"
    elif "-d" in argv:
        argv[argv.index("-d")] = "--foreground"
    else:
        argv.append("--foreground")

    proc = subprocess.Popen(
        [sys.executable] + argv[1:],
        stdout=log_handle,
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    log_handle.close()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))
    print(f"  Daemon started (PID {proc.pid})", file=sys.stderr)
    print(f"  Log: {log_file}", file=sys.stderr)
    sys.exit(0)
