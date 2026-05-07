"""Tests for watch command — policy, DB, parser, and sub-commands."""

import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from videocaptioner.automation import db as taskdb
from videocaptioner.automation.policy import (
    evaluate_condition,
    parse_steps,
    steps_summary,
    validate_condition,
)
from videocaptioner.cli import exit_codes as EXIT
from videocaptioner.cli.main import main


# ── Policy: parse_steps ───────────────────────────────────────────────────────

class TestParseSteps:
    def test_all_expands(self):
        steps = parse_steps(["all"])
        assert steps == ["transcribe", "optimize", "translate", "synthesize"]

    def test_canonical_order(self):
        # Regardless of input order, output is always canonical
        steps = parse_steps(["synthesize", "transcribe", "optimize"])
        assert steps == ["transcribe", "optimize", "synthesize"]

    def test_deduplication(self):
        steps = parse_steps(["transcribe", "transcribe", "translate"])
        assert steps == ["transcribe", "translate"]

    def test_all_with_extra(self):
        steps = parse_steps(["transcribe", "all"])
        assert steps == ["transcribe", "optimize", "translate", "synthesize"]

    def test_unknown_step_raises(self):
        with pytest.raises(ValueError, match="Unknown step"):
            parse_steps(["transcribe", "magic"])

    def test_empty_input(self):
        assert parse_steps([]) == []

    def test_case_insensitive(self):
        steps = parse_steps(["TRANSCRIBE", "Optimize"])
        assert steps == ["transcribe", "optimize"]


class TestStepsSummary:
    def test_arrow_joined(self):
        assert steps_summary(["transcribe", "optimize"]) == "transcribe → optimize"

    def test_empty(self):
        assert steps_summary([]) == "(none)"


# ── Policy: evaluate_condition ────────────────────────────────────────────────

class TestEvaluateCondition:
    def test_always(self):
        assert evaluate_condition("always", "en") is True
        assert evaluate_condition("always", "zh") is True

    def test_never(self):
        assert evaluate_condition("never", "en") is False

    def test_non_cjk_true(self):
        assert evaluate_condition("non-cjk", "en") is True
        assert evaluate_condition("non-cjk", "fr") is True

    def test_non_cjk_false(self):
        assert evaluate_condition("non-cjk", "zh") is False
        assert evaluate_condition("non-cjk", "ja") is False
        assert evaluate_condition("non-cjk", "ko") is False

    def test_non_zh(self):
        assert evaluate_condition("non-zh", "en") is True
        assert evaluate_condition("non-zh", "zh") is False
        assert evaluate_condition("non-zh", "zh-Hans") is False

    def test_is_en(self):
        assert evaluate_condition("is-en", "en") is True
        assert evaluate_condition("is-en", "zh") is False

    def test_or_expression(self):
        assert evaluate_condition("is-en or is-ja", "en") is True
        assert evaluate_condition("is-en or is-ja", "ja") is True
        assert evaluate_condition("is-en or is-ja", "fr") is False

    def test_and_expression(self):
        # non-zh AND non-cjk: both must be true; 'en' passes both
        assert evaluate_condition("non-zh and non-cjk", "en") is True
        # 'ja' is non-zh but IS cjk → fails
        assert evaluate_condition("non-zh and non-cjk", "ja") is False

    def test_empty_expression_defaults_true(self):
        assert evaluate_condition("", "en") is True

    def test_case_insensitive(self):
        assert evaluate_condition("NON-CJK", "en") is True

    def test_lang_with_region(self):
        # zh-Hans should match non-zh = False
        assert evaluate_condition("non-zh", "zh-Hans") is False

    def test_invalid_clause_raises(self):
        with pytest.raises(ValueError):
            evaluate_condition("bogus-clause", "en")


class TestValidateCondition:
    def test_valid_expressions(self):
        for expr in ["always", "never", "non-cjk", "non-zh", "is-en", "is-en or is-ja"]:
            validate_condition(expr)  # should not raise

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            validate_condition("garbage-condition")


# ── DB ────────────────────────────────────────────────────────────────────────

