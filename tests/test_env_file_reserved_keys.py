"""The LLM stub is a `canyonos test`-only control: a user's project `.env` must
never be able to inject CANYONOS_LLM_STUB_TEXT into the controller environment
(which would silently stub real LLM calls in a normal deploy)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.controller.global_controller import GlobalController


class LoadDotenvReservedKeysTests(unittest.TestCase):
    def _write_env(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_reserved_stub_key_is_not_loaded_from_user_env(self):
        os.environ.pop("CANYONOS_LLM_STUB_TEXT", None)
        os.environ.pop("MY_API_KEY", None)
        self.addCleanup(os.environ.pop, "MY_API_KEY", None)

        path = self._write_env("CANYONOS_LLM_STUB_TEXT=sneaky\nMY_API_KEY=real-secret\n")
        GlobalController._load_dotenv(path)

        # The reserved control key is ignored...
        self.assertNotIn("CANYONOS_LLM_STUB_TEXT", os.environ)
        # ...while ordinary user secrets still load as before.
        self.assertEqual(os.environ.get("MY_API_KEY"), "real-secret")

    def test_a_stub_value_already_set_is_left_untouched(self):
        # `canyonos test` sets it on the GC container; _load_dotenv must not clear it.
        os.environ["CANYONOS_LLM_STUB_TEXT"] = "test"
        self.addCleanup(os.environ.pop, "CANYONOS_LLM_STUB_TEXT", None)

        path = self._write_env("CANYONOS_LLM_STUB_TEXT=sneaky\n")
        GlobalController._load_dotenv(path)

        self.assertEqual(os.environ["CANYONOS_LLM_STUB_TEXT"], "test")


if __name__ == "__main__":
    unittest.main()
