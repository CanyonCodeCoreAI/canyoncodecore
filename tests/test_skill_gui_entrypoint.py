import os
import sys
import tempfile
import unittest

SKILL = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", ".claude", "skills", "porting-to-canyonos"
    )
)
sys.path.insert(0, SKILL)

from validation.core import Report  # noqa: E402
from validation.entrypoint import check_gui_entrypoint  # noqa: E402


class GuiEntrypointTests(unittest.TestCase):
    """A GUI-toolkit entrypoint imports and builds cleanly, then fails at
    container startup -- no display server exists there -- so this has to be
    caught statically or it only ever surfaces as a runtime crash."""

    def test_a_tkinter_entrypoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as source_dir:
            with open(os.path.join(source_dir, "gui.py"), "w", encoding="utf-8") as f:
                f.write("import tkinter\n\nclass Agent:\n    pass\n")
            report = Report(source_dir)
            check_gui_entrypoint(report, source_dir, "MyAgent", "gui.py")
        errors, _ = report.counts()
        self.assertEqual(errors, 1)
        self.assertEqual(report.findings[0]["check"], "V042")
        self.assertIn("tkinter", report.findings[0]["summary"])

    def test_a_headless_entrypoint_is_unaffected(self):
        with tempfile.TemporaryDirectory() as source_dir:
            with open(os.path.join(source_dir, "cli.py"), "w", encoding="utf-8") as f:
                f.write("import json\n\nclass Agent:\n    pass\n")
            report = Report(source_dir)
            check_gui_entrypoint(report, source_dir, "MyAgent", "cli.py")
        errors, _ = report.counts()
        self.assertEqual(errors, 0)

    def test_a_transitively_imported_gui_toolkit_is_still_caught(self):
        """The entrypoint itself is clean; a local helper it imports is not."""
        with tempfile.TemporaryDirectory() as source_dir:
            with open(
                os.path.join(source_dir, "helper.py"), "w", encoding="utf-8"
            ) as f:
                f.write("import PyQt5\n")
            with open(os.path.join(source_dir, "app.py"), "w", encoding="utf-8") as f:
                f.write("import helper\n\nclass Agent:\n    pass\n")
            report = Report(source_dir)
            check_gui_entrypoint(report, source_dir, "MyAgent", "app.py")
        errors, _ = report.counts()
        self.assertEqual(errors, 1)
        self.assertIn("PyQt5", report.findings[0]["summary"])


if __name__ == "__main__":
    unittest.main()