class TestDatabase:
    @pytest.fixture
    def conn(self, tmp_path):
        db_path = tmp_path / "test.db"
        c = taskdb.open_db(db_path)
        yield c
        c.close()

    def test_add_task(self, conn):
        task_id = taskdb.add_task(conn, "/foo/bar.mp4", ["transcribe"])
        assert task_id is not None

    def test_duplicate_ignored(self, conn):
        taskdb.add_task(conn, "/foo/bar.mp4", ["transcribe"])
        second = taskdb.add_task(conn, "/foo/bar.mp4", ["transcribe"])
        assert second is None

    def test_is_known(self, conn):
        assert taskdb.is_known(conn, "/foo/bar.mp4") is False
        taskdb.add_task(conn, "/foo/bar.mp4", ["transcribe"])
        assert taskdb.is_known(conn, "/foo/bar.mp4") is True

    def test_get_next_task(self, conn):
        taskdb.add_task(conn, "/a.mp4", ["transcribe"])
        task = taskdb.get_next_task(conn)
        assert task is not None
        assert task["file_path"] == "/a.mp4"
        # The returned dict reflects the row before the status update,
        # so status is still 'pending' in the returned snapshot.
        assert task["status"] == "pending"
        # After fetch, the DB row is marked as processing
        row = conn.execute("SELECT status FROM tasks WHERE file_path=?", ("/a.mp4",)).fetchone()
        assert row["status"] == "processing"

    def test_mark_done(self, conn):
        tid = taskdb.add_task(conn, "/a.mp4", ["transcribe"])
        taskdb.mark_done(conn, tid)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "done"

    def test_mark_error(self, conn):
        tid = taskdb.add_task(conn, "/a.mp4", ["transcribe"])
        taskdb.mark_error(conn, tid, "something went wrong")
        row = conn.execute("SELECT status, error_msg FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "error"
        assert "something went wrong" in row["error_msg"]

    def test_reset_task(self, conn):
        tid = taskdb.add_task(conn, "/a.mp4", ["transcribe"])
        taskdb.mark_error(conn, tid, "oops")
        ok = taskdb.reset_task(conn, "/a.mp4")
        assert ok is True
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "pending"

    def test_priority_ordering(self, conn):
        taskdb.add_task(conn, "/low.mp4", ["transcribe"], priority=10)
        taskdb.add_task(conn, "/high.mp4", ["transcribe"], priority=90)
        task = taskdb.get_next_task(conn)
        assert task["file_path"] == "/high.mp4"

    def test_bump_priority(self, conn):
        taskdb.add_task(conn, "/a.mp4", ["transcribe"], priority=10)
        ok = taskdb.bump_priority(conn, "/a.mp4", 200)
        assert ok is True
        row = conn.execute("SELECT priority FROM tasks WHERE file_path=?", ("/a.mp4",)).fetchone()
        assert row["priority"] == 200

    def test_status_summary(self, conn):
        taskdb.add_task(conn, "/a.mp4", ["transcribe"])
        taskdb.add_task(conn, "/b.mp4", ["transcribe"])
        tid = taskdb.add_task(conn, "/c.mp4", ["transcribe"])
        taskdb.mark_done(conn, tid)
        summary = taskdb.get_status_summary(conn)
        assert summary.get("pending", 0) == 2
        assert summary.get("done", 0) == 1


# ── CLI parser ────────────────────────────────────────────────────────────────

class TestWatchParser:
    def test_watch_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["watch", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "watch" in out.lower()

    def test_watch_stop_subcommand(self, tmp_path):
        # stop with no daemon running should succeed (warns but exits 0)
        result = main(["watch", "stop"])
        assert result == EXIT.SUCCESS

    def test_watch_status_subcommand(self):
        result = main(["watch", "status"])
        assert result == EXIT.SUCCESS

    def test_watch_logs_missing_file(self):
        # Log file doesn't exist → FILE_NOT_FOUND
        result = main(["watch", "logs", "--last", "10"])
        assert result == EXIT.FILE_NOT_FOUND

    def test_watch_nonexistent_dir(self, tmp_path):
        result = main(["watch", str(tmp_path / "nonexistent")])
        assert result == EXIT.FILE_NOT_FOUND

    def test_watch_invalid_steps(self, tmp_path):
        result = main(["watch", str(tmp_path), "--steps", "bogus"])
        assert result == EXIT.USAGE_ERROR

    def test_watch_invalid_condition(self, tmp_path):
        result = main(["watch", str(tmp_path), "--steps", "transcribe",
                       "--condition", "invalid-expr"])
        assert result == EXIT.USAGE_ERROR

    def test_watch_in_help(self, capsys):
        """'watch' must appear in the top-level help."""
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "watch" in out
