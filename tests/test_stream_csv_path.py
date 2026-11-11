"""Unit tests for continuous CSV stream path naming."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from iograph.services.csv_stream import stream_csv_path, unique_csv_path


class TestStreamCsvPath(unittest.TestCase):
    def test_stream_csv_path_absolute_start_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            started = datetime(2026, 11, 9, 12, 34, 12)
            path = stream_csv_path(root, started_at=started)
            self.assertEqual(path, root / "IOGraphica - 2026-11-09_12-34-12.csv")
            self.assertNotIn(":", path.name)

    def test_stream_csv_path_collision_allocates_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            started = datetime(2026, 11, 9, 12, 34, 12)
            first = stream_csv_path(root, started_at=started)
            first.write_text("x,y,time\n", encoding="utf-8")
            second = stream_csv_path(root, started_at=started)
            self.assertEqual(second, root / "IOGraphica - 2026-11-09_12-34-12_001.csv")

    def test_unique_csv_path_increments_until_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "IOGraphica - 2026-01-01_00-00-00.csv"
            base.write_text("x,y,time\n", encoding="utf-8")
            occupied = root / "IOGraphica - 2026-01-01_00-00-00_001.csv"
            occupied.write_text("x,y,time\n", encoding="utf-8")
            free = unique_csv_path(base)
            self.assertEqual(free, root / "IOGraphica - 2026-01-01_00-00-00_002.csv")


if __name__ == "__main__":
    unittest.main()
