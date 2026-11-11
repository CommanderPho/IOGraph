"""Unit tests for SessionController.export_base_name absolute filename format."""

from __future__ import annotations

import unittest
from datetime import datetime

from iograph.core.session_controller import SessionController


class TestExportBaseName(unittest.TestCase):
    def test_export_base_name_absolute_start_and_duration(self) -> None:
        session = SessionController()
        session._started_at = datetime(2026, 11, 9, 12, 34, 12)
        # round(53280000 / 1000) / 3600 = 14.8 -> "14.8000h"
        name = session.export_base_name(53_280_000, app_name="IOGraphica")
        self.assertEqual(name, "IOGraphica - 2026-11-09_12-34-12_14.8000h")
        self.assertNotIn(":", name)

    def test_export_base_name_sanitizes_app_name(self) -> None:
        session = SessionController()
        session._started_at = datetime(2026, 1, 2, 3, 4, 5)
        name = session.export_base_name(3_600_000, app_name="IO/Graph:ica*")
        self.assertEqual(name, "IO-Graph-ica- - 2026-01-02_03-04-05_1.0000h")
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)
        self.assertNotIn("*", name)

    def test_export_base_name_fallback_now_shape_when_not_started(self) -> None:
        session = SessionController()
        self.assertIsNone(session._started_at)
        name = session.export_base_name(0, app_name="IOGraphica")
        self.assertRegex(
            name,
            r"^IOGraphica - \d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_0\.0000h$",
        )


if __name__ == "__main__":
    unittest.main()
