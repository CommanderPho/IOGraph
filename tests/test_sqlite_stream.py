"""Unit tests for SQLite WAL mouse stream writer and CSV export bridge."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from iograph.services.sqlite_stream import (
    SqliteStreamWriter,
    default_sqlite_path,
    export_session_csv_text,
    wall_us_now,
)


class TestSqliteStreamWriter(unittest.TestCase):
    def _wait_session(self, writer: SqliteStreamWriter, timeout_s: float = 2.0) -> int:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            sid = writer.session_id
            if sid is not None:
                return int(sid)
            time.sleep(0.01)
        ## END while time.monotonic() < deadline....

        self.fail("SQLite writer session_id was not assigned")

    def test_default_sqlite_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(default_sqlite_path(root), root / "iograph_mouse.sqlite")

    def test_idle_compaction_keeps_one_idle_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "iograph_mouse.sqlite"
            writer = SqliteStreamWriter()
            writer.start(db_path, started_at_us=1_000_000)
            sid = self._wait_session(writer)
            base_wall = wall_us_now()
            writer.accept(10.0, 20.0, base_wall, 0, appended=True)
            for i in range(200):
                writer.accept(None, None, base_wall + i, 1_000 + i * 1000, appended=False)
            ## END for i in range(200)....

            writer.flush_pending_idle()
            writer.accept(11.0, 21.0, base_wall + 5000, 250_000, appended=True)
            writer.stop(end_session=True)
            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute(
                    "SELECT x, y, t_mono_us FROM samples WHERE session_id = ? ORDER BY rowid",
                    (sid,),
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0][0], 10)
            self.assertEqual(rows[0][1], 20)
            self.assertIsNone(rows[1][0])
            self.assertIsNone(rows[1][1])
            self.assertEqual(rows[2][0], 11)
            self.assertEqual(rows[2][1], 21)

    def test_csv_export_relative_ms_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "iograph_mouse.sqlite"
            writer = SqliteStreamWriter()
            writer.start(db_path, started_at_us=2_000_000)
            sid = self._wait_session(writer)
            writer.accept(100.0, 200.0, wall_us_now(), 55_000, appended=True)
            writer.accept(None, None, wall_us_now(), 890_000, appended=True)
            writer.accept(101.0, 201.0, wall_us_now(), 1_000_000, appended=True)
            writer.flush_pending_idle()
            writer.stop(end_session=True)
            text = export_session_csv_text(db_path, sid)
            self.assertEqual(
                text,
                "x,y,time\n100,200,55\n,,890\n101,201,1000\n",
            )

    def test_reopen_preserves_prior_session_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "iograph_mouse.sqlite"
            writer = SqliteStreamWriter()
            writer.start(db_path, started_at_us=3_000_000)
            sid = self._wait_session(writer)
            writer.accept(1.0, 2.0, wall_us_now(), 10_000, appended=True)
            writer.stop(end_session=True)

            writer2 = SqliteStreamWriter()
            writer2.start(db_path, started_at_us=4_000_000)
            sid2 = self._wait_session(writer2)
            self.assertNotEqual(sid, sid2)
            writer2.accept(3.0, 4.0, wall_us_now(), 20_000, appended=True)
            writer2.stop(end_session=True)

            conn = sqlite3.connect(str(db_path))
            try:
                sessions = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
                rows = conn.execute("SELECT session_id, x, y FROM samples ORDER BY rowid").fetchall()
            finally:
                conn.close()
            self.assertEqual(len(sessions), 2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0], (sid, 1, 2))
            self.assertEqual(rows[1], (sid2, 3, 4))


if __name__ == "__main__":
    unittest.main()
