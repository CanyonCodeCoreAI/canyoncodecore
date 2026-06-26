import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from ventis import cli


class CliBuildTests(unittest.TestCase):
    def setUp(self):
        self._repo_cwd = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        os.chdir(self._repo_cwd)

    def _write_config(self, root):
        config_dir = os.path.join(root, "config")
        docker_dir = os.path.join(root, "docker")
        os.makedirs(config_dir, exist_ok=True)
        os.makedirs(docker_dir, exist_ok=True)

        with open(os.path.join(config_dir, "global_controller.yaml"), "w") as f:
            yaml.safe_dump({"agents": []}, f, sort_keys=False)

        for name in ("generic-agent.Dockerfile", "global-controller.Dockerfile"):
            with open(os.path.join(docker_dir, name), "w") as f:
                f.write("FROM scratch\n")

    def _write_ec2_config(self, root):
        config_dir = os.path.join(root, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "global_controller.yaml"), "w") as f:
            yaml.safe_dump(
                {
                    "agents": [{"name": "ExampleAgent", "provider": "EC2", "entrypoint": "agents/example_agent.py"}],
                    "ec2": {
                        "region": "us-east-1",
                        "ami_id": "ami-123",
                        "instance_type": "t2.nano",
                        "subnet_id": "subnet-123",
                        "security_group_ids": ["sg-123"],
                        "ssh_user": "ec2-user",
                        "agent_image": "ec2-agent-base",
                        "controller_image": "ec2-global-controller",
                    },
                },
                f,
                sort_keys=False,
            )

    def test_build_uses_linux_amd64_platform_and_builds_controller_images(self):
        commands = []

        def _capture_run(cmd, check=True, **_kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_config(tmpdir)
            config_path = os.path.join(tmpdir, "config", "global_controller.yaml")
            args = SimpleNamespace(config=config_path)

            with patch("subprocess.run", side_effect=_capture_run):
                with patch("os.getcwd", return_value=tmpdir):
                    with patch.dict(os.environ, {}, clear=False):
                        cli.cmd_build(args)

        docker_builds = [cmd for cmd in commands if cmd[:2] == ["docker", "build"]]
        self.assertEqual(len(docker_builds), 2)
        self.assertEqual(
            docker_builds[0],
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "-f",
                os.path.join(tmpdir, "docker", "generic-agent.Dockerfile"),
                "-t",
                "ventis-agent-base",
                tmpdir,
            ],
        )
        self.assertEqual(
            docker_builds[1],
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "-f",
                os.path.join(tmpdir, "docker", "global-controller.Dockerfile"),
                "-t",
                "ventis-global-controller",
                tmpdir,
            ],
        )

    def test_build_respects_platform_override(self):
        commands = []

        def _capture_run(cmd, check=True, **_kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_config(tmpdir)
            config_path = os.path.join(tmpdir, "config", "global_controller.yaml")
            args = SimpleNamespace(config=config_path)

            with patch("subprocess.run", side_effect=_capture_run):
                with patch("os.getcwd", return_value=tmpdir):
                    with patch.dict(os.environ, {"VENTIS_DOCKER_PLATFORM": "linux/arm64"}, clear=False):
                        cli.cmd_build(args)

        docker_builds = [cmd for cmd in commands if cmd[:2] == ["docker", "build"]]
        self.assertTrue(docker_builds)
        self.assertTrue(all(cmd[2:4] == ["--platform", "linux/arm64"] for cmd in docker_builds))

    def test_ec2_build_uses_resolved_config_and_custom_image_names(self):
        commands = []

        def _capture_run(cmd, check=True, **_kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_config(tmpdir)
            self._write_ec2_config(tmpdir)
            args = SimpleNamespace(config="config/global_controller.yaml")
            os.chdir(tmpdir)
            try:
                with patch("subprocess.run", side_effect=_capture_run):
                    with patch("os.getcwd", return_value=tmpdir):
                        with patch("shutil.which", return_value="/usr/bin/docker"):
                            with patch("ventis.cli._running_on_ec2", return_value=True):
                                cli.cmd_build(args)
            finally:
                os.chdir(self._repo_cwd)

        docker_builds = [cmd for cmd in commands if cmd[:2] == ["docker", "build"]]
        self.assertIn("ec2-agent-base", docker_builds[0])
        self.assertIn("ec2-global-controller", docker_builds[1])

    def test_ec2_build_fails_fast_without_docker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_config(tmpdir)
            self._write_ec2_config(tmpdir)
            args = SimpleNamespace(config="config/global_controller.yaml")
            os.chdir(tmpdir)
            try:
                with patch("os.getcwd", return_value=tmpdir):
                    with patch("shutil.which", return_value=None):
                        with patch("ventis.cli._running_on_ec2", return_value=True):
                            with self.assertRaisesRegex(RuntimeError, "EC2 translation for `ventis build` requires local Docker"):
                                cli.cmd_build(args)
            finally:
                os.chdir(self._repo_cwd)


if __name__ == "__main__":
    unittest.main()
