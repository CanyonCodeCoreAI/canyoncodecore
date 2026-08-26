import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.utils import env_file as env_file_utils

try:
    from ventis.controller.global_controller import GlobalController
except ImportError:  # generated grpc_stubs are not on the path
    GlobalController = None


class ResolveEnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.env_path = os.path.join(self.tmpdir.name, ".env")
        with open(self.env_path, "w") as f:
            f.write("OPENAI_API_KEY=sk-test\n")

    def test_returns_none_when_env_file_is_not_configured(self):
        self.assertIsNone(env_file_utils.resolve_env_file({}))
        self.assertIsNone(env_file_utils.resolve_env_file({"env_file": None}))
        self.assertIsNone(env_file_utils.resolve_env_file({"env_file": ""}))

    def test_resolves_relative_path_against_base_dir(self):
        resolved = env_file_utils.resolve_env_file(
            {"env_file": ".env"}, base_dir=self.tmpdir.name
        )

        self.assertEqual(resolved, os.path.abspath(self.env_path))

    def test_resolves_relative_path_against_cwd_by_default(self):
        original_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)
        self.addCleanup(os.chdir, original_cwd)

        resolved = env_file_utils.resolve_env_file({"env_file": ".env"})

        self.assertEqual(os.path.realpath(resolved), os.path.realpath(self.env_path))

    def test_keeps_absolute_path_as_is(self):
        resolved = env_file_utils.resolve_env_file(
            {"env_file": self.env_path}, base_dir="/nowhere"
        )

        self.assertEqual(resolved, os.path.abspath(self.env_path))

    def test_expands_user_home_prefix(self):
        with patch.dict(os.environ, {"HOME": self.tmpdir.name}):
            resolved = env_file_utils.resolve_env_file({"env_file": "~/.env"})

        self.assertEqual(resolved, os.path.abspath(self.env_path))

    def test_raises_when_configured_file_is_missing(self):
        with self.assertRaisesRegex(ValueError, "env_file does not exist"):
            env_file_utils.resolve_env_file(
                {"env_file": "missing.env"}, base_dir=self.tmpdir.name
            )

    def test_raises_when_configured_path_is_a_directory(self):
        with self.assertRaisesRegex(ValueError, "env_file is not a file"):
            env_file_utils.resolve_env_file(
                {"env_file": self.tmpdir.name}, base_dir=self.tmpdir.name
            )

    def test_raises_when_configured_file_is_unreadable(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read anything")
        os.chmod(self.env_path, 0o000)
        self.addCleanup(os.chmod, self.env_path, 0o600)

        with self.assertRaisesRegex(ValueError, "env_file is not readable"):
            env_file_utils.resolve_env_file(
                {"env_file": ".env"}, base_dir=self.tmpdir.name
            )


class RemoteEnvPathTests(unittest.TestCase):
    def test_path_is_container_scoped_so_replicas_do_not_collide(self):
        first = env_file_utils.remote_env_path("ventis-ec2-alpha-0")
        second = env_file_utils.remote_env_path("ventis-ec2-alpha-1")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("/tmp/"))
        self.assertIn("ventis-ec2-alpha-0", first)


