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

from validation.python_source import parse_python  # noqa: E402


class BomParsingTests(unittest.TestCase):
    """A leading UTF-8 BOM is something Python's own import machinery already
    tolerates, so a source file that has one still runs correctly in the real
    container -- `ast.parse` on a plain-utf-8-decoded string does not tolerate
    the literal U+FEFF character, though, so this used to surface as a bogus
    V006 'the entrypoint does not parse' on an otherwise-valid file."""

    def test_a_leading_bom_does_not_break_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "entrypoint.py")
            with open(path, "wb") as handle:
                handle.write(b"\xef\xbb\xbf" + b"class Agent:\n    pass\n")
            tree, error = parse_python(path)
        self.assertIsNone(error)
        self.assertIsNotNone(tree)

    def test_a_file_without_a_bom_is_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "entrypoint.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("class Agent:\n    pass\n")
            tree, error = parse_python(path)
        self.assertIsNone(error)
        self.assertIsNotNone(tree)

    def test_a_genuine_syntax_error_still_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "entrypoint.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("def broken(:\n")
            tree, error = parse_python(path)
        self.assertIsNone(tree)
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
