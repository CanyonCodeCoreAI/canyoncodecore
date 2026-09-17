import fnmatch
import subprocess

from canyonos import clean


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_matching_canyonos_images_are_removed_without_force(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **_):
        calls.append(argv)
        if argv[:2] == ["docker", "images"]:
            return completed(argv, stdout="image-one\nimage-two\n")
        return completed(argv)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(clean.subprocess, "run", fake_run)

    clean.run_clean()

    assert calls == [
        ["docker", "images", "--filter", f"reference={clean.IMAGE_REFERENCE}", "-q"],
        ["docker", "image", "rm", "image-one", "image-two"],
    ]
    assert "-f" not in calls[1]
    assert "--force" not in calls[1]


def test_docker_unavailable_warns_and_does_not_crash(monkeypatch, tmp_path):
    warnings = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        clean.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("docker")),
    )
    monkeypatch.setattr(clean.ui, "warn", warnings.append)

    clean.run_clean()

    assert any("Docker" in warning for warning in warnings)


def test_car_directory_is_removed(monkeypatch, tmp_path):
    car_dir = tmp_path / ".car"
    car_dir.mkdir()
    (car_dir / "artifact").write_text("built")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        clean.subprocess,
        "run",
        lambda argv, **_: completed(argv),
    )

    clean.run_clean()

    assert not car_dir.exists()


def test_image_removal_failure_warns_without_forcing(monkeypatch, tmp_path):
    calls = []
    warnings = []

    def fake_run(argv, **_):
        calls.append(argv)
        if argv[:2] == ["docker", "images"]:
            return completed(argv, stdout="image-in-use\n")
        return completed(argv, returncode=1, stderr="image is being used by running container")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(clean.subprocess, "run", fake_run)
    monkeypatch.setattr(clean.ui, "warn", warnings.append)

    clean.run_clean()

    assert calls[1] == ["docker", "image", "rm", "image-in-use"]
    assert "-f" not in calls[1]
    assert "image is being used by running container" in warnings[-1]


def test_image_pattern_matches_built_images_but_not_pulled_ones():
    for built in ("canyonos-workflow", "canyonos-cvpilotagent"):
        assert fnmatch.fnmatch(built, clean.IMAGE_REFERENCE)
    for pulled in (
        "ghcr.io/canyoncodecoreai/canyonos-api",
        "ghcr.io/canyoncodecoreai/canyonos-core",
        "redis",
    ):
        assert not fnmatch.fnmatch(pulled, clean.IMAGE_REFERENCE)
