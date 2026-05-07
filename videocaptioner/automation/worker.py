"""Task worker — pulls tasks from the DB and runs the processing pipeline.

Each task specifies a list of steps (e.g. ["transcribe", "optimize", "translate"]).
The worker maps those steps to the existing CLI pipeline commands.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from argparse import Namespace
from pathlib import Path
from typing import Dict, List, Optional

from videocaptioner.automation import db as taskdb
from videocaptioner.automation.policy import evaluate_condition

logger = logging.getLogger(__name__)


class Worker:
    """Continuously fetches pending tasks from the DB and processes them."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: dict,
        condition: str = "always",
        output_dir: Optional[str] = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.conn = conn
        self.config = config
        self.condition = condition
        self.output_dir = output_dir
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Main worker loop — runs until stop() is called."""
        logger.info("Worker started")
        while not self._stop.is_set():
            task = taskdb.get_next_task(self.conn)
            if task is None:
                self._stop.wait(self.poll_interval)
                continue
            self._process_task(task)
        logger.info("Worker stopped")

    def _process_task(self, task: Dict) -> None:
        task_id = task["id"]
        file_path = task["file_path"]
        steps: List[str] = json.loads(task.get("steps", "[]"))

        logger.info("Processing [%d] %s  steps=%s", task_id, file_path, steps)

        try:
            self._run_steps(file_path, steps)
            taskdb.mark_done(self.conn, task_id)
            logger.info("Done [%d] %s", task_id, file_path)
        except Exception as exc:
            msg = str(exc)
            taskdb.mark_error(self.conn, task_id, msg)
            logger.error("Error [%d] %s: %s", task_id, file_path, msg)

    def _run_steps(self, file_path: str, steps: List[str]) -> None:
        """Execute the requested steps in canonical order."""
        path = Path(file_path)
        out_dir = Path(self.output_dir) if self.output_dir else path.parent

        subtitle_path: Optional[str] = None

        if "transcribe" in steps:
            subtitle_path = self._step_transcribe(path, out_dir)

        if subtitle_path and ("optimize" in steps or "translate" in steps):
            subtitle_path = self._step_subtitle(
                subtitle_path=subtitle_path,
                path=path,
                out_dir=out_dir,
                do_optimize="optimize" in steps,
                do_translate="translate" in steps,
            )

        if subtitle_path and "synthesize" in steps:
            self._step_synthesize(path, subtitle_path, out_dir)

    # ── individual steps ──────────────────────────────────────────────────────

    def _step_transcribe(self, path: Path, out_dir: Path) -> str:
        """Run ASR transcription.  Returns path to the produced .srt file."""
        from videocaptioner.cli.commands.transcribe import run as transcribe_run

        out_srt = str(out_dir / f"{path.stem}.srt")
        args = Namespace(
            input=str(path),
            output=out_srt,
            format="srt",
            word_timestamps=True,
            verbose=False,
            quiet=False,
            config=None,
            asr=None,
            language=None,
            fw_model=None,
            fw_device=None,
            fw_vad_method=None,
            fw_vad_threshold=None,
            fw_voice_extraction=False,
            fw_prompt=None,
            whisper_api_key=None,
            whisper_api_base=None,
            whisper_model=None,
            whisper_prompt=None,
        )
        ret = transcribe_run(args, self.config)
        if ret != 0:
            raise RuntimeError(f"Transcription failed (exit {ret})")
        return out_srt

    def _step_subtitle(
        self,
        subtitle_path: str,
        path: Path,
        out_dir: Path,
        do_optimize: bool,
        do_translate: bool,
    ) -> str:
        """Run subtitle optimization and/or translation.

        Respects the condition expression before translating.
        Returns path to the processed subtitle file.
        """
        from videocaptioner.cli.commands.subtitle import run as subtitle_run

        # Evaluate condition: should we translate?
        if do_translate:
            detected_lang = self._detect_language(subtitle_path)
            if not evaluate_condition(self.condition, detected_lang):
                logger.info(
                    "Skipping translation (condition %r not met for lang %r)",
                    self.condition,
                    detected_lang,
                )
                do_translate = False

        if not do_optimize and not do_translate:
            return subtitle_path

        out_srt = str(out_dir / f"{path.stem}_processed.srt")
        from videocaptioner.cli.config import get
        args = Namespace(
            input=subtitle_path,
            output=out_srt,
            format=get(self.config, "output.format", "srt"),
            no_optimize=not do_optimize,
            no_translate=not do_translate,
            no_split=False,
            verbose=False,
            quiet=False,
            config=None,
            api_key=None,
            api_base=None,
            model=None,
            translator=None,
            target_language=None,
            reflect=False,
            max_cjk=None,
            max_english=None,
            prompt=None,
            prompt_file=None,
            thread_num=None,
            batch_size=None,
            layout=None,
        )
        ret = subtitle_run(args, self.config)
        if ret != 0:
            raise RuntimeError(f"Subtitle processing failed (exit {ret})")
        return out_srt

    def _step_synthesize(self, path: Path, subtitle_path: str, out_dir: Path) -> None:
        """Burn subtitles into the video."""
        from videocaptioner.cli.commands.synthesize import run as synthesize_run

        out_video = str(out_dir / f"{path.stem}_captioned{path.suffix}")
        args = Namespace(
            video=str(path),
            subtitle=subtitle_path,
            output=out_video,
            subtitle_mode=None,
            quality=None,
            style=None,
            layout=None,
            format=None,
            verbose=False,
            quiet=False,
            config=None,
        )
        ret = synthesize_run(args, self.config)
        if ret != 0:
            raise RuntimeError(f"Video synthesis failed (exit {ret})")

    @staticmethod
    def _detect_language(subtitle_path: str) -> str:
        """Detect the dominant language of the subtitle file.

        Falls back to 'und' (undetermined) if detection fails.
        """
        try:
            from langdetect import detect

            text = _read_subtitle_text(subtitle_path)
            if len(text.strip()) < 20:
                return "und"
            return detect(text) or "und"
        except Exception:
            return "und"


def _read_subtitle_text(path: str, max_chars: int = 2000) -> str:
    """Extract plain text from a subtitle file (SRT/ASS/VTT) for language detection."""
    import re

    try:
        content = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    # Strip SRT timing lines and index numbers
    content = re.sub(r"\d+\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\n", "", content)
    # Strip ASS/SSA metadata and tags
    content = re.sub(r"\{[^}]*\}", "", content)
    content = re.sub(r"^(Dialogue|Style|Format|Info):.*", "", content, flags=re.MULTILINE)
    # Remove HTML-like tags
    content = re.sub(r"<[^>]+>", "", content)
    # Collapse whitespace
    content = " ".join(content.split())
    return content[:max_chars]


def run_worker(
    conn: sqlite3.Connection,
    config: dict,
    condition: str,
    output_dir: Optional[str],
) -> None:
    """Run the worker loop (blocking).  Called in main thread or daemon."""
    w = Worker(conn, config, condition=condition, output_dir=output_dir)
    try:
        w.run()
    except KeyboardInterrupt:
        pass
