"""`canyonos build`'s skill install: where the skill comes from, and what
happens when it can't be installed."""

import pytest

from canyonos import build as build_cmd

CODELOAD = "https://codeload.github.com/CanyonCodeCoreAI/canyoncodecore"


@pytest.mark.parametrize(
    "ref",
    ["main", "cli-v0.1.724", "some/feature-branch"],
)
def test_the_tarball_url_is_a_bare_ref(ref):
    """codeload resolves a branch, a tag or a commit from `tar.gz/<ref>`; the
    refs/heads/ form 404s for a tag, which a skill source is allowed to be."""
    assert build_cmd._tarball_url(ref) == f"{CODELOAD}/tar.gz/{ref}"


@pytest.fixture
def skill_dir(tmp_path):
    source = tmp_path / "porting-to-canyonos"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: porting-to-canyonos\n---\n")
    return source


def test_a_local_skill_directory_is_copied_into_place(skill_dir, tmp_path):
    dest = tmp_path / "skills" / "porting-to-canyonos"

    assert build_cmd.install_skill(str(dest), str(skill_dir)) is True
    assert (dest / "SKILL.md").is_file()
    # Copied, not moved: the checkout it came from is still there.
    assert (skill_dir / "SKILL.md").is_file()


def test_a_failed_copy_is_an_install_failure_not_a_traceback(
    monkeypatch, skill_dir, tmp_path
):
    dest = tmp_path / "skills" / "porting-to-canyonos"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("previous install\n")

    def denied(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(build_cmd.shutil, "copytree", denied)

    def never(*_args, **_kwargs):
        raise AssertionError("an explicit local source must not fall back to a fetch")

    monkeypatch.setattr(build_cmd, "FETCH_STRATEGIES", (("git", never),))

    assert build_cmd.install_skill(str(dest), str(skill_dir)) is False
    # Nothing was torn down on the way out.
    assert (dest / "SKILL.md").read_text() == "previous install\n"


def test_the_production_ref_is_never_read_as_a_directory(monkeypatch, tmp_path):
    """A project with a `main/` directory next to it still gets the ref fetched."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / build_cmd.SKILL_REF).mkdir()
    fetched = []

    monkeypatch.setattr(
        build_cmd,
        "FETCH_STRATEGIES",
        (("git", lambda _dest, ref: fetched.append(ref) or True),),
    )
    monkeypatch.setattr(
        build_cmd.shutil,
        "copytree",
        lambda *_a, **_k: pytest.fail("the ref was read as a path"),
    )

    assert build_cmd.install_skill(str(tmp_path / "dest"), build_cmd.SKILL_REF) is True
    assert fetched == [build_cmd.SKILL_REF]
