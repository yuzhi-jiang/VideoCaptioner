"""Directory scanner — finds new video files and adds them to the task queue.

Supported extensions are a broad set; the same list used by the process pipeline.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import List, Set

from videocaptioner.automation import db as taskdb

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS: Set[str] = {
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "ts",
    "mts", "m2ts", "mpg", "mpeg", "3gp", "3g2", "f4v", "rm", "rmvb",
    "vob", "ogv", "divx", "xvid",
}


def scan_directory(
    directory: Path,
    conn: sqlite3.Connection,
    steps: List[str],
    recursive: bool = True,
) -> int:
    """Scan *directory* for video files not yet in the DB.

    Returns the number of newly added tasks.
    """
    added = 0
    glob_pattern = "**/*" if recursive else "*"

    for path in directory.glob(glob_pattern):
        if not path.is_file():
            continue
        ext = path.suffix.lstrip(".").lower()
        if ext not in VIDEO_EXTENSIONS:
            continue
        abs_path = str(path.resolve())
        task_id = taskdb.add_task(conn, abs_path, steps)
        if task_id is not None:
            added += 1
            logger.info("Queued: %s", abs_path)

    return added


def watch_loop(
    directory: Path,
    conn: sqlite3.Connection,
    steps: List[str],
    interval: int,
    stop_event,
    recursive: bool = True,
) -> None:
    """Continuously scan *directory* every *interval* seconds until *stop_event* is set."""
    logger.info("Watching: %s (interval=%ds)", directory, interval)
    while not stop_event.is_set():
        try:
            n = scan_directory(directory, conn, steps, recursive=recursive)
            if n:
                logger.info("Found %d new video(s)", n)
        except Exception:
            logger.exception("Error during directory scan")
        if interval <= 0:
            break  # one-shot mode
        stop_event.wait(interval)
