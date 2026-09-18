import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", ".claude", "skills", "porting-to-canyonos"
    )
)
sys.path.insert(0, SKILL)

from validation.core import Report  # noqa: E402
from validation.runtime import RUNTIME_FLAT_NAMES, _base_requirements  # noqa: E402
from validation.smoke import (  # noqa: E402
    _env_file_values,
    _find_uv,
    _stub_overlay,
    check_installs_and_imports,
)

import validate as validate_module  # noqa: E402


class InvocationTests(unittest.TestCase):
    """The check has to run under the command the skill documents."""

    def _smoke_flag(self, argv):
        with patch.object(validate_module, "validate") as validated:
            validated.return_value = Report(".")
            with patch.object(validate_module, "print_report"):
                validate_module.main(argv)
        return validated.call_args.kwargs["smoke"]

    def test_the_documented_invocation_runs_the_check(self):
        # `validate.py .car` is the command the skill tells the porter to run.
        # Behind an opt-in flag nothing ever passed, the check never ran at all:
        # two ports shipped a dependency set that resolved and did not load,
        # and `canyonos deploy` was the first thing to notice.
        self.assertIs(self._smoke_flag([".car"]), True)

    def test_no_smoke_opts_out(self):
        self.assertIs(self._smoke_flag(["--no-smoke", ".car"]), False)


class EnvFileTests(unittest.TestCase):
    def test_reads_the_env_file_beside_the_artifact(self):
        # The container is handed this file via `env_file`. Without it a client
        # that validates its key during __init__ fails here and nowhere else.
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, ".car", "app")
            os.makedirs(source)
            with open(os.path.join(root, ".env"), "w", encoding="utf-8") as handle:
                handle.write(
                    "# a comment\n\nOPENAI_API_KEY=sk-test\nQUOTED='v'\nbroken\n"
                )
            values = _env_file_values(source)
        self.assertEqual(values, {"OPENAI_API_KEY": "sk-test", "QUOTED": "v"})

    def test_a_missing_env_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, ".car", "app")
            os.makedirs(source)
            self.assertEqual(_env_file_values(source), {})


class StubOverlayTests(unittest.TestCase):
    def test_writes_the_runtime_modules_the_image_provides(self):
        with tempfile.TemporaryDirectory() as overlay:
            _stub_overlay(overlay, {})
            for module in RUNTIME_FLAT_NAMES:
                self.assertTrue(os.path.isfile(os.path.join(overlay, module)), module)

    def test_writes_each_agent_stub_flat_and_at_its_entrypoint(self):
        # `canyonos build` puts the stub in both places; a smoke that only had
        # one would import the real entrypoint and pull in dependencies the
        # workflow image never installs.
        with tempfile.TemporaryDirectory() as overlay:
            _stub_overlay(overlay, {"pkg/run.py": "MyAgent"})
            self.assertTrue(os.path.isfile(os.path.join(overlay, "pkg", "run.py")))
            self.assertTrue(os.path.isfile(os.path.join(overlay, "run.py")))
            self.assertTrue(os.path.isfile(os.path.join(overlay, "pkg", "__init__.py")))
            with open(
                os.path.join(overlay, "pkg", "run.py"), encoding="utf-8"
            ) as handle:
                self.assertIn("class MyAgent", handle.read())


class BaseRequirementsFallbackTests(unittest.TestCase):
    """`canyonos_core` lives only inside `canyonos`'s own isolated `uv tool`
    environment -- `from canyonos_core import stub_generator` fails on every
    real run, not just a broken one, so this fallback is what every smoke
    check actually installs. `ipdb`/`ipython` used to sit in it as a guess,
    which meant a source's own missing import of either was never caught: the
    smoke install always carried it in as "base" regardless of what the
    source declared."""

    def test_fallback_does_not_carry_speculative_packages(self):
        with patch.dict("sys.modules", {"canyonos_core": None}):
            agent, workflow = _base_requirements()
        names = {pin.split("==")[0] for pin in agent + workflow}
        self.assertNotIn("ipython", names)
        self.assertNotIn("ipdb", names)

    def test_fallback_workflow_extends_fallback_agent(self):
        with patch.dict("sys.modules", {"canyonos_core": None}):
            agent, workflow = _base_requirements()
        for pin in agent:
            self.assertIn(pin, workflow)


class MissingUvTests(unittest.TestCase):
    def test_a_missing_uv_warns_rather_than_failing_the_port(self):
        # No uv anywhere means the set was not verified, which is worth saying;
        # it is not evidence that the port is broken.
        report = Report(".")
        with (
            patch("validation.smoke.shutil.which", return_value=None),
            patch("validation.smoke.os.access", return_value=False),
        ):
            check_installs_and_imports(
                report,
                ".",
                {"name": "A", "requirements": []},
                "a.py",
                [],
                "config.yaml",
            )
        errors, warnings = report.counts()
        self.assertEqual((errors, warnings), (0, 1))


class FindUvTests(unittest.TestCase):
    """The worker's bash tool runs non-interactive shells, which never source
    ~/.bashrc -- the one place uv's installer appends its PATH entry. Without
    a fallback, `shutil.which` reports "not found" on every real build and
    check_installs_and_imports warns-and-skips instead of ever running."""

    def test_falls_back_to_a_known_install_location_when_not_on_path(self):
        with (
            patch("validation.smoke.shutil.which", return_value=None),
            patch(
                "validation.smoke.os.access",
                side_effect=lambda path, mode: path.endswith("/.local/bin/uv"),
            ),
        ):
            self.assertTrue(_find_uv().endswith("/.local/bin/uv"))

    def test_prefers_the_path_entry_when_present(self):
        with patch("validation.smoke.shutil.which", return_value="/usr/bin/uv"):
            self.assertEqual(_find_uv(), "/usr/bin/uv")

    def test_none_when_uv_is_nowhere(self):
        with (
            patch("validation.smoke.shutil.which", return_value=None),
            patch("validation.smoke.os.access", return_value=False),
        ):
            self.assertIsNone(_find_uv())


if __name__ == "__main__":
    unittest.main()
