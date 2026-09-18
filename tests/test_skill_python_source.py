import ast
import os
import sys
import unittest

SKILL = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", ".claude", "skills", "porting-to-canyonos"
    )
)
sys.path.insert(0, SKILL)

from validation.python_source import (  # noqa: E402
    dotted_import_names,
    toplevel_import_names,
)


class MainGuardImportTests(unittest.TestCase):
    """A source file the runtime imports never runs its own
    `if __name__ == "__main__":` block -- __name__ is the dotted module name,
    not "__main__", there. An import that exists only inside that block is
    dead code, not a reachable dependency; treating it as reachable turned a
    script-only helper import into a phantom missing-dependency finding."""

    def test_an_import_inside_the_guard_is_not_reachable(self):
        tree = ast.parse(
            "import os\n"
            'if __name__ == "__main__":\n'
            "    from test import pretty_print_results\n"
            "    import sys\n"
        )
        names = toplevel_import_names(tree)
        self.assertIn("os", names)
        self.assertNotIn("test", names)
        self.assertNotIn("sys", names)

    def test_dotted_names_are_pruned_the_same_way(self):
        tree = ast.parse('if __name__ == "__main__":\n    import a.b.c\nimport d.e\n')
        names = dotted_import_names(tree)
        self.assertIn("d.e", names)
        self.assertNotIn("a.b.c", names)

    def test_an_import_elsewhere_in_the_file_is_unaffected(self):
        # The guard prunes only its own body -- a sibling function's import
        # still counts, and so does one after the guard in file order.
        tree = ast.parse(
            'if __name__ == "__main__":\n'
            "    import inside_guard\n"
            "def f():\n"
            "    import inside_function\n"
            "import after_guard\n"
        )
        names = toplevel_import_names(tree)
        self.assertIn("inside_function", names)
        self.assertIn("after_guard", names)
        self.assertNotIn("inside_guard", names)

    def test_a_guard_that_is_not_on_name_is_left_alone(self):
        # Only `if __name__ == "__main__":` is dead-by-construction; any other
        # top-level `if` is ordinary conditional code that may well run.
        tree = ast.parse('if some_flag == "__main__":\n    import still_reachable\n')
        self.assertIn("still_reachable", toplevel_import_names(tree))


if __name__ == "__main__":
    unittest.main()