class EnvFileArgsTests(unittest.TestCase):
    """`env_file_args` owns the local-vs-remote staging both runtimes rely on."""

    REMOTE = "10.0.0.30"
    CONTAINER = "ventis-ec2-alpha-0"

    def setUp(self):
        self.controller = SimpleNamespace(
            env_file_path="/project/.env",
            _push_file=MagicMock(),
            _run_cmd=MagicMock(return_value=SimpleNamespace(returncode=0)),
        )

    def _args(self, host=REMOTE, is_local=False):
        return env_file_utils.env_file_args(
            self.controller, host, "ubuntu", self.CONTAINER, is_local
        )

    def test_yields_nothing_when_env_file_is_not_configured(self):
        self.controller.env_file_path = None

        with self._args() as args:
            self.assertEqual(args, [])

        self.controller._push_file.assert_not_called()
        self.controller._run_cmd.assert_not_called()

    def test_yields_nothing_when_the_controller_predates_env_file_support(self):
        del self.controller.env_file_path

        with self._args() as args:
            self.assertEqual(args, [])

    def test_local_container_reads_the_original_file_without_copying(self):
        with self._args(host="localhost", is_local=True) as args:
            self.assertEqual(args, ["--env-file", "/project/.env"])

        self.controller._push_file.assert_not_called()
        self.controller._run_cmd.assert_not_called()

    def test_remote_container_gets_a_container_scoped_copy(self):
        remote_path = env_file_utils.remote_env_path(self.CONTAINER)

        with self._args() as args:
            self.assertEqual(args, ["--env-file", remote_path])
            # Still present while the caller runs `docker run`.
            self.controller._run_cmd.assert_not_called()

        self.controller._push_file.assert_called_once_with(
            "/project/.env", remote_path, self.REMOTE, user="ubuntu"
        )
        self.controller._run_cmd.assert_called_once_with(
            ["rm", "-f", remote_path], self.REMOTE, user="ubuntu"
        )

    def test_remote_copy_is_deleted_even_when_the_body_raises(self):
        with self.assertRaisesRegex(RuntimeError, "docker exploded"):
            with self._args():
                raise RuntimeError("docker exploded")

        self.controller._run_cmd.assert_called_once_with(
            ["rm", "-f", env_file_utils.remote_env_path(self.CONTAINER)],
            self.REMOTE,
            user="ubuntu",
        )

    def test_a_failed_delete_never_masks_the_caller_error(self):
        self.controller._run_cmd.side_effect = RuntimeError("ssh down")

        with self.assertRaisesRegex(RuntimeError, "docker exploded"):
            with self._args():
                raise RuntimeError("docker exploded")

    def test_a_failed_delete_does_not_fail_a_healthy_launch(self):
        self.controller._run_cmd.return_value = SimpleNamespace(returncode=1)

        with self._args() as args:
            self.assertIn("--env-file", args)


@unittest.skipIf(GlobalController is None, "generated grpc_stubs are not on the path")
class PushFileTests(unittest.TestCase):
    """`_push_file` is how a secrets file reaches a remote host."""

    def setUp(self):
        self.controller = object.__new__(GlobalController)
        self.controller.config = {"ec2": {"ssh_private_key_path": "/keys/ventis"}}
        self.source = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.source.write("OPENAI_API_KEY=sk-test\n")
        self.source.close()
        self.addCleanup(os.unlink, self.source.name)

    def _run(self, returncode=0):
        with patch(
            "ventis.controller.global_controller.subprocess.run",
            return_value=SimpleNamespace(returncode=returncode, stdout="", stderr="nope"),
        ) as run:
            if returncode == 0:
                self.controller._push_file(
                    self.source.name, "/tmp/ventis-env-alpha", "10.0.0.30", user="ubuntu"
                )
            else:
                with self.assertRaisesRegex(RuntimeError, "Failed to copy"):
                    self.controller._push_file(
                        self.source.name,
                        "/tmp/ventis-env-alpha",
                        "10.0.0.30",
                        user="ubuntu",
                    )
        return run

    def test_writes_through_ssh_with_a_private_umask(self):
        run = self._run()

        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("/keys/ventis", argv)
        self.assertEqual(argv[-2], "ubuntu@10.0.0.30")
        # umask 077 means the copy is never briefly world-readable on the host.
        self.assertEqual(argv[-1], "umask 077; cat > /tmp/ventis-env-alpha")
        # Contents stream over stdin so the secret never lands in a command line.
        self.assertIsNotNone(run.call_args.kwargs["stdin"])

    def test_raises_when_the_copy_fails(self):
        self._run(returncode=1)


if __name__ == "__main__":
    unittest.main()
