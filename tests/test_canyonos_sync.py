import contextlib
import shutil
import subprocess
import sys

from canyonos import sync as sync_cmd


def test_resync_removes_a_host_file_deleted_since_the_previous_sync(
    monkeypatch, tmp_path
):
    source = tmp_path / "source"
    destination = tmp_path / "workspace"
    source.mkdir()
    destination.mkdir()
    (source / "a.txt").write_text("A")
    (source / "b.txt").write_text("B")
    (source / "managed").mkdir()
    (source / "managed" / "source.txt").write_text("managed")

    monkeypatch.chdir(source)
    monkeypatch.setattr(
        sync_cmd,
        "require_state",
        lambda: {"container_id": "abcdef123456", "port": 8000},
    )
    monkeypatch.setattr(sync_cmd.ui, "status", lambda _message: contextlib.nullcontext())
    monkeypatch.setattr(
        sync_cmd, "SYNC_STATE_PATH", str(tmp_path / "sync-manifest.json")
    )

    remote_files = {}

    def fake_docker(argv, **_kwargs):
        if argv[:2] == ["docker", "cp"]:
            local_path, remote = argv[2:]
            if remote.endswith(":" + sync_cmd.GC_WORKSPACE_PATH):
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                remote_path = remote.split(":", 1)[1]
                staged_path = tmp_path / ("remote-" + remote_path.rsplit("/", 1)[-1])
                shutil.copyfile(local_path, staged_path)
                remote_files[remote_path] = staged_path
            return subprocess.CompletedProcess(argv, 0, "", "")

        assert argv[:3] == ["docker", "exec", "abcdef123456"]
        remote_manifest = remote_files[argv[-1]]
        return subprocess.run(
            [sys.executable, "-c", argv[5], str(destination), str(remote_manifest)],
            capture_output=True,
            text=True,
            check=False,
        )

    monkeypatch.setattr(sync_cmd, "run_docker", fake_docker)

    assert sync_cmd.run_sync()
    (destination / "managed" / "container-generated.txt").write_text("keep me")
    (source / "b.txt").unlink()
    shutil.rmtree(source / "managed")
    assert sync_cmd.run_sync()

    assert (destination / "a.txt").read_text() == "A"
    assert not (destination / "b.txt").exists()
    assert not (destination / "managed" / "source.txt").exists()
    assert (
        destination / "managed" / "container-generated.txt"
    ).read_text() == "keep me"
